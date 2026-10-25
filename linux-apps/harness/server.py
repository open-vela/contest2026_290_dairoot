"""web 配置台的 HTTP 层：配置台、历史会话和日记页面、配置与安装接口、一个录音代理。

- GET  /             页面
- GET  /history      历史会话页面：搜索、按日期筛选及查看会话消息
- GET  /diary        AI 日记页面：月历查看日记、编辑长期记忆
- GET  /api/config   当前配置 + SDK 的 TTS 引擎目录 + 麦克风列表（页面加载时填表单，之后不再轮询，免得覆盖正在编辑的内容）
- POST /api/config   保存配置并按新配置重建 ChatBot
- POST /api/restart  用当前配置重开一轮会话
- POST /api/send     发一句文本给 ChatBot（等同说话，照样会出声）
- POST /api/voiceprints  从当前语音消息录入姓名与声纹，保存到 harness 数据库
- DELETE /api/voiceprints  按姓名删除声纹
- POST /api/mcp/install  给缺 Python 包连不上的 MCP server 装包，装完自动重启重连
- POST /api/skills/install?filename=...  上传 ZIP 技能包或 SKILL.md，保存并重启后生效
- POST /api/skills/command  执行技能安装命令，输出通过 SSE 推送
- GET  /api/state    完整状态 + llm.messages（页面加载、保存配置、重启后各取一次）
- GET  /api/events   SSE：把会变的那部分状态和 llm.messages 推给页面，代替轮询
- GET  /api/sessions?q=...&date=YYYY-MM-DD&limit=20&offset=0  从数据库搜索历史会话并分页
- GET  /api/sessions/{session_id}  从数据库读取会话消息
- GET  /api/diaries?month=YYYY-MM  某月的日记（按日期升序）及全部日记的日期范围
- POST /api/diaries/update  唤醒后台马上整理一轮日记和记忆，进度见 status.diary
- GET  /api/memory   当前长期记忆
- PUT  /api/memory   保存页面修改的记忆（空串即清空），下次会话生效
- GET  /api/schedules  定时任务列表、配置时区和今天的日期
- POST /api/schedules  新建定时任务（配置台和定时任务 MCP 共用），立即生效
- PATCH /api/schedules/{id}  修改定时任务，只改提交的字段
- DELETE /api/schedules/{id}  删除定时任务
- POST /api/schedules/{id}/run  马上执行一次，不改变之后的排期
- GET  /audio/...    转发到 ASR 服务上的用户录音（见 get_audio）
"""

import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from sqlalchemy.exc import SQLAlchemyError

from aichat_sdk.config import ASR_SERVER_WS_URL
from schedules import TaskNotFound
from skill_install import MAX_UPLOAD_BYTES

INDEX_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
HISTORY_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.html")
DIARY_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diary.html")
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")  # 日记页按月查询，YYYY-MM
EVENT_INTERVAL = 0.5  # 秒；SSE 每隔这么久看一眼状态变没变
logger = logging.getLogger("web_console")

# ASR 服务的 http 根，跟 AsrWebSocketClient 一样按 ws 地址推出来
_ASR = urlsplit(ASR_SERVER_WS_URL)
ASR_HTTP_BASE = urlunsplit(("https" if _ASR.scheme == "wss" else "http", _ASR.netloc, "", "", ""))


