"""定时任务：保存说明、重复方式和时间，到点由 main.py 的调度循环交给助手执行。

任务存在 data/db.sqlite3 的 scheduled_tasks 表；配置台和 mcp/schedule_mcp.py 都通过 harness 的
/api/schedules 接口读写，写入集中在 harness 进程。时间按 config.json 的时区（固定的 UTC 偏移）解释。
"""

import datetime
from pathlib import Path
import re

from sqlalchemy import JSON, Text, func, inspect, select
from sqlalchemy.orm import Mapped, mapped_column

from session_store import Base, _read_database, _write_database

REPEATS = {"once": "不重复", "hourly": "每小时", "daily": "每天", "weekdays": "工作日", "weekly": "每周"}
SCHEDULE_FIELDS = ("repeat", "time", "date", "weekdays", "in_minutes")
EDITABLE_FIELDS = ("title", "prompt", "repeat", "time", "date", "weekdays", "enabled")
WEEKDAYS = "一二三四五六日"
MAX_TASKS = 50
PROMPT_LIMIT = 500
TITLE_LIMIT = 40
MAX_IN_MINUTES = 7 * 24 * 60
# 到点时 harness 没在跑（关机、重启中）：这么久以内的照常补执行，更早的记为错过。
MISS_GRACE = datetime.timedelta(minutes=15)
CLOCK = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # 说明可以换行，其他控制字符不收

# 到点时发给 ChatBot 的用户消息：正文带前缀，历史会话和日记据此认出这不是用户说的话。
TRIGGER_PREFIX = "【定时任务】"
TRIGGER_INSTRUCTIONS = (
    "这条消息不是用户刚说的话，而是用户之前设置的定时任务到点自动触发，【定时任务】后面是任务说明。"
    "现在就照着做：需要查信息就调用工具，然后用一两句口语直接告诉用户；要提醒的事直接提醒。"
    "不要新建或删除定时任务，也不要问用户还要不要提醒。"
)


class TaskNotFound(LookupError):
    """要修改、删除或运行的任务不存在。"""


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text)  # 可为空串，页面显示说明的开头
    prompt: Mapped[str] = mapped_column(Text)  # 说明：到点时让助手做的事
    repeat: Mapped[str] = mapped_column(Text)  # REPEATS 的键
    time: Mapped[str] = mapped_column(Text)  # 本地 HH:MM 或 HH:MM:SS；每小时的任务只用分和秒
    date: Mapped[str | None] = mapped_column(Text)  # 不重复的任务的本地日期 YYYY-MM-DD
    weekdays: Mapped[list] = mapped_column(JSON)  # 每周的任务：1=周一 … 7=周日
    enabled: Mapped[bool]
    # 以下都是 UTC ISO 时间。updated_at 同时是找下一次执行的起点：改过的任务不补跑改之前的时间点。
    created_at: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text)
    last_run_at: Mapped[str | None] = mapped_column(Text)  # 上次执行或错过的计划时间；手动运行记运行时刻
    last_status: Mapped[str | None] = mapped_column(Text)  # done / missed / error
    last_error: Mapped[str | None] = mapped_column(Text)


# ---------------- 时间 ----------------

def timezone_of(offset: float) -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=float(offset)))


