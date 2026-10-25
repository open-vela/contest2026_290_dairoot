"""带 web 配置台的语音对话 demo（在 tests/test_run_v2.py 基础上加了 web 配置和查看）。

音频照旧走本机：麦克风 sd.InputStream → ChatBot，TTS 音频 → sd.OutputStream；
web 页面提供配置、聊天记录、声纹录入、定时任务和 AI 日记；声纹保存在 harness 本机数据库。
后台每小时把新保存的会话整理成日记并更新长期记忆（见 diary.py），新会话开始时把记忆接在人设后面。
定时任务到点时交给助手执行并播报（见 schedules.py），会话已结束就像唤醒词一样开一轮新会话。

启动：python examples/web/main.py，然后打开 http://127.0.0.1:8080
"""

import asyncio
import datetime
import json
import logging
import os
import shlex
import shutil
import sys
import urllib.request
import uuid
import zoneinfo

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from sqlalchemy.exc import SQLAlchemyError

load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)  # server.py
sys.path.insert(0, os.path.dirname(os.path.dirname(BASE_DIR)))  # aichat_sdk

import diary
import schedules
from server import serve
from session_store import get_session, query_sessions, save_debug_log, save_session
from skill_install import MAX_OUTPUT_CHARS, install_skill, run_skill_command

from aichat_sdk import DEFAULT_TTS_ENGINE, get_tts_engines
from voiceprint import VoiceprintChatBot as ChatBot, VoiceprintClient
from aichat_sdk import config as sdk_config
from aichat_sdk.config import DEFAULT_AGENT_SOUL
from aichat_sdk.llm import mcp as sdk_mcp
from aichat_sdk.llm.mcp import (
    DEFAULT_TOOLS_SERVER,
    get_mcp_server_status,
    register_default_tools_server,
    remove_mcp_server,
)
from aichat_sdk.llm.system_prompt import SKILLS_DIR, load_skills
from aichat_sdk.llm.type_enum import AgentInfo, DeviceInfo
from aichat_sdk.mcp_tool import mcp as tools_mcp

# SDK 默认 10 秒，板上不够用：aichat-tools 子进程光 import aichat_sdk 就要 9.9 秒
# （aarch64，Mac 上 0.8 秒），连上稳定要 10.3 秒，每次都差一点点超时被跳过。
sdk_mcp.MCP_TOOL_TIMEOUT_SECONDS = 20

logger = logging.getLogger("web_console")

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SESSION_DB_PATH = os.path.join(BASE_DIR, "data", "db.sqlite3")
DEBUG = os.getenv("DEBUG", "false").strip().lower() == "true"
WEB_HOST = "0.0.0.0"  # 注意：页面能改外部 MCP server 的启动命令（等于能在本机拉进程），绑 0.0.0.0 后同网段都能访问
WEB_PORT = 8080
OUTPUT_SAMPLE_RATE = 24000  # 采样率不放到页面上配，扬声器流建一次就一直用
DEFAULT_SPEAKER_VOLUME = 80  # 启动时把扬声器拉到这个音量；之后由「音量」MCP 按对话调整
PIP_INSTALL_TIMEOUT = 300  # 秒；pip 卡在网络上别让「安装中」永远转下去
DIARY_FIRST_DELAY = 60  # 秒；启动后先等一会儿再整理日记，避开板上的启动高峰
DIARY_INTERVAL = 3600  # 秒；之后每小时把新保存的会话整理进日记和记忆
SCHEDULE_IDLE_WAIT = 60  # 秒；定时任务到点时正在对话，最多等这么久让这一轮说完再插进去
SCHEDULE_MAX_SLEEP = 60  # 秒；调度循环没事也每分钟醒一次，系统时间被调过也能跟上

DEFAULT_CONFIG = {
    "tts_engine": DEFAULT_TTS_ENGINE,
    "mic": "",  # 拾音设备名，空 = 系统默认输入设备
    "send_idle_farewell": False,
    "tool_thinking": True,
    "timezone": 8,
    "city": "深圳",
    "soul": DEFAULT_AGENT_SOUL,
    # aichat-tools 各工具的开关，{工具名: 是否启用}，缺省启用（不走下面的 mcpServers 配置）
    "aichat_tools": {},
    # 技能的开关，{技能名: 是否启用}，缺省启用；停用的不烘进系统提示词
    "skills": {},
    # 外部 MCP server，格式同 AgentInfo.mcp；fetch 用 uvx 起在它自己的环境里——mcp-server-fetch 钉
    # mcp<2，装进本 venv 会跟 SDK 依赖的 fastmcp 要的 mcp>=2 打架（表现为 server 起不来报 ImportError）。
    # 本地 MCP 脚本统一放在 mcp/，依赖在 pyproject 里，直接用本 venv 的 python 起。
    # python 靠 PATH 找到本 venv，脚本路径相对于 harness 工作目录。
    "mcp": {
        "mcpServers": {
            "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]},
            "米家": {"command": "python", "args": ["mcp/miloco_mcp.py"]},
            "音量": {"command": "python", "args": ["mcp/volume_mcp.py"]},
            "定时任务": {"command": "python", "args": ["mcp/schedule_mcp.py"]},
        }
    },
}


def _tts_requirements(engine: str) -> tuple[str, tuple[str, ...]]:
    engine = engine.strip().lower()
    for item in get_tts_engines():
        if engine == item["name"] or engine in item["aliases"]:
            engine = item["name"]
            break
    bytedance = ("BYTEDANCE_TTS_APP_ID", "BYTEDANCE_TTS_APP_KEY")
    return {
        "bytedancev1": ("豆包 v1", bytedance),
        "bytedancev2": ("豆包 v2", bytedance),
        "mimo": ("小米 MiMo", ("MIMO_API_KEY",)),
        "edge": ("微软 Edge", ()),
    }.get(engine, (engine, ()))