def create_app(client) -> Starlette:
    http = httpx.AsyncClient(timeout=10)  # 跟进程同生死，不单独关

    async def index(request: Request):
        return FileResponse(INDEX_HTML)

    async def get_config(request: Request):
        return JSONResponse(client.config_payload())

    async def post_config(request: Request):
        try:
            await client.apply_config(await request.json())
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return _result(client)

    async def post_restart(request: Request):
        await client.restart()
        return _result(client)

    async def post_send(request: Request):
        try:
            await client.send_text((await request.json()).get("text", ""))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def post_mcp_install(request: Request):
        try:
            package = await client.install_mcp_package((await request.json()).get("name", ""))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "package": package})

    async def update_voiceprints(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求需要 JSON 对象")
            name = body.get("name", "")
            audio_url = body.get("audio_url", "")
            if not isinstance(name, str) or not isinstance(audio_url, str):
                raise ValueError("姓名和录音地址必须是字符串")
            if request.method == "DELETE":
                client.voiceprints.remove(name)
            else:
                client.voiceprints.enroll(audio_url, name)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (OSError, SQLAlchemyError):
            logger.exception("保存声纹失败")
            return JSONResponse({"ok": False, "error": "保存声纹失败，请检查数据库"}, status_code=503)
        return JSONResponse({"ok": True, "voiceprints": client.voiceprints.status()})

    async def post_skill_install(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                return JSONResponse({"ok": False, "error": "技能文件不能超过 10 MB"}, status_code=413)
            data.extend(chunk)
        try:
            skill = await client.install_skill_upload(request.query_params.get("filename", ""), bytes(data))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        status = client.status()
        return JSONResponse({"ok": True, "skill": skill, "skills": status["skills"],
                             "skills_dir": status["skills_dir"]})

    async def post_skill_command(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求需要包含 command 的 JSON 对象")
            result = await client.install_skill_command(body.get("command", ""))
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e),
                                 "output": client.skill_install_state["output"]}, status_code=400)
        status = client.status()
        return JSONResponse({"ok": True, **result, "skills": status["skills"], "skills_dir": status["skills_dir"]})

    async def get_state(request: Request):
        return JSONResponse({"status": client.status(), "messages": client.messages()})

    async def get_sessions(request: Request):
        try:
            limit = int(request.query_params.get("limit", "20"))
            offset = int(request.query_params.get("offset", "0"))
            query = request.query_params.get("q", "").strip()
            date = request.query_params.get("date", "")
            if date:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError("date 需为 YYYY-MM-DD 格式的日期")
                try:
                    datetime.date.fromisoformat(date)
                except ValueError:
                    raise ValueError("date 需为有效日期") from None
            if not 1 <= limit <= 100 or offset < 0:
                raise ValueError("limit 需为 1～100，offset 不能小于 0")
            if len(query) > 200:
                raise ValueError("搜索词不能超过 200 个字符")
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        try:
            data = await asyncio.to_thread(client.query_sessions, query=query, date=date, limit=limit, offset=offset)
        except (OSError, SQLAlchemyError, ValueError):
            logger.exception("查询历史会话失败")
            return JSONResponse({"ok": False, "error": "聊天记录暂时不可用，请稍后重试"}, status_code=503)
        return JSONResponse({"ok": True, **data}, headers={"Cache-Control": "no-store"})

    async def get_session(request: Request):
        try:
            record = await asyncio.to_thread(client.get_session, request.path_params["session_id"])
        except (OSError, SQLAlchemyError, ValueError):
            logger.exception("查询历史会话消息失败")
            return JSONResponse({"ok": False, "error": "聊天记录暂时不可用，请稍后重试"}, status_code=503)
        if record is None:
            return JSONResponse({"ok": False, "error": "会话不存在"}, status_code=404)
        return JSONResponse({"ok": True, "session": record}, headers={"Cache-Control": "no-store"})

    async def history_page(request: Request):
        return FileResponse(HISTORY_HTML)

    async def diary_page(request: Request):
        return FileResponse(DIARY_HTML)

    async def get_diaries(request: Request):
        month = request.query_params.get("month", "")
        if not MONTH.fullmatch(month):
            return JSONResponse({"ok": False, "error": "month 需为 YYYY-MM 格式的月份，例如 2026-10"}, status_code=400)
        try:
            data = await asyncio.to_thread(client.list_diaries, month)
        except (OSError, SQLAlchemyError):
            logger.exception("查询日记失败")
            return JSONResponse({"ok": False, "error": "日记暂时不可用，请稍后重试"}, status_code=503)
        return JSONResponse({"ok": True, **data}, headers={"Cache-Control": "no-store"})

    async def post_diary_update(request: Request):
        try:
            state = client.request_diary_update()
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "diary": state})

    async def get_memory(request: Request):
        try:
            memory = await asyncio.to_thread(client.get_memory)
        except (OSError, SQLAlchemyError):
            logger.exception("读取记忆失败")
            return JSONResponse({"ok": False, "error": "记忆暂时不可用，请稍后重试"}, status_code=503)
        return JSONResponse({"ok": True, "memory": memory}, headers={"Cache-Control": "no-store"})

    async def put_memory(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求需要包含 content 的 JSON 对象")
            memory = await asyncio.to_thread(client.save_memory, body.get("content"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (OSError, SQLAlchemyError):
            logger.exception("保存记忆失败")
            return JSONResponse({"ok": False, "error": "保存记忆失败，请检查数据库"}, status_code=503)
        return JSONResponse({"ok": True, "memory": memory})

    async def get_schedules(request: Request):
        try:
            data = await client.list_schedules()
        except (OSError, SQLAlchemyError):
            logger.exception("查询定时任务失败")
            return JSONResponse({"ok": False, "error": "定时任务暂时不可用，请稍后重试"}, status_code=503)
        return JSONResponse({"ok": True, **data}, headers={"Cache-Control": "no-store"})

    async def change_schedule(request: Request):
        """新建（POST）、修改（PATCH）、删除（DELETE）和立即运行（POST …/run）共用的错误处理。"""
        task_id = request.path_params.get("task_id")
        try:
            if request.method == "DELETE":
                await client.delete_schedule(task_id)
                return JSONResponse({"ok": True})
            if request.url.path.endswith("/run"):
                task = await client.run_schedule_now(task_id)
            else:
                try:
                    body = await request.json()
                except json.JSONDecodeError:
                    raise ValueError("请求需要 JSON 对象") from None
                if task_id is None:
                    task = await client.create_schedule(body)
                else:
                    task = await client.update_schedule(task_id, body)
        except TaskNotFound as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except (OSError, SQLAlchemyError):
            logger.exception("保存定时任务失败")
            return JSONResponse({"ok": False, "error": "保存定时任务失败，请检查数据库"}, status_code=503)
        return JSONResponse({"ok": True, "task": task})

    async def get_events(request: Request):
        """状态和新消息的 SSE 流：ChatBot 没有变更回调，服务端自己看着，变了才发一帧。

        llm.messages 只追加、不改已有的（压缩只影响发给模型的那份，见 ContextCompressor.compact），
        所以每帧只带 messages[start:]，start 是这条连接已经发出去的条数，页面照着往后拼。
        重启会换一个新的 ChatBot，messages 也换成新 list，这时 start 归 0 整份重发。
        """

        async def stream():
            last_status = None
            last_messages = None
            sent = 0
            while True:
                messages = client.messages()
                if messages is not last_messages:  # 换了新会话（重启 / 还没起来），整份重来
                    last_messages, sent = messages, 0
                status = client.live_status()
                if status != last_status or sent < len(messages):
                    frame = json.dumps(
                        {"status": status, "start": sent, "messages": messages[sent:]}, ensure_ascii=False
                    )
                    last_status, sent = status, len(messages)  # 发出去了才算已发
                    yield f"data: {frame}\n\n"
                await asyncio.sleep(EVENT_INTERVAL)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def get_audio(request: Request):
        """代理 ASR 服务上的用户录音。

        ASR 给的 audio_url 是它自己的地址（默认 http://127.0.0.1:8086），页面从别的机器打开时
        浏览器会去请求自己的 localhost，放不出来。让页面走同源的这条路由，谁打开都能放。
        """
        try:
            upstream = await http.get(ASR_HTTP_BASE + quote(request.url.path, safe="/"))
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"取录音失败：{e}"}, status_code=502)
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "audio/wav"),
        )

    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/config", get_config),
            Route("/api/config", post_config, methods=["POST"]),
            Route("/api/restart", post_restart, methods=["POST"]),
            Route("/api/send", post_send, methods=["POST"]),
            Route("/api/voiceprints", update_voiceprints, methods=["POST", "DELETE"]),
            Route("/api/mcp/install", post_mcp_install, methods=["POST"]),
            Route("/api/skills/install", post_skill_install, methods=["POST"]),
            Route("/api/skills/command", post_skill_command, methods=["POST"]),
            Route("/api/state", get_state),
            Route("/api/events", get_events),
            Route("/api/sessions", get_sessions),
            Route("/api/sessions/{session_id}", get_session),
            Route("/history", history_page),
            Route("/diary", diary_page),
            Route("/api/diaries", get_diaries),
            Route("/api/diaries/update", post_diary_update, methods=["POST"]),
            Route("/api/memory", get_memory),
            Route("/api/memory", put_memory, methods=["PUT"]),
            Route("/api/schedules", get_schedules),
            Route("/api/schedules", change_schedule, methods=["POST"]),
            Route("/api/schedules/{task_id:int}", change_schedule, methods=["PATCH", "DELETE"]),
            Route("/api/schedules/{task_id:int}/run", change_schedule, methods=["POST"]),
            Route("/audio/{path:path}", get_audio),
        ]
    )


def _result(client) -> JSONResponse:
    return JSONResponse({"ok": not client.last_error, "error": client.last_error, "config": client.config_payload()})


async def serve(client, host: str, port: int) -> None:
    """跑到外面把这个协程 cancel 掉（Ctrl+C）为止。"""
    server = uvicorn.Server(uvicorn.Config(create_app(client), host=host, port=port, log_level="warning"))
    # 不让 uvicorn 接管 SIGINT，Ctrl+C 照常打断主协程去关 ChatBot 和音频流
    server.capture_signals = contextlib.nullcontext
    task = asyncio.create_task(server.serve())
    try:
        await asyncio.shield(task)
    finally:
        # 直接 cancel 的话 uvicorn 会吐一大段 CancelledError，让它自己收尾
        server.should_exit = True
        await task