def timezone_label(offset: float) -> str:
    """页面在时间旁边显示的时区，例如 UTC+8、UTC+5:30。"""
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(round(abs(float(offset)) * 60), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def _utc(moment: datetime.datetime) -> str:
    return moment.astimezone(datetime.timezone.utc).isoformat()


def _clock_text(clock: datetime.time) -> str:
    return clock.strftime("%H:%M:%S" if clock.second else "%H:%M")


def _parse_clock(value) -> datetime.time:
    match = CLOCK.fullmatch(value.strip()) if isinstance(value, str) else None
    if match:
        hour, minute, second = (int(part or 0) for part in match.groups())
        if hour < 24 and minute < 60 and second < 60:
            return datetime.time(hour, minute, second)
    raise ValueError("time 需为 24 小时制的 HH:MM，例如 07:30")


def _parse_date(value) -> datetime.date:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("date 需为有效的 YYYY-MM-DD 日期")


def _runs_on(repeat: str, weekdays: list[int], day: datetime.date) -> bool:
    if repeat == "weekdays":
        return day.isoweekday() <= 5
    if repeat == "weekly":
        return day.isoweekday() in weekdays
    return True


def next_run(task: dict, after: datetime.datetime, tz: datetime.tzinfo) -> datetime.datetime | None:
    """after 之后（不含）的第一次执行时间；不重复的任务过了时间就是 None。"""
    clock = _parse_clock(task["time"])
    if task["repeat"] == "once":
        run = datetime.datetime.combine(_parse_date(task["date"]), clock, tzinfo=tz)
        return run if run > after else None
    local = after.astimezone(tz)
    if task["repeat"] == "hourly":
        run = local.replace(minute=clock.minute, second=clock.second, microsecond=0)
        return run if run > local else run + datetime.timedelta(hours=1)
    for offset in range(8):  # 每周的任务一周之内总有一天命中
        day = local.date() + datetime.timedelta(days=offset)
        run = datetime.datetime.combine(day, clock, tzinfo=tz)
        if run > local and _runs_on(task["repeat"], task["weekdays"], day):
            return run
    return None


def _anchor(task: dict) -> datetime.datetime:
    """找下一次执行的起点：上次执行和最后修改中较晚的那个。"""
    moments = [datetime.datetime.fromisoformat(task["updated_at"])]
    if task["last_run_at"]:
        moments.append(datetime.datetime.fromisoformat(task["last_run_at"]))
    return max(moments)


def _latest_due(task: dict, now: datetime.datetime, tz: datetime.tzinfo) -> datetime.datetime | None:
    """起点之后、now 及之前最近的一次计划时间；还没到点返回 None。"""
    run = next_run(task, _anchor(task), tz)
    if run is None or run > now:
        return None
    while (later := next_run(task, run, tz)) is not None and later <= now:
        run = later
    return run


def describe_time(moment: datetime.datetime, now: datetime.datetime, tz: datetime.tzinfo) -> str:
    """念给用户、显示在页面上的时间：今天 09:00、明天 07:30、10月28日 星期二 20:00。"""
    local, today = moment.astimezone(tz), now.astimezone(tz).date()
    day = {-1: "昨天", 0: "今天", 1: "明天", 2: "后天"}.get((local.date() - today).days)
    if day is None:
        day = f"{local.month}月{local.day}日" if local.year == today.year else f"{local.year}年{local.month}月{local.day}日"
        day += " 星期" + WEEKDAYS[local.weekday()]
    return f"{day} {_clock_text(local.time())}"


def summary(task: dict) -> str:
    """排期的一句话说明：每天 09:00、工作日 08:30、每周一、三 20:00、每小时第 15 分、10月26日 15:00。"""
    clock = _parse_clock(task["time"])
    repeat = task["repeat"]
    if repeat == "hourly":
        return f"每小时第 {clock.minute} 分" + (f" {clock.second} 秒" if clock.second else "")
    text = _clock_text(clock)
    if repeat == "once":
        day = _parse_date(task["date"])
        return f"{day.month}月{day.day}日 {text}"
    if repeat == "weekly":
        return "每周" + "、".join(WEEKDAYS[day - 1] for day in task["weekdays"]) + " " + text
    return f"{REPEATS[repeat]} {text}"


# ---------------- 校验 ----------------

def _prompt(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("请填写说明：到点时让助手做什么")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) > PROMPT_LIMIT:
        raise ValueError(f"说明不能超过 {PROMPT_LIMIT} 字")
    if CONTROL.search(value):
        raise ValueError("说明不能包含控制字符")
    return value


def _title(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("名称需为字符串")
    value = value.strip()
    if len(value) > TITLE_LIMIT or "\n" in value or CONTROL.search(value):
        raise ValueError(f"名称需为 {TITLE_LIMIT} 字以内的一行文字")
    return value


def _schedule(raw: dict, task: dict, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict:
    """新的 repeat、time、date、weekdays；task 是修改前的值（新建时为默认值）。"""
    repeat = raw.get("repeat", task["repeat"])
    if repeat not in REPEATS:
        raise ValueError("repeat 只能是 " + "、".join(REPEATS))
    minutes = raw.get("in_minutes")
    if minutes is not None:
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= MAX_IN_MINUTES:
            raise ValueError(f"in_minutes 需为 1～{MAX_IN_MINUTES} 的整数分钟")
        if "repeat" in raw and repeat != "once":
            raise ValueError("in_minutes 只用于不重复的任务")
        run = (now + datetime.timedelta(minutes=minutes)).astimezone(tz).replace(microsecond=0)
        return {"repeat": "once", "time": _clock_text(run.time()), "date": run.date().isoformat(), "weekdays": []}

    clock = _parse_clock(raw.get("time", task["time"]))
    if repeat == "hourly":
        return {"repeat": repeat, "time": _clock_text(clock.replace(hour=0)), "date": "", "weekdays": []}
    if repeat == "weekly":
        days = raw.get("weekdays", task["weekdays"])
        if (not isinstance(days, list) or not days
                or any(isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7 for day in days)):
            raise ValueError("每周的任务要选星期几：1=周一 … 7=周日")
        return {"repeat": repeat, "time": _clock_text(clock), "date": "", "weekdays": sorted(set(days))}
    if repeat != "once":
        return {"repeat": repeat, "time": _clock_text(clock), "date": "", "weekdays": []}

    # 不重复：没给日期时沿用原来还没到的那天，否则取这个时间下一次到来的那天（今天过了就是明天）
    def at(day: datetime.date) -> datetime.datetime:
        return datetime.datetime.combine(day, clock, tzinfo=tz)

    if raw.get("date"):
        day = _parse_date(raw["date"])
    elif task["repeat"] == "once" and task["date"] and at(_parse_date(task["date"])) > now:
        day = _parse_date(task["date"])
    else:
        day = now.astimezone(tz).date()
        if at(day) <= now:
            day += datetime.timedelta(days=1)
    if at(day) <= now:
        raise ValueError("这个时间已经过去了，请选一个将来的时间")
    return {"repeat": repeat, "time": _clock_text(clock), "date": day.isoformat(), "weekdays": []}


def _normalize(raw, current: dict | None, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict:
    """校验页面或 MCP 提交的字段，返回要保存的 EDITABLE_FIELDS；修改时只动提交了的字段。"""
    if not isinstance(raw, dict):
        raise ValueError("请求需要 JSON 对象")
    if current is None:
        task = {"title": "", "prompt": "", "repeat": "once", "time": "", "date": "", "weekdays": [], "enabled": True}
    else:
        task = {key: current[key] for key in EDITABLE_FIELDS}
    if current is None or "prompt" in raw:
        task["prompt"] = _prompt(raw.get("prompt"))
    if "title" in raw:
        task["title"] = _title(raw["title"])
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("enabled 需为 true 或 false")
        task["enabled"] = raw["enabled"]
    # 只改名称、说明或启停时不重新校验时间，已经执行过的一次性任务也能改说明
    if current is None or any(key in raw for key in SCHEDULE_FIELDS):
        task.update(_schedule(raw, task, now=now, tz=tz))
    return task


# ---------------- 数据库 ----------------

def _task_dict(record: ScheduledTask) -> dict:
    return {
        "id": record.id, "title": record.title, "prompt": record.prompt, "repeat": record.repeat,
        "time": record.time, "date": record.date or "", "weekdays": list(record.weekdays or []),
        "enabled": record.enabled, "created_at": record.created_at, "updated_at": record.updated_at,
        "last_run_at": record.last_run_at or "", "last_status": record.last_status or "",
        "last_error": record.last_error or "",
    }


def _apply(record: ScheduledTask, task: dict) -> None:
    for key in EDITABLE_FIELDS:
        setattr(record, key, task[key] if key != "date" else task[key] or None)


def _has_table(session) -> bool:
    return inspect(session.connection()).has_table(ScheduledTask.__tablename__)


def present(task: dict, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict:
    """接口返回的任务：附上排期说明、下一次执行时间和状态文字（已按配置时区换算）。"""
    upcoming, status = None, ""
    if task["enabled"]:
        due = _latest_due(task, now, tz)
        if due is not None and now - due <= MISS_GRACE:
            upcoming, status = due, "马上执行"
        else:
            upcoming = next_run(task, max(_anchor(task), now), tz)
            if upcoming is not None:
                status = "下次 " + describe_time(upcoming, now, tz)
            elif due is not None:
                status = "已错过"  # 超过宽限还没执行，调度循环下一轮会记下来
    if not status:
        status = "已暂停" if not task["enabled"] else {"missed": "已错过", "error": "执行失败"}.get(
            task["last_status"], "已完成")
    last_run = datetime.datetime.fromisoformat(task["last_run_at"]) if task["last_run_at"] else None
    return {
        **task,
        "summary": summary(task),
        "next_run": upcoming.astimezone(tz).isoformat() if upcoming else "",
        "status_text": status,
        "last_run_text": describe_time(last_run, now, tz) if last_run else "",
    }


def list_tasks(path: str, *, now: datetime.datetime, tz: datetime.tzinfo) -> list[dict]:
    """全部任务，按创建顺序；数据库或表还不存在时返回空列表，不创建文件。"""
    with _read_database(path) as session:
        if session is None or not _has_table(session):
            return []
        tasks = [_task_dict(record) for record in session.scalars(select(ScheduledTask).order_by(ScheduledTask.id))]
    return [present(task, now=now, tz=tz) for task in tasks]


def get_task(path: str, task_id: int, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict | None:
    with _read_database(path) as session:
        if session is None or not _has_table(session):
            return None
        record = session.get(ScheduledTask, task_id)
        task = _task_dict(record) if record is not None else None
    return present(task, now=now, tz=tz) if task else None


def create_task(path: str, raw, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict:
    task = _normalize(raw, None, now=now, tz=tz)
    stamp = _utc(now)
    with _write_database(path) as session:
        if session.scalar(select(func.count()).select_from(ScheduledTask)) >= MAX_TASKS:
            raise ValueError(f"定时任务最多 {MAX_TASKS} 个，先删掉不用的")
        record = ScheduledTask(created_at=stamp, updated_at=stamp)
        _apply(record, task)
        session.add(record)
        session.flush()
        created = _task_dict(record)
    return present(created, now=now, tz=tz)


def update_task(path: str, task_id: int, raw, *, now: datetime.datetime, tz: datetime.tzinfo) -> dict:
    if not Path(path).exists():
        raise TaskNotFound("定时任务不存在")
    with _write_database(path) as session:
        record = session.get(ScheduledTask, task_id)
        if record is None:
            raise TaskNotFound("定时任务不存在")
        _apply(record, _normalize(raw, _task_dict(record), now=now, tz=tz))
        record.updated_at = _utc(now)
        updated = _task_dict(record)
    return present(updated, now=now, tz=tz)


def delete_task(path: str, task_id: int) -> None:
    if not Path(path).exists():
        raise TaskNotFound("定时任务不存在")
    with _write_database(path) as session:
        record = session.get(ScheduledTask, task_id)
        if record is None:
            raise TaskNotFound("定时任务不存在")
        session.delete(record)


def mark_runs(path: str, runs: list[tuple[int, datetime.datetime]], *, status: str, error: str = "") -> None:
    """记下执行（或错过）的计划时间和结果；期间被删掉的任务跳过。"""
    with _write_database(path) as session:
        for task_id, moment in runs:
            record = session.get(ScheduledTask, task_id)
            if record is not None:
                record.last_run_at = _utc(moment)
                record.last_status = status
                record.last_error = error or None


def collect_due(
    path: str, *, now: datetime.datetime, tz: datetime.tzinfo,
) -> tuple[list[tuple[dict, datetime.datetime]], datetime.datetime | None]:
    """到点待执行的任务及其计划时间，以及之后最早的一次执行时间（调度循环睡到那时）。

    超过 MISS_GRACE 还没执行的（harness 当时没在跑）直接记为错过，不再补执行。
    """
    with _read_database(path) as session:
        if session is None or not _has_table(session):
            return [], None
        records = session.scalars(
            select(ScheduledTask).where(ScheduledTask.enabled.is_(True)).order_by(ScheduledTask.id)
        )
        tasks = [_task_dict(record) for record in records]
    due, missed, wake = [], [], None
    for task in tasks:
        run = _latest_due(task, now, tz)
        if run is not None:
            (due if now - run <= MISS_GRACE else missed).append((task, run))
        upcoming = next_run(task, max(_anchor(task), now), tz)
        if upcoming is not None and (wake is None or upcoming < wake):
            wake = upcoming
    if missed:
        mark_runs(path, [(task["id"], run) for task, run in missed], status="missed")
    return due, wake


def trigger_text(tasks: list[dict]) -> str:
    """同一时刻到点的几个任务合成一条消息，助手一次说完。"""
    prompts = [task["prompt"] for task in tasks]
    if len(prompts) == 1:
        return TRIGGER_PREFIX + prompts[0]
    return TRIGGER_PREFIX + "\n" + "\n".join(f"{index}. {prompt}" for index, prompt in enumerate(prompts, 1))
