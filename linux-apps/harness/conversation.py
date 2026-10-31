"""单会话消息队列，以及等待宿主实际播完的 SDK 接口。"""

import asyncio
from dataclasses import dataclass, field
import logging
import uuid

from voiceprint import VoiceprintChatBot

logger = logging.getLogger("web_console")
MAX_PENDING = 50


@dataclass
class Message:
    text: str
    source: str = "text"
    kwargs: dict = field(default_factory=dict)
    runs: list = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def present(self):
        return {"id": self.id, "text": self.text, "source": self.source}


class ConversationQueue:
    """执行回调必须等生成和播放都结束；停止保留待发消息，显式继续后再执行。"""

    def __init__(self, execute, finish, idle):
        self.execute, self.finish, self.idle = execute, finish, idle
        self.pending: list[Message] = []
        self.active: Message | None = None
        self.paused = False
        self.error = ""
        self.worker: asyncio.Task | None = None

    def status(self):
        return {"active": self.active.present() if self.active else None,
                "pending": [item.present() for item in self.pending],
                "paused": self.paused, "error": self.error}

    def check_capacity(self):
        if len(self.pending) >= MAX_PENDING:
            raise ValueError(f"待发消息最多 {MAX_PENDING} 条，请等待或清空队列")

    def enqueue(self, message: Message):
        self.check_capacity()
        self.pending.append(message)
        self._start()
        return message.present()

    def _start(self):
        if self.pending and not self.paused and self.worker is None:
            self.worker = asyncio.create_task(self._drain())

    async def _settle(self, message, status, error):
        try:
            await self.finish(message, status, error)
        except Exception as exc:
            self.error = f"保存执行结果失败：{exc}"
            logger.exception("保存消息执行结果失败")

    async def _drain(self):
        try:
            while self.pending and not self.paused:
                message = self.pending.pop(0)
                self.active, self.error = message, ""
                status, error = "done", ""
                cancelled = False
                try:
                    await self.execute(message)
                except asyncio.CancelledError:
                    status, error, cancelled = "cancelled", "执行已停止", True
                except Exception as exc:
                    status, error = "error", str(exc) or type(exc).__name__
                    self.error = error
                    logger.exception("消息执行失败")
                finally:
                    # 停止等待结果落库后才返回，不能留下后台写入覆盖后续状态。
                    settle = asyncio.create_task(self._settle(message, status, error))
                    try:
                        await asyncio.shield(settle)
                    except asyncio.CancelledError:
                        await settle
                        cancelled = True
                    finally:
                        self.active = None
                if cancelled:
                    return
        finally:
            self.worker = None
            self._start()
            if not self.pending:
                self.idle()

    async def stop(self):
        self.paused = True
        worker = self.worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            if self.worker is worker:
                self.worker = None

    def resume(self):
        self.paused = False
        self._start()
        if not self.pending and self.active is None:
            self.idle()

    async def clear(self):
        pending, self.pending = self.pending, []
        for message in pending:
            await self._settle(message, "cancelled", "待发消息已清空")

    async def reset(self):
        await self.stop()
        await self.clear()
        self.paused, self.error = False, ""


class HarnessChatBot(VoiceprintChatBot):
    """ASR 和 SDK 的空闲告别都交给宿主排队；执行只由队列调用。"""

    def __init__(self, *, submit_text, conversation_busy, **kwargs):
        super().__init__(**kwargs)
        self.submit_text = submit_text
        self.conversation_busy = conversation_busy

    async def send_text(self, user_text, role="user", response_instructions=None,
                        audio_url=None, speaker=None):
        await self.submit_text(self, user_text, role=role, response_instructions=response_instructions,
                               audio_url=audio_url, speaker=speaker)

    async def execute_text(self, text, **kwargs):
        await super().send_text(text, **kwargs)
        # SDK 返回时音频可能仍堆在队列里。栅栏排在本轮所有音频之后，由播放循环确认。
        played = asyncio.get_running_loop().create_future()
        await self._queue.put({"type": "turn_finish", "played": played})
        await played

    async def abort(self):
        await super().abort()
        # 道别已生成、close 尚未播放时也可能被停止；下一句仍属于宿主保留的会话。
        self._is_session_closed = False

    def _start_idle_timer(self):
        if not self.conversation_busy():
            super()._start_idle_timer()

    def arm_idle_timer(self):
        self._start_idle_timer()
