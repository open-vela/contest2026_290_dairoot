"""harness 本地声纹库与 ASR 接入；embedding 不放进对话消息或浏览器状态。"""

from collections import OrderedDict
import asyncio
from datetime import datetime, timezone
from inspect import signature
import json
import logging
from pathlib import Path

import numpy as np
from sqlalchemy import JSON, Text, URL, create_engine, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from aichat_sdk import ChatBot
from aichat_sdk.asr.asr_client import AsrWebSocketClient
from session_store import Base, _read_database

logger = logging.getLogger("web_console")
MIN_SPEECH_MS = 1000
MAX_SAMPLES = 10
MAX_UTTERANCES = 200
PROFILE_SYNC_TIMEOUT = 5


class Voiceprint(Base):
    __tablename__ = "voiceprints"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    embeddings: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[str] = mapped_column(Text)


def normalize(embedding) -> np.ndarray:
    try:
        vector = np.asarray(embedding, dtype=np.float64)
    except (TypeError, ValueError):
        raise ValueError("声纹 embedding 格式无效") from None
    if vector.ndim != 1 or not vector.size or not np.isfinite(vector).all():
        raise ValueError("声纹 embedding 必须是非空的有限数值向量")
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("声纹 embedding 不能为零向量")
    return vector / norm


class VoiceprintStore:
    def __init__(self, path: str):
        self.path = path
        self._profiles: dict[str, list[np.ndarray]] | None = None
        self.error = ""

    def _load(self):
        if self._profiles is not None:
            return
        profiles = {}
        try:
            with _read_database(self.path) as session:
                if session is not None:
                    for record in session.scalars(select(Voiceprint)):
                        vectors = [normalize(value) for value in record.embeddings]
                        if not vectors or len({v.size for v in vectors}) != 1:
                            raise ValueError("声纹样本维度不一致或为空")
                        profiles[record.name] = vectors
        except (OSError, SQLAlchemyError, ValueError, TypeError):
            self.error = "读取声纹数据库失败，请检查 data/db.sqlite3"
            logger.exception(self.error)
        self._profiles = profiles

    def summary(self) -> list[dict]:
        self._load()
        return [{"name": name, "samples": len(vectors)} for name, vectors in sorted(self._profiles.items())]

    def export_samples(self) -> list[dict]:
        self._load()
        if self.error:
            raise ValueError(self.error)
        return [
            {"name": name, "embedding": vector.tolist()}
            for name, vectors in sorted(self._profiles.items()) for vector in vectors
        ]

    def add(self, name: str, vector: np.ndarray):
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > 40
                or any(ord(char) < 32 for char in name.strip())):
            raise ValueError("姓名需为 1～40 个字符，不能包含换行等控制字符")
        name = name.strip()
        vector = normalize(vector)
        self._load()
        if self.error:
            raise ValueError(self.error)
        for other_name, vectors in self._profiles.items():
            for sample in vectors:
                if sample.size != vector.size:
                    raise ValueError("声纹模型维度已变化，请先删除旧声纹再录入")
                if np.allclose(sample, vector, atol=1e-6, rtol=0):
                    if other_name == name:
                        return name  # 同一句话重复点录入，不重复计数。
                    raise ValueError(f"这条声纹已录入为“{other_name}”")
        vectors = [*self._profiles.get(name, []), vector][-MAX_SAMPLES:]
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(URL.create("sqlite+pysqlite", database=self.path))
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session, session.begin():
                session.merge(Voiceprint(
                    name=name, embeddings=[sample.tolist() for sample in vectors],
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ))
        finally:
            engine.dispose()
        self._profiles[name] = vectors  # 事务成功后才更新内存。
        return name

    def remove(self, name: str):
        self._load()
        if self.error:
            raise ValueError(self.error)
        if name not in self._profiles:
            raise ValueError("声纹不存在")
        engine = create_engine(URL.create("sqlite+pysqlite", database=self.path))
        try:
            with Session(engine) as session, session.begin():
                session.execute(delete(Voiceprint).where(Voiceprint.name == name))
        finally:
            engine.dispose()
        del self._profiles[name]