def tts_configuration_error(engine: str) -> str:
    """检查 SDK 实际加载的配置，只列缺失项，不输出凭据值。"""
    label, required = _tts_requirements(engine)
    missing = [name for name in required if not str(getattr(sdk_config, name, "") or "").strip()]
    if missing:
        return f"TTS（{label}）尚未配置：请在 .env 中填写 {'、'.join(missing)}，然后重启服务。"
    return ""


def startup_error(error: BaseException, tts_engine: str) -> str:
    """SDK 将连接异常转成 SystemExit；从原异常的来源判断失败组件。"""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        traceback = error.__traceback__
        while traceback is not None:
            module = traceback.tb_frame.f_globals.get("__name__", "")
            if module.startswith("aichat_sdk.asr."):
                return "语音识别（ASR）服务连接失败，请检查 ASR 服务是否启动及 ASR_SERVER_WS_URL 配置。"
            if module.startswith("aichat_sdk.tts."):
                label, _ = _tts_requirements(tts_engine)
                return f"TTS（{label}）连接失败，请检查该引擎的凭据和网络连接，具体原因见服务日志。"
            traceback = traceback.tb_next
        error = error.__cause__ or error.__context__
    return "对话服务初始化失败，请查看服务日志中的具体原因。"


async def set_default_volume() -> None:
    """把扬声器拉到 DEFAULT_SPEAKER_VOLUME。失败只记一条日志：板上没装 pulseaudio-utils
    或音频服务没起来时，不该连带着让整个对话进程起不来。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{DEFAULT_SPEAKER_VOLUME}%",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logger.warning("设置默认音量失败：%s", exc)
        return
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning("设置默认音量超时，请检查当前用户的 PulseAudio 服务")
        return
    if proc.returncode:
        logger.warning("设置默认音量失败：%s", stderr.decode(errors="replace").strip() or proc.returncode)


def input_devices() -> list[str]:
    """当前可用的输入设备名，页面下拉用（重名的只留一个，反正也只能按名字选）。"""
    return list(dict.fromkeys(device["name"] for device in sd.query_devices() if device["max_input_channels"] > 0))


def resolve_mic(name: str) -> int | None:
    """设备名 → 设备号；空名字或设备已经不在（拔了/改名了）都返回 None，即用系统默认。"""
    if name:
        for device in sd.query_devices():
            if device["max_input_channels"] > 0 and device["name"] == name:
                return device["index"]
    return None


def detect_location() -> dict:
    """按出口 IP 定位时区和城市（https://ipinfo.io/json），config.json 不存在时用；失败返回空 dict 走默认值。"""
    try:
        with urllib.request.urlopen("https://ipinfo.io/json", timeout=5) as resp:
            info = json.load(resp)
        offset = datetime.datetime.now(zoneinfo.ZoneInfo(info["timezone"])).utcoffset().total_seconds() / 3600
    except Exception as exc:
        logger.warning("IP 定位失败（%s），时区/城市用默认值", exc)
        return {}
    detected = {"timezone": int(offset) if offset.is_integer() else offset}
    if str(info.get("city", "")).strip():
        detected["city"] = str(info["city"]).strip()
    logger.info("IP 定位：%s（UTC%+g）", detected.get("city", "未知城市"), offset)
    return detected


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config.update(json.load(f))
    else:
        config.update(detect_location())
    return validate_config(config)


