"""/api/state 与 /api/events 的分工回归：不启动 ChatBot，用假 client 只跑 web 层。"""

import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

import server
from server import create_app

# TestClient 会把响应收完才返回，SSE 是条不会结束的流，只能直接按 ASGI 调
EVENTS_SCOPE = {
    "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
    "method": "GET", "path": "/api/events", "query_string": b"", "root_path": "",
    "headers": [(b"host", b"testserver")],
}


class FakeClient:
    """只实现 web 层用到的那几个方法；messages() 跟 AIClient 一样返回会话里那个 list 本身。"""

    def __init__(self):
        self.msgs = [{"role": "system", "content": "很长的系统提示词"}]
        self.round = 0

    def live_status(self) -> dict:
        return {"running": True, "round": self.round}

    def status(self) -> dict:
        return {**self.live_status(), "tts_engine": "bytedancev1", "mcp": [], "skills": [], "skills_dir": "/skills"}

    def messages(self) -> list[dict]:
        return self.msgs


class StateEventsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        interval = patch.object(server, "EVENT_INTERVAL", 0.01)  # 别让测试等在 0.5 秒的轮子上
        interval.start()
        self.addCleanup(interval.stop)

    async def frames(self, count: int, act=None) -> list[dict]:
        """收 count 帧就断开；act 在第一帧之后调用，用来制造下一帧。"""
        collected: list[dict] = []
        closed = asyncio.Event()

        async def receive() -> dict:
            await closed.wait()  # 页面关掉，服务端那个 while 才收得住
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] != "http.response.body":
                return
            for line in message["body"].decode().splitlines():
                if line.startswith("data: "):
                    collected.append(json.loads(line[len("data: "):]))
            if len(collected) == 1 and act:
                act()
            if len(collected) >= count:
                closed.set()

        await asyncio.wait_for(create_app(self.client)(EVENTS_SCOPE, receive, send), timeout=5)
        self.assertEqual(len(collected), count)
        return collected

    async def test_state_is_full_and_events_carry_only_live_status(self):
        with TestClient(create_app(self.client)) as web:
            state = web.get("/api/state").json()
        self.assertEqual(state["status"], self.client.status())
        self.assertEqual(state["messages"], self.client.msgs)
        # 只在重启 / 保存配置后才变的那几项不该每帧重发
        frame = (await self.frames(1))[0]
        self.assertEqual(frame["status"], self.client.live_status())

    async def test_first_frame_carries_all_messages_then_only_new_ones(self):
        first, second = await self.frames(2, act=lambda: self.client.msgs.append({"role": "user", "content": "你好"}))
        self.assertEqual((first["start"], first["messages"]), (0, self.client.msgs[:1]))
        self.assertEqual((second["start"], second["messages"]), (1, self.client.msgs[1:]))

    async def test_new_session_resends_everything_from_zero(self):
        def restart():
            self.client.msgs = [{"role": "system", "content": "新会话"}]  # 重启换了 ChatBot，messages 是新 list
            self.client.round = 1

        first, second = await self.frames(2, act=restart)
        self.assertEqual(first["start"], 0)
        self.assertEqual((second["start"], second["messages"]), (0, self.client.msgs))
        self.assertEqual(second["status"]["round"], 1)

    async def test_queue_changes_are_pushed_without_new_chat_messages(self):
        conversation = {"active": None, "pending": [], "paused": False, "error": ""}
        self.client.live_status = lambda: {"running": True, "conversation": {
            **conversation, "pending": list(conversation["pending"]),
        }}

        def enqueue():
            conversation["pending"] = [{"id": "message-1", "text": "等一会发", "source": "text"}]
            conversation["paused"] = True

        first, second = await self.frames(2, act=enqueue)
        self.assertEqual(first["status"]["conversation"]["pending"], [])
        self.assertEqual(second["messages"], [])
        self.assertEqual(second["status"]["conversation"]["pending"][0]["id"], "message-1")
        self.assertTrue(second["status"]["conversation"]["paused"])


if __name__ == "__main__":
    unittest.main()