class VoiceprintClient:
    """只暂存本轮最近 200 句的 embedding；点录入才写入本地数据库。"""

    def __init__(self, path: str):
        self.store = VoiceprintStore(path)
        self.utterances = OrderedDict()
        self.revision = 0
        self.changed = asyncio.Event()
        self.sync_state = {"connected": False, "pending": True, "error": ""}

    def profiles_changed(self):
        self.revision += 1
        self.sync_state["pending"] = True
        self.changed.set()
        self.changed = asyncio.Event()

    def capture(self, payload: dict):
        audio_url = payload.get("audio_url")
        if not isinstance(audio_url, str) or not audio_url:
            return
        vector, error = None, ""
        try:
            speech_ms = float(payload.get("speech_ms") or 0)
            if not np.isfinite(speech_ms) or speech_ms < MIN_SPEECH_MS:
                raise ValueError("需要至少 1 秒有效语音，请重新说一句长一些的话")
            vector = normalize(payload.get("embedding"))
        except (ValueError, TypeError):
            error = "语音不足 1 秒或无法提取声纹，请重新说一句长一些的话"
        self.utterances[audio_url] = {
            "vector": vector,
            "speaker": payload.get("speaker") if isinstance(payload.get("speaker"), str) else "",
            "enrolled_name": "", "error": error,
        }
        self.utterances.move_to_end(audio_url)
        while len(self.utterances) > MAX_UTTERANCES:
            self.utterances.popitem(last=False)

    def status(self) -> dict:
        return {
            "profiles": self.store.summary(), "error": self.store.error,
            "sync": dict(self.sync_state),
            "utterances": {
                url: {key: value for key, value in item.items() if key != "vector"}
                for url, item in self.utterances.items()
            },
        }

    def enroll(self, audio_url: str, name: str):
        item = self.utterances.get(audio_url)
        if item is None:
            raise ValueError("这条消息的声纹已失效，请在当前会话重新说一句话")
        if item["error"]:
            raise ValueError(item["error"])
        item["enrolled_name"] = self.store.add(name, item["vector"])
        self.profiles_changed()

    def remove(self, name: str):
        self.store.remove(name)
        self.profiles_changed()
        for item in self.utterances.values():
            if item["enrolled_name"] == name:
                item["enrolled_name"] = ""


class PresetAsrClient(AsrWebSocketClient):
    """沿用 SDK 0.1.2 的语音链路，在同一连接内同步本地预设样本。"""

    def __init__(self, url, voiceprints, **kwargs):
        super().__init__(url, **kwargs)
        self.voiceprints = voiceprints
        self._profile_ack = None
        self._sent_revision = -1

    async def _sync_profiles(self, websocket):
        revision = self.voiceprints.revision
        profiles = self.voiceprints.store.export_samples()
        self._profile_ack = asyncio.get_running_loop().create_future()
        try:
            await websocket.send(json.dumps({"type": "set_speaker_profiles", "profiles": profiles}, ensure_ascii=False))
            reply = await asyncio.wait_for(self._profile_ack, PROFILE_SYNC_TIMEOUT)
            if not reply.get("ok") or reply.get("samples") != len(profiles):
                raise ValueError("ASR 未确认完整的预设声纹，请更新 ASR 服务")
            self._sent_revision = revision
            self.voiceprints.sync_state.update(
                connected=True, pending=revision != self.voiceprints.revision, error="",
            )
        finally:
            if not self._profile_ack.done():
                self._profile_ack.cancel()
            self._profile_ack = None

    async def _watch_profiles(self, websocket):
        while True:
            changed = self.voiceprints.changed
            if self._sent_revision != self.voiceprints.revision:
                await self._sync_profiles(websocket)
            else:
                await changed.wait()

    async def _send_audio(self, websocket):
        tasks = set()
        self._sent_revision = -1
        self.voiceprints.sync_state.update(connected=True, pending=True)
        try:
            # 先确认预设声纹已生效，再让麦克风音频进入识别链路。
            await self._sync_profiles(websocket)
            tasks = {
                asyncio.create_task(super()._send_audio(websocket)),
                asyncio.create_task(self._watch_profiles(websocket)),
            }
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            self.voiceprints.sync_state["error"] = str(exc) or "ASR 声纹同步超时，请检查或更新 ASR 服务"
            raise
        finally:
            self.voiceprints.sync_state.update(connected=False, pending=True)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _receive_results(self, websocket):
        async def results():
            async for raw in websocket:
                try:
                    message = json.loads(raw) if isinstance(raw, str) else None
                except ValueError:
                    message = None
                if isinstance(message, dict):
                    ack = self._profile_ack
                    if message.get("type") == "speaker_profiles_set":
                        if ack is not None and not ack.done():
                            ack.set_result(message)
                        continue
                    if message.get("type") == "error" and message.get("code") in (
                        "invalid_speaker_profiles", "binary_audio_required",
                    ) and ack is not None and not ack.done():
                        reason = message.get("message", "ASR 拒绝预设声纹")
                        if message["code"] == "binary_audio_required":
                            reason = "ASR 服务尚不支持预设声纹，请更新 ASR 服务"
                        ack.set_exception(ValueError(reason))
                        continue
                yield raw

        await super()._receive_results(results())


class VoiceprintChatBot(ChatBot):
    """同步预设声纹，姓名匹配和 speaker 返回均由 ASR 服务完成。"""

    def __init__(self, *, voiceprints: VoiceprintClient, **kwargs):
        if "speaker" not in signature(ChatBot.send_text).parameters:
            raise RuntimeError("当前 aichat-sdk 不支持 speaker 参数，请执行 uv sync 安装 aichat-sdk>=0.1.4")
        super().__init__(**kwargs)
        self.voiceprints = voiceprints
        self.asr = PresetAsrClient(self.asr.url, voiceprints)
        self.voiceprints.utterances.clear()
        # 只保留供手动录入的 embedding，不在 harness 做姓名匹配。
        self.asr.on("stt")(voiceprints.capture)
