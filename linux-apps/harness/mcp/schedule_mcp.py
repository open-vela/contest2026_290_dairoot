"""定时任务的 stdio MCP server：让助手按用户的话新建、查看、修改和删除定时任务。

任务由 harness 保存、到点执行（见 schedules.py）；这里只调用 harness 配置台的 /api/schedules 接口，
和页面上的「定时任务」是同一份数据。配到 mcpServers 里即可（cwd 和 PATH 跟着 harness 走）：
    {"定时任务": {"command": "python", "args": ["mcp/schedule_mcp.py"]}}
"""

import json
import logging
import sys
from typing import Annotated, Literal
import urllib.error
import urllib.request

from fastmcp import FastMCP
from pydantic import Field

HARNESS_URL = "http://127.0.0.1:8080"  # harness 配置台，与 main.py 的 WEB_PORT 一致
TIMEOUT = 10  # 秒

Repeat = Literal["once", "hourly", "daily", "weekdays", "weekly"]
Weekday = Annotated[int, Field(ge=1, le=7)]

mcp = FastMCP("schedule")
# 本机接口不走代理：环境里配了 http_proxy 时，127.0.0.1 也会被转发出去
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,  # stdout 是 MCP 的 JSON-RPC 通道，日志只能写 stderr
)
logger = logging.getLogger("schedule")


def api(method: str, path: str, payload: dict | None = None) -> dict:
    """调 harness 的定时任务接口；接口报的错（400 / 404 的 error）原样抛 RuntimeError，模型能据此改参数。"""
    request = urllib.request.Request(
        HARNESS_URL + path, method=method,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read()).get("error")
        except (ValueError, AttributeError):
            reason = None
        raise RuntimeError(reason or f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"连不上 harness（{HARNESS_URL}）：{e.reason}") from None


def brief(task: dict) -> dict:
    """交给模型看的字段：id 用来改和删，status 是下次执行时间或已完成、已暂停等状态。"""
    item = {key: task[key] for key in ("id", "title", "prompt", "enabled")}
    item.update(schedule=task["summary"], status=task["status_text"])
    if task.get("last_status") == "error" and task.get("last_error"):
        item["last_error"] = task["last_error"]
    return item


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    try:
        return {"isError": False, **api(method, path, payload)}
    except RuntimeError as e:
        logger.warning("%s %s 失败：%s", method, path, e)
        return {"isError": True, "error": str(e)}


@mcp.tool()
def create_scheduled_task(
    prompt: str = Field(description="到点时让助手做的事，写成给将来的自己的完整指令，例如「提醒用户关火」「查询深圳今天的天气并播报」"),
    repeat: Repeat = Field(default="once", description="once 一次；hourly 每小时；daily 每天；weekdays 周一到周五；weekly 每周（配合 weekdays）"),
    time: str = Field(default="", description="本地 24 小时制 HH:MM，如 07:30；hourly 只看分钟；用 in_minutes 时不填"),
    date: str = Field(default="", description="once 的本地日期 YYYY-MM-DD；不填就取这个时间下一次到来的那天"),
    weekdays: list[Weekday] = Field(default_factory=list, description="weekly 在星期几执行，1=周一 … 7=周日"),
    in_minutes: Annotated[int, Field(ge=1, le=10080)] | None = Field(
        default=None, description="相对时间：多少分钟后执行一次，「十分钟后」传 10；只用于 once，此时不填 time 和 date"),
    title: str = Field(default="", description="简短名称，可不填"),
) -> dict:
    """Create a scheduled task. When it is due, the assistant runs `prompt` on its own and speaks the result,
    even if nobody is talking to it. Use for reminders, alarms, timers and recurring briefings, e.g.
    「十分钟后提醒我关火」→ repeat=once, in_minutes=10;「每天早上七点半叫我起床」→ repeat=daily, time=07:30;
    「工作日八点播报天气」→ repeat=weekdays, time=08:00;「每周一三五晚上八点提醒我跑步」→ repeat=weekly,
    weekdays=[1,3,5], time=20:00. Write `prompt` as a complete instruction for your future self: when it runs
    there is no context from this conversation. Times are local 24-hour HH:MM.
    Afterwards tell the user in one short sentence when it will run (from status)."""
    payload = {"prompt": prompt, "repeat": repeat, "title": title}
    if in_minutes is not None:
        payload["in_minutes"] = in_minutes
    else:
        payload["time"] = time
        if date:
            payload["date"] = date
        if weekdays:
            payload["weekdays"] = weekdays
    logger.info("新建定时任务：%s", payload)
    result = _call("POST", "/api/schedules", payload)
    return {"isError": False, "task": brief(result["task"])} if not result["isError"] else result


@mcp.tool()
def list_scheduled_tasks() -> dict:
    """List all scheduled tasks with id, schedule and status (next run time, done, paused).
    Use when the user asks what reminders or scheduled tasks exist, and to find a task's id before changing or
    deleting it."""
    result = _call("GET", "/api/schedules")
    if result["isError"]:
        return result
    return {"isError": False, "timezone": result["timezone"], "tasks": [brief(task) for task in result["tasks"]]}


@mcp.tool()
def update_scheduled_task(
    task_id: int = Field(description="要改的任务 id，来自 list_scheduled_tasks"),
    prompt: str | None = Field(default=None, description="新的任务说明"),
    repeat: Repeat | None = Field(default=None, description="新的重复方式"),
    time: str | None = Field(default=None, description="新的本地时间 HH:MM"),
    date: str | None = Field(default=None, description="once 的新日期 YYYY-MM-DD"),
    weekdays: list[Weekday] | None = Field(default=None, description="weekly 的新星期几，1=周一 … 7=周日"),
    in_minutes: Annotated[int, Field(ge=1, le=10080)] | None = Field(default=None, description="改成多少分钟后执行一次"),
    title: str | None = Field(default=None, description="新的名称"),
    enabled: bool | None = Field(default=None, description="false 暂停，true 恢复"),
) -> dict:
    """Change an existing scheduled task; pass only the fields to change. enabled=false pauses it and
    enabled=true resumes it. Get task_id from list_scheduled_tasks; if several tasks could match, ask which one."""
    fields = {"prompt": prompt, "repeat": repeat, "time": time, "date": date, "weekdays": weekdays,
              "in_minutes": in_minutes, "title": title, "enabled": enabled}
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        return {"isError": True, "error": "没有要修改的字段"}
    logger.info("修改定时任务 %s：%s", task_id, payload)
    result = _call("PATCH", f"/api/schedules/{task_id}", payload)
    return {"isError": False, "task": brief(result["task"])} if not result["isError"] else result


@mcp.tool()
def delete_scheduled_task(task_id: int = Field(description="要删除的任务 id，来自 list_scheduled_tasks")) -> dict:
    """Delete a scheduled task for good, e.g.「取消关火提醒」「把每天的天气播报删了」. Get task_id from
    list_scheduled_tasks first; if several tasks could match, ask which one instead of guessing."""
    logger.info("删除定时任务 %s", task_id)
    result = _call("DELETE", f"/api/schedules/{task_id}")
    return {"isError": False, "deleted": task_id} if not result["isError"] else result


if __name__ == "__main__":
    logger.info("定时任务 mcp 启动：harness=%s", HARNESS_URL)
    mcp.run()