def save_config(config: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def _load_tools_catalog() -> list[dict]:
    """aichat-tools 的完整工具目录（含被停用的），从进程内的同一注册表拿，页面渲染开关用。"""
    tools = asyncio.run(tools_mcp.list_tools())
    return sorted(
        (
            {"name": tool.name, "description": tool.description or ""}
            for tool in tools
            if not ((tool.meta or {}).get("aichat_sdk") or {}).get("internal")
        ),
        key=lambda tool: tool["name"],
    )


TOOLS_CATALOG = _load_tools_catalog()


def disabled_aichat_tools(config: dict) -> set[str]:
    return {name for name, on in config["aichat_tools"].items() if not on}


def normalize_mcp_servers(mcp: dict) -> dict:
    """接收 MCP 配置对象或 mcpServers 内部映射，并逐个校验服务配置。"""
    servers = mcp.get("mcpServers", mcp)
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 要是 JSON 对象")

    for name, spec in servers.items():
        if not name.strip():
            raise ValueError("server 名称不能为空")
        if isinstance(spec, str):
            if not spec.strip():
                raise ValueError(f"server「{name}」要填命令或 URL")
        elif isinstance(spec, dict):
            if not (spec.get("command") or spec.get("url")):
                raise ValueError(f"server「{name}」要填命令或 URL")
        else:
            raise ValueError(f"server「{name}」的配置要是字符串或 JSON 对象")
    return servers


def effective_mcp(mcp: dict) -> dict:
    """真正交给 AgentInfo 的那份：去掉停用的 server，以及只给页面用的 enabled 字段。"""
    servers = {}
    for name, spec in mcp.get("mcpServers", {}).items():
        if isinstance(spec, dict):
            if spec.get("enabled") is False:
                continue
            spec = {key: value for key, value in spec.items() if key != "enabled"}
        servers[name] = spec
    return {"mcpServers": servers}


def missing_python_module(spec, error: str) -> tuple[str, str] | None:
    """server 连不上的原因是「python -m 的模块没装」时，返回 (解释器, 模块名)，页面据此给一键安装按钮。

    `python -m 没装的模块` 会往 stderr 打一行「No module named xxx」然后退出，
    _connect_external 把它带进了 error；模块装上了但缺依赖也是这句（引号包着依赖名），
    重装模块本身的包会顺带补齐依赖，一样能治。
    """
    if "No module named" not in error:
        return None
    if isinstance(spec, str):
        parts = shlex.split(spec)
        command, args = (parts[0] if parts else ""), parts[1:]
    elif isinstance(spec, dict):
        command, args = spec.get("command") or "", list(spec.get("args") or [])
    else:
        return None
    if not os.path.basename(command).lower().startswith("python"):
        return None  # uvx / npx 这些自带安装逻辑，装不动的原因五花八门，不掺和
    if "-m" in args and args.index("-m") + 1 < len(args):
        module = args[args.index("-m") + 1]
        if module and not module.startswith("-"):
            return command, module
    return None


async def run_install_command(args: list[str]) -> tuple[int, str]:
    """跑一条安装命令，返回 (退出码, stdout+stderr)；超时杀掉进程并抛 ValueError。"""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=PIP_INSTALL_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError(f"「{' '.join(args)}」超过 {PIP_INSTALL_TIMEOUT} 秒没跑完，已放弃")
    return proc.returncode, output.decode(errors="replace")


def validate_config(raw: dict) -> dict:
    """校验页面提交的配置，返回干净的一份；不合法就抛 ValueError，消息直接显示给页面。"""
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")

    engine = str(raw.get("tts_engine", "")).strip().lower()
    engines = get_tts_engines()
    match = next((item for item in engines if engine == item["name"] or engine in item["aliases"]), None)
    if match is None:
        raise ValueError(f"tts_engine 只能是：{'、'.join(item['name'] for item in engines)}")
    engine = match["name"]

    soul = str(raw.get("soul", "")).strip()
    if not soul:
        raise ValueError("角色人设不能为空")

    try:
        timezone = float(raw.get("timezone", 8))
    except (TypeError, ValueError):
        raise ValueError("时区要填数字，比如 8")

    tool_toggles = raw.get("aichat_tools", {})
    if not isinstance(tool_toggles, dict):
        raise ValueError("aichat_tools 要是 JSON 对象（{工具名: 是否启用}）")

    skill_toggles = raw.get("skills", {})
    if not isinstance(skill_toggles, dict):
        raise ValueError("skills 要是 JSON 对象（{技能名: 是否启用}）")

    mcp = raw.get("mcp") or {}
    if isinstance(mcp, str):  # 页面 JSON 模式直接给一段文本，交给这里解析，报错信息统一
        try:
            mcp = json.loads(mcp) if mcp.strip() else {}
        except json.JSONDecodeError as e:
            raise ValueError(f"MCP 配置不是合法 JSON：{e}")
    if not isinstance(mcp, dict):
        raise ValueError("MCP 配置要是 JSON 对象")
    mcp = {"mcpServers": normalize_mcp_servers(mcp)}

    return {
        "tts_engine": engine,
        "mic": str(raw.get("mic", "")).strip(),
        "send_idle_farewell": bool(raw.get("send_idle_farewell")),
        "tool_thinking": bool(raw.get("tool_thinking")),
        "timezone": int(timezone) if timezone.is_integer() else timezone,
        "city": str(raw.get("city", "")).strip(),
        "soul": soul,
        "aichat_tools": {str(name): bool(on) for name, on in tool_toggles.items()},
        "skills": {str(name): bool(on) for name, on in skill_toggles.items()},
        "mcp": mcp,
    }


class AIClient:
    def __init__(self, config: dict):
        self.config = config
        self.voiceprints = VoiceprintClient(SESSION_DB_PATH)
        self.chat_bot: ChatBot | None = None
        self.skills: list[dict] = []
        self.last_error = ""
        self.is_playing = False
        self.is_session_closed = False
        self.is_restarting = False
        self.installing = ""  # 正在 pip install 依赖的 server 名，页面按钮显示「安装中」并防连点
        self.mic_device = ""  # 实际拾音的输入设备名（配的设备没了就是回退到的那个），页面挂个 badge 显示
        self._session_started_at: datetime.datetime | None = None
        self._session_ended_at: datetime.datetime | None = None
        self._session_id = uuid.uuid4().hex
        self._messages_saved = False
        self._debug_log_saved = False
        self._play_task: asyncio.Task | None = None
        self._text_tasks: set[asyncio.Task] = set()
        self._output_stream: sd.OutputStream | None = None
        self._input_stream: sd.InputStream | None = None
        self._lock = asyncio.Lock()
        self._skill_install_lock = asyncio.Lock()
        self.skill_install_state = {"running": False, "output": "", "error": ""}
        # 整理状态走 SSE 推给页面；每次整体替换，变化才能被比较出来。
        self.diary_state = {"running": False, "finished_at": "", "error": "", "dates": [],
                            "sessions": 0, "pending": 0, "skipped": 0}
        self._diary_wakeup = asyncio.Event()  # 页面点「立即整理」时唤醒后台循环
        self._diary_task: asyncio.Task | None = None
        # 任务每次增删改或执行后 revision 加一，页面经 SSE 发现变化再重取列表；error 是调度循环自身的错误。
        self.schedule_state = {"revision": 0, "error": ""}
        self._schedule_wakeup = asyncio.Event()  # 任务有改动或要手动运行时唤醒调度循环重算
        self._schedule_task: asyncio.Task | None = None
        self._manual_runs: list[int] = []  # 页面点了「立即运行」、等调度循环执行的任务 id

    # ---------------- web 层用到的接口 ----------------

    def config_payload(self) -> dict:
        """给页面的配置：附 SDK 引擎目录和当前麦克风列表。"""
        return {**self.config, "tts_engines": get_tts_engines(), "mics": input_devices()}

    def query_sessions(self, *, query: str = "", date: str = "", limit: int = 20, offset: int = 0) -> dict:
        return query_sessions(SESSION_DB_PATH, query=query, date=date, timezone=self.config["timezone"],
                              limit=limit, offset=offset)

    def get_session(self, session_id: str) -> dict | None:
        return get_session(SESSION_DB_PATH, session_id)

    def list_diaries(self, month: str) -> dict:
        return diary.list_diaries(SESSION_DB_PATH, month)

    def get_memory(self) -> dict:
        return diary.get_memory(SESSION_DB_PATH)

    def save_memory(self, content) -> dict:
        """页面修改的记忆：统一换行、去掉首尾空白后覆盖保存，空串即清空；下次会话生效。"""
        if not isinstance(content, str):
            raise ValueError("记忆内容必须是字符串")
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(content) > diary.MEMORY_LIMIT:
            raise ValueError(f"记忆不能超过 {diary.MEMORY_LIMIT} 字")
        return diary.save_memory(SESSION_DB_PATH, content)

    def request_diary_update(self) -> dict:
        """页面点「立即整理」：唤醒后台循环马上整理一轮，结果随 status.diary 推给页面。"""
        if self.diary_state["running"]:
            raise ValueError("正在整理日记，请稍候")
        self._diary_wakeup.set()
        return dict(self.diary_state)

    def _schedule_clock(self) -> tuple[datetime.datetime, datetime.timezone]:
        """定时任务按配置时区解释：返回当前 UTC 时间和配置时区。"""
        return datetime.datetime.now(datetime.timezone.utc), schedules.timezone_of(self.config["timezone"])

    async def list_schedules(self) -> dict:
        now, tz = self._schedule_clock()
        tasks = await asyncio.to_thread(schedules.list_tasks, SESSION_DB_PATH, now=now, tz=tz)
        return {"tasks": tasks, "timezone": schedules.timezone_label(self.config["timezone"]),
                "today": now.astimezone(tz).date().isoformat()}

    async def create_schedule(self, raw) -> dict:
        now, tz = self._schedule_clock()
        task = await asyncio.to_thread(schedules.create_task, SESSION_DB_PATH, raw, now=now, tz=tz)
        self._schedules_changed()
        return task

    async def update_schedule(self, task_id: int, raw) -> dict:
        now, tz = self._schedule_clock()
        task = await asyncio.to_thread(schedules.update_task, SESSION_DB_PATH, task_id, raw, now=now, tz=tz)
        self._schedules_changed()
        return task

    async def delete_schedule(self, task_id: int) -> None:
        await asyncio.to_thread(schedules.delete_task, SESSION_DB_PATH, task_id)
        self._schedules_changed()

    async def run_schedule_now(self, task_id: int) -> dict:
        """页面点「立即运行」：交给调度循环马上执行一次，不改变之后的排期。"""
        now, tz = self._schedule_clock()
        task = await asyncio.to_thread(schedules.get_task, SESSION_DB_PATH, task_id, now=now, tz=tz)
        if task is None:
            raise schedules.TaskNotFound("定时任务不存在")
        if task_id not in self._manual_runs:
            self._manual_runs.append(task_id)
        self._schedule_wakeup.set()
        return task

    def _bump_schedule_revision(self) -> None:
        # 整体替换，SSE 才比较得出变化
        self.schedule_state = {**self.schedule_state, "revision": self.schedule_state["revision"] + 1}

    def _schedules_changed(self) -> None:
        """任务有增删改：通知页面重取列表，并唤醒调度循环按新的排期重算。"""
        self._bump_schedule_revision()
        self._schedule_wakeup.set()

    def live_status(self) -> dict:
        """会话跑着自己就会变的那几项，走 SSE 推给页面。"""
        return {
            "running": self.chat_bot is not None,
            "restarting": self.is_restarting,
            "session_closed": self.is_session_closed,
            "speaking": self.is_playing,
            "round": self.chat_bot.llm.round if self.chat_bot else 0,
            "error": self.last_error,
            "compression": self.compression_status(),
            "skill_install": dict(self.skill_install_state),
            "voiceprints": self.voiceprints.status(),
            "diary": dict(self.diary_state),
            "schedules": dict(self.schedule_state),
        }

    def status(self) -> dict:
        """完整状态：live 的加上只在重启/保存配置后才变的（引擎、麦克风、MCP、技能）。"""
        return {
            **self.live_status(),
            "tts_engine": self.config["tts_engine"],
            "mic": self.mic_device,
            "mcp": self.mcp_status(),
            "skills": [{**skill, "enabled": self.config["skills"].get(skill["name"], True)} for skill in self.skills],
            "skills_dir": str(SKILLS_DIR),
        }

    def compression_status(self) -> dict:
        """上下文压缩状态：页面在消息列表插「已压缩」分隔线、头部挂「压缩中」badge 用。"""
        if self.chat_bot is None:
            return {}
        comp = self.chat_bot.llm.llm_client.compressor
        return {"compressing": comp.compressing, "cursor": comp.cursor, "summary": comp.summary or ""}

    def mcp_status(self) -> list[dict]:
        """每个外部 MCP server 一行：配置里的启用状态 + 上次启动时的连接结果，页面挂状态用。"""
        states = {state["name"]: state for state in get_mcp_server_status()}
        rows = []
        for name, spec in self.config["mcp"].get("mcpServers", {}).items():
            state = states.pop(name, {})
            missing = missing_python_module(spec, state.get("error", ""))
            rows.append(
                {
                    "name": name,
                    "enabled": not (isinstance(spec, dict) and spec.get("enabled") is False),
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": state.get("tools", []),
                    # 缺 pip 包连不上的，给页面报可安装的包名（PyPI 上 _ 和 - 等价，展示用 - 的写法）
                    "install": missing[1].replace("_", "-") if missing else "",
                    "installing": name == self.installing,
                }
            )
        # SDK 自带工具的 server 不走 mcpServers 配置，每个工具由 aichat_tools 开关控制；
        # 工具列表用完整目录（停用的不在注册表的列表里，但行上要显示才能再启用）
        if DEFAULT_TOOLS_SERVER not in self.config["mcp"].get("mcpServers", {}):
            state = states.pop(DEFAULT_TOOLS_SERVER, {})
            toggles = self.config["aichat_tools"]
            rows.append(
                {
                    "name": DEFAULT_TOOLS_SERVER,
                    "enabled": True,
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": [{**tool, "enabled": toggles.get(tool["name"], True)} for tool in TOOLS_CATALOG],
                    "readonly": True,
                    "toggle": True,
                }
            )
        # 其他默认注册、不在配置里的 server，页面上显示为只读行
        for name, state in states.items():
            rows.append(
                {
                    "name": name,
                    "enabled": True,
                    "connected": bool(state.get("connected")),
                    "error": state.get("error", ""),
                    "tools": state.get("tools", []),
                    "readonly": True,
                }
            )
        return rows

    async def send_text(self, text: str) -> None:
        """页面手打的文本，走跟 ASR 识别结果一样的链路（照样会出声）；不合法就抛 ValueError 给页面。"""
        text = text.strip()
        if not text:
            raise ValueError("消息不能为空")
        chat_bot = self.chat_bot
        if chat_bot is None or self.is_restarting:
            raise ValueError("ChatBot 没在运行，先修好配置或点「重新开始」")
        if self.is_session_closed:
            raise ValueError("会话已结束，点「重新开始」再聊")
        self._start_text_task(chat_bot.send_text(text))

    def _start_text_task(self, coroutine) -> None:
        """在后台跑一轮 ChatBot.send_text：手打的文本和到点的定时任务都走这里。"""
        # 立刻停拾音，别让接下来扬声器里的回答被麦克风收回去（play_audio 收到 stt 也会置位，
        # 但那要等 send_text 真的跑起来）
        self.is_playing = True
        # send_text 要等整轮回答说完才返回，这里不等：HTTP 立刻返回，页面照旧轮询看消息
        task = asyncio.create_task(coroutine)
        self._text_tasks.add(task)
        task.add_done_callback(self._finish_text_task)

    def _finish_text_task(self, task: asyncio.Task) -> None:
        self._text_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.is_playing = False  # 兜底：这轮没能正常播完，别把拾音一直关着
            logger.error("发送文本失败", exc_info=(type(error), error, error.__traceback__))

    def messages(self) -> list[dict]:
        return self.chat_bot.llm.messages if self.chat_bot else []

    async def apply_config(self, raw: dict) -> None:
        """校验并保存页面提交的配置，再按新配置重建 ChatBot（人设/MCP 都是建 LlmAgent 时烘进系统提示词的，只能重建）。"""
        config = validate_config(raw)
        save_config(config)
        await self.restart(config)

    async def install_skill_upload(self, filename: str, data: bytes) -> dict:
        if self._skill_install_lock.locked():
            raise ValueError("已有技能正在安装，请稍后再试")
        async with self._skill_install_lock:
            skill = install_skill(filename, data, SKILLS_DIR, {item["name"] for item in load_skills()})
            self.skills = load_skills()
        return skill

    async def install_skill_command(self, command: str) -> dict:
        if self._skill_install_lock.locked():
            raise ValueError("已有技能正在安装，请稍后再试")
        async with self._skill_install_lock:
            before = {item["name"] for item in load_skills()}
            self.skill_install_state = {"running": True, "output": "", "error": ""}

            def append_output(text):
                self.skill_install_state["output"] = (self.skill_install_state["output"] + text)[-MAX_OUTPUT_CHARS:]

            try:
                code = await run_skill_command(command, BASE_DIR, append_output)
                if code != 0:
                    raise ValueError(f"安装命令失败（退出码 {code}），请查看安装输出")
                self.skills = load_skills()
                return {"installed": [item["name"] for item in self.skills if item["name"] not in before],
                        "output": self.skill_install_state["output"]}
            except ValueError as e:
                self.skill_install_state["error"] = str(e)
                raise
            finally:
                self.skill_install_state["running"] = False

    async def install_mcp_package(self, name: str) -> str:
        """给缺 Python 包连不上的 MCP server 装包，装完重启 ChatBot 重连，返回装的包名。

        用 server 配置里的那个解释器跑 pip，包才装进它启动时用的环境；解释器没带 pip
        （uv 建的 venv 默认就没有）就退回 `uv pip install --python 该解释器` 装进同一环境。
        不合法/装失败抛 ValueError 给页面。
        """
        if self.installing:
            raise ValueError(f"正在给「{self.installing}」装依赖，等它装完")
        spec = self.config["mcp"].get("mcpServers", {}).get(name)
        if spec is None:
            raise ValueError(f"配置里没有叫「{name}」的 MCP server")
        state = next((row for row in get_mcp_server_status() if row["name"] == name), {})
        missing = missing_python_module(spec, state.get("error", ""))
        if missing is None:
            raise ValueError(f"「{name}」连不上不是因为缺 Python 包，没法一键安装")
        interpreter, module = missing
        package = module.replace("_", "-")

        self.installing = name
        try:
            logger.info("pip install %s（%s）", package, interpreter)
            code, output = await run_install_command([interpreter, "-m", "pip", "install", package])
            if code != 0 and "No module named pip" in output:
                uv = shutil.which("uv")
                if uv is None:
                    raise ValueError(f"{interpreter} 没带 pip，也找不到 uv；手动装：uv pip install {package}")
                logger.info("解释器没带 pip，改用 uv 装 %s", package)
                code, output = await run_install_command([uv, "pip", "install", "--python", interpreter, package])
            if code != 0:
                tail = "\n".join(output.strip().splitlines()[-3:])
                raise ValueError(f"安装 {package} 失败：{tail}")
        finally:
            self.installing = ""

        logger.info("已安装 %s，重启 ChatBot 重连「%s」", package, name)
        await self.restart()  # _start_bot 会让工具列表重建，连不上的 server 这次会重试
        return package

    async def restart(self, config: dict | None = None) -> None:
        async with self._lock:
            self.is_restarting = True
            try:
                await self._stop_bot()
                if config is not None:
                    self._sync_mcp_registry(config)  # 必须在 self.config 被换掉之前，要拿旧配置做差异
                    self.config = config
                await self._start_bot()
                if self.chat_bot is not None:
                    self._open_input_stream()
                else:
                    self._close_input_stream()  # 启动失败时保留具体原因，不再用麦克风报错覆盖它。
            finally:
                self.is_restarting = False

    # ---------------- ChatBot 生命周期 ----------------

    async def _start_bot(self) -> None:
        """起不来就把原因记进 last_error（chat_bot 保持 None），页面还能接着改配置重试。"""
        config = self.config
        self.last_error = tts_configuration_error(config["tts_engine"])
        if self.last_error:
            logger.error(self.last_error)
            return
        # 长期记忆接在人设后面烘进系统提示词；读不出来只记日志，这轮会话照常开始。
        try:
            memory = await asyncio.to_thread(diary.load_memory, SESSION_DB_PATH)
        except (OSError, SQLAlchemyError):
            logger.exception("读取长期记忆失败，本轮会话不带记忆")
            memory = ""
        # 技能是建 LlmAgent 时扫 SKILLS_DIR 烘进系统提示词的，这里同步扫一份给页面渲染开关；
        # 停用的不传给 AgentInfo.skills，模型就看不到它
        self.skills = load_skills()
        enabled_skills = [skill["name"] for skill in self.skills if config["skills"].get(skill["name"], True)]
        # SDK 自带工具的开关：停用的工具作为 --disable 传给 aichat-tools server，不对模型提供
        register_default_tools_server(disabled_tools=sorted(disabled_aichat_tools(config)))
        try:
            chat_bot = ChatBot(
                voiceprints=self.voiceprints,
                tts_engine=config["tts_engine"],
                output_sample_rate=OUTPUT_SAMPLE_RATE,
                send_idle_farewell=config["send_idle_farewell"],
                user_info=DeviceInfo(timezone=config["timezone"], city=config["city"]),
                agent_info=AgentInfo(
                    soul=diary.memory_soul(config["soul"], memory),
                    mcp=effective_mcp(config["mcp"]),
                    skills=enabled_skills,
                    tool_thinking=config["tool_thinking"],
                ),
            )
            await chat_bot.initialize()
        except SystemExit as exc:  # SDK 的 exit(1) 不应退出配置台，也不应丢掉失败组件。
            self.last_error = startup_error(exc, config["tts_engine"])
            logger.error(self.last_error)
            return
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            logger.exception("启动 ChatBot 失败")
            return

        self.chat_bot = chat_bot
        self.is_session_closed = False
        self.is_playing = False
        self._session_started_at = datetime.datetime.now(datetime.timezone.utc)
        self._session_ended_at = None
        self._session_id = uuid.uuid4().hex
        self._messages_saved = False
        self._debug_log_saved = False
        self._play_task = asyncio.create_task(self.play_audio(chat_bot))

    def save_messages(self, chat_bot: ChatBot | None = None) -> str:
        """把这一轮非 system 消息存入 SQLite，返回会话 ID；未写入则返回空串。

        会话结束（收到 close）时存一次；没等到 close 就被重启/退出打断的，
        由 _stop_bot 补存。DEBUG=true 时同时保存完整消息的 JSONL 调试文件；
        两种存储分别记录成功状态，失败时可独立重试。
        """
        chat_bot = chat_bot or self.chat_bot
        if chat_bot is None or (self._messages_saved and (not DEBUG or self._debug_log_saved)):
            return ""
        messages = chat_bot.llm.messages
        message_count = sum(message.get("role") != "system" for message in messages)
        if not message_count:
            self._messages_saved = True
            self._debug_log_saved = True
            return ""

        if self._session_ended_at is None:
            self._session_ended_at = datetime.datetime.now(datetime.timezone.utc)
        started_at = self._session_started_at or self._session_ended_at
        saved_session_id = ""
        if not self._messages_saved:
            try:
                save_session(SESSION_DB_PATH, self._session_id, started_at, self._session_ended_at, messages)
            except (OSError, SQLAlchemyError, TypeError, ValueError):  # 存记录失败不该影响会话本身
                logger.exception("保存会话记录失败")
            else:
                self._messages_saved = True
                saved_session_id = self._session_id
                logger.info("[session] 会话记录已保存：%s，id=%s（%d 条 message）",
                            SESSION_DB_PATH, self._session_id, message_count)

        if DEBUG and not self._debug_log_saved:
            log_path = os.path.join(
                os.path.dirname(SESSION_DB_PATH), f"{started_at:%Y%m%d-%H%M%S}-{self._session_id}.jsonl",
            )
            try:
                save_debug_log(log_path, messages)
            except (OSError, TypeError, ValueError):
                logger.exception("保存会话调试日志失败")
            else:
                self._debug_log_saved = True
                logger.info("[session] 会话调试日志已保存：%s（%d 条 message）", log_path, len(messages))
        return saved_session_id

    def _sync_mcp_registry(self, new_config: dict) -> None:
        """只注销删掉的和改过的 server（stdio 子进程随之退出）。

        没动过的留着不碰：重建 LlmAgent 时 load_mcp_servers 照旧注册它们，
        _collect_tools 直接复用已连上的 client，不用重拉子进程。
        """
        old_servers = effective_mcp(self.config["mcp"])["mcpServers"]
        new_servers = effective_mcp(new_config["mcp"])["mcpServers"]
        for name, spec in old_servers.items():
            if new_servers.get(name) != spec:
                remove_mcp_server(name)
        # 自带工具的开关变了也要重开 aichat-tools（--disable 在启动命令里，复用旧连接供的还是旧工具集）
        if disabled_aichat_tools(self.config) != disabled_aichat_tools(new_config):
            remove_mcp_server(DEFAULT_TOOLS_SERVER)

    async def _stop_bot(self) -> None:
        task, chat_bot = self._play_task, self.chat_bot
        self._play_task, self.chat_bot = None, None
        for text_task in self._text_tasks:
            text_task.cancel()
        if self._text_tasks:
            await asyncio.gather(*self._text_tasks, return_exceptions=True)
        self._text_tasks.clear()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if chat_bot is not None:
            try:
                await chat_bot.close()
            finally:
                # 先停止 SDK 内仍在生成回复的任务，再保存最终消息。
                self.save_messages(chat_bot)

    # ---------------- AI 日记 ----------------

    async def update_diary(self) -> None:
        """整理一轮日记和记忆，结果写进 diary_state；失败只记录，不抛出。"""
        self.diary_state = {**self.diary_state, "running": True, "error": ""}
        result, error = {}, ""
        try:
            result = await diary.update(SESSION_DB_PATH, timezone=self.config["timezone"], soul=self.config["soul"])
        except ValueError as exc:  # 未配置模型、模型返回空内容等，原因可以直接给页面看
            error = str(exc)
            logger.warning("整理日记失败：%s", error)
        except Exception:  # 网络、鉴权、数据库等：原始异常只进日志，不在页面暴露
            error = "整理日记失败，详见服务日志"
            logger.exception("整理日记失败")
        finally:
            self.diary_state = {
                "running": False,
                "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "error": error,
                "dates": result.get("dates", []),
                **{key: result.get(key, 0) for key in ("sessions", "pending", "skipped")},
            }

    async def _diary_loop(self) -> None:
        """启动 DIARY_FIRST_DELAY 秒后整理第一轮，之后每 DIARY_INTERVAL 秒或页面点「立即整理」时一轮。

        只有这一个任务执行整理，轮次不会重叠；LLM 走异步请求，退出时取消任务即可。
        """
        delay = DIARY_FIRST_DELAY
        while True:
            try:
                await asyncio.wait_for(self._diary_wakeup.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self._diary_wakeup.clear()
            await self.update_diary()
            delay = DIARY_INTERVAL

    # ---------------- 定时任务 ----------------

    async def _schedule_loop(self) -> None:
        """睡到最近一次执行时间再执行到点的任务；任务有改动或要手动运行时立即重算。

        只有这一个任务执行定时任务，执行不会重叠；执行完马上重算下一次。
        """
        while True:
            self._schedule_wakeup.clear()
            delay = SCHEDULE_MAX_SLEEP
            try:
                now, tz = self._schedule_clock()
                due, wake_at = await asyncio.to_thread(schedules.collect_due, SESSION_DB_PATH, now=now, tz=tz)
                runs: list[tuple[dict, datetime.datetime | None]] = list(due)
                manual, self._manual_runs = self._manual_runs, []
                for task_id in manual:
                    task = await asyncio.to_thread(schedules.get_task, SESSION_DB_PATH, task_id, now=now, tz=tz)
                    if task is not None and all(task["id"] != item["id"] for item, _ in runs):
                        runs.append((task, None))  # None：手动运行，按实际运行时刻记录
                if self.schedule_state["error"]:
                    self.schedule_state = {**self.schedule_state, "error": ""}
                if runs:
                    await self._run_scheduled(runs)
                    continue
                if wake_at is not None:
                    left = (wake_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    delay = min(delay, max(0.0, left))
            except (OSError, SQLAlchemyError, ValueError, TypeError):
                logger.exception("定时任务调度失败")
                self.schedule_state = {**self.schedule_state, "error": "定时任务读取或保存失败，详见服务日志"}
            try:
                await asyncio.wait_for(self._schedule_wakeup.wait(), delay)
            except asyncio.TimeoutError:
                pass

    async def _wait_until_idle(self, timeout: float) -> None:
        """等当前这一轮对话说完（最多 timeout 秒），免得定时任务插在用户和助手中间。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (self.is_restarting or self.is_playing or self._text_tasks) and loop.time() < deadline:
            await asyncio.sleep(0.5)

    async def _run_scheduled(self, runs: list[tuple[dict, datetime.datetime | None]]) -> None:
        """让助手执行到点（或手动运行）的任务：同一时刻的合成一条消息，会话已结束就开一轮新会话。

        先记下执行时间再交给 ChatBot：记录写不进去就不执行，同一次不会重复播报。
        """
        await self._wait_until_idle(SCHEDULE_IDLE_WAIT)
        started = datetime.datetime.now(datetime.timezone.utc)
        marks = [(task["id"], occurrence or started) for task, occurrence in runs]
        await asyncio.to_thread(schedules.mark_runs, SESSION_DB_PATH, marks, status="done")
        names = "、".join(task["title"] or task["prompt"][:20] for task, _ in runs)
        error = ""
        try:
            if self.chat_bot is None or self.is_session_closed:
                await self.restart()  # 跟唤醒词一样开一轮新会话，说完用户还能接着回话
            chat_bot = self.chat_bot
            if chat_bot is None:
                error = self.last_error or "对话服务没有运行"
            else:
                self._start_text_task(chat_bot.send_text(
                    schedules.trigger_text([task for task, _ in runs]),
                    response_instructions=schedules.TRIGGER_INSTRUCTIONS,
                ))
                logger.info("[schedule] 执行定时任务：%s", names)
        except Exception:
            logger.exception("执行定时任务失败")
            error = "执行失败，详见服务日志"
        if error:
            logger.error("[schedule] 定时任务没能执行（%s）：%s", names, error)
            try:
                await asyncio.to_thread(schedules.mark_runs, SESSION_DB_PATH, marks, status="error", error=error)
            except (OSError, SQLAlchemyError):
                logger.exception("记录定时任务失败原因失败")
        self._bump_schedule_revision()

    # ---------------- 音频 ----------------

    async def play_audio(self, chat_bot: ChatBot):
        opus_decoder = chat_bot.create_opus_decoder()
        prime = True  # 每句开头先垫一段静音，见下面 audio 分支

        while True:
            item = await chat_bot.receive_item()
            if item.get("type") == "close":
                # 会话结束不退进程：页面上显示「会话已结束」，点重新开始再建一个
                self.save_messages(chat_bot)
                self.is_session_closed = True
                self.is_playing = False
                logger.info("[session] 会话结束，停止拾音；页面点「重新开始」再开一轮")
                return

            elif item.get("type") == "audio_finish":
                await asyncio.sleep(0.3)
                self.is_playing = False
                prime = True

            elif item.get("type") == "stt":
                self.is_playing = True

            elif item.get("type") == "audio":
                self.is_playing = True
                pcm = opus_decoder.decode(item["data"])
                audio_array = np.frombuffer(pcm, dtype=np.int16)
                if prime:
                    # 句首 TTS 网络流还没跑到实时速率，先写 200ms 静音把输出缓冲
                    # 灌到安全水位——供数毛刺落在静音里，而不是打碎前几个字
                    await asyncio.to_thread(
                        self._output_stream.write,
                        np.zeros(int(OUTPUT_SAMPLE_RATE * 0.2), dtype=np.int16))
                    prime = False
                # 写扬声器按实时速率阻塞，放线程里，不然一说话 web 就卡住不响应
                await asyncio.to_thread(self._output_stream.write, audio_array)

    def _open_input_stream(self) -> None:
        """按配置的设备开拾音流（换设备只能关了重开）；打不开就记进 last_error，页面还能再挑一个。"""
        self._close_input_stream()
        try:
            stream = sd.InputStream(
                device=resolve_mic(self.config["mic"]),
                callback=self.audio_callback,
                channels=1,
                samplerate=16000,
                blocksize=int(240 * 16000 / 1000),
            )
            stream.start()
        except Exception as e:
            self.mic_device = ""
            self.last_error = f"麦克风打不开：{type(e).__name__}: {e}"
            logger.exception("打开麦克风失败")
            return
        self._input_stream = stream
        self.mic_device = sd.query_devices(stream.device)["name"]

    def _close_input_stream(self) -> None:
        stream, self._input_stream = self._input_stream, None
        if stream is not None:
            stream.stop()
            stream.close()

    def audio_callback(self, indata: np.ndarray, frames: int, time, status) -> None:
        chat_bot = self.chat_bot
        # 会话结束后就别再拾音了：bot 还留着（页面要看 llm.messages），但 play_audio 已经退出，
        # 再往里送音频只会开新一轮、音频包全堆在队列里没人消费，表现就是"能说话但没声音"
        if chat_bot is None or self.is_playing or self.is_session_closed:
            return

        chat_bot.audio_callback(indata, frames, time, status)

    # ---------------- 入口 ----------------

    async def run(self):
        await set_default_volume()
        self._output_stream = sd.OutputStream(samplerate=OUTPUT_SAMPLE_RATE, channels=1, dtype=np.int16)
        self._output_stream.start()
        try:
            self._diary_task = asyncio.create_task(self._diary_loop())
            await self.restart()  # 拾音流在里面按配置开，换设备保存后一起重开
            # 首轮会话起来之后再调度：到点的任务要开新会话时，不会和这里的首次启动同时重启
            self._schedule_task = asyncio.create_task(self._schedule_loop())
            print(f"web 配置台: http://{WEB_HOST}:{WEB_PORT}    聆听中... (Ctrl+C 退出)")
            await serve(self, WEB_HOST, WEB_PORT)
        finally:
            for task in (self._diary_task, self._schedule_task):
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            async with self._lock:
                await self._stop_bot()
            self._close_input_stream()
            self._output_stream.stop()
            self._output_stream.close()


if __name__ == "__main__":
    aiclient = AIClient(load_config())
    asyncio.run(aiclient.run())
