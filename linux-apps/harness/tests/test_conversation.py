"""消息排队、停止和播放完成确认；使用模拟 SDK/音频设备，不访问外部服务。"""

import asyncio
import copy
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

with patch("dotenv.load_dotenv"):
    import main

from conversation import ConversationQueue, HarnessChatBot, MAX_PENDING, Message
from server import create_app
from voiceprint import VoiceprintChatBot


class QueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.started = asyncio.Queue()
        self.release = asyncio.Queue()
        self.results = []

        async def execute(message):
            self.started.put_nowait(message.text)
            await self.release.get()
            if message.text == "失败":
                raise RuntimeError("网络断线")

        async def finish(message, status, error):
            self.results.append((message.text, status, error))

        self.queue = ConversationQueue(execute, finish, Mock())
        self.addAsyncCleanup(self.queue.reset)

    async def next_started(self):
        return await asyncio.wait_for(self.started.get(), 2)

    async def test_fifo_and_failure_does_not_block_next_message(self):
        for text in ("第一句", "失败", "第三句"):
            self.queue.enqueue(Message(text))
        self.assertEqual(await self.next_started(), "第一句")
        self.assertEqual([m["text"] for m in self.queue.status()["pending"]], ["失败", "第三句"])
        self.release.put_nowait(None)
        self.assertEqual(await self.next_started(), "失败")
        with self.assertLogs("web_console", level="ERROR"):
            self.release.put_nowait(None)
            self.assertEqual(await self.next_started(), "第三句")
        self.release.put_nowait(None)
        await self.queue.worker
        self.assertEqual([item[:2] for item in self.results], [("第一句", "done"), ("失败", "error"), ("第三句", "done")])

    async def test_stop_preserves_pending_until_resume(self):
        self.queue.enqueue(Message("停止我"))
        self.queue.enqueue(Message("保留我"))
        await self.next_started()
        await self.queue.stop()
        self.assertEqual(self.results, [("停止我", "cancelled", "执行已停止")])
        self.assertTrue(self.queue.paused)
        self.assertEqual([m.text for m in self.queue.pending], ["保留我"])
        self.assertIsNone(self.queue.worker)
        self.queue.resume()
        self.assertEqual(await self.next_started(), "保留我")
        self.release.put_nowait(None)
        await self.queue.worker

    async def test_stop_before_worker_starts_can_be_resumed(self):
        self.queue.enqueue(Message("尚未开始"))
        await self.queue.stop()
        self.assertEqual(len(self.queue.pending), 1)
        self.queue.resume()
        self.assertEqual(await self.next_started(), "尚未开始")
        self.release.put_nowait(None)
        await self.queue.worker

    async def test_clear_cancels_pending_without_interrupting_active(self):
        self.queue.enqueue(Message("正在执行"))
        self.queue.enqueue(Message("尚未执行"))
        await self.next_started()
        await self.queue.clear()
        self.assertEqual(self.results, [("尚未执行", "cancelled", "待发消息已清空")])
        self.assertEqual(self.queue.active.text, "正在执行")
        self.release.put_nowait(None)
        await self.queue.worker

    async def test_queue_is_bounded_and_status_is_a_copy(self):
        self.queue.paused = True
        for _ in range(MAX_PENDING):
            self.queue.enqueue(Message("消息"))
        with self.assertRaisesRegex(ValueError, "最多"):
            self.queue.enqueue(Message("放不下"))
        state = self.queue.status()
        state["pending"][0]["text"] = "不能改队列"
        self.assertEqual(self.queue.pending[0].text, "消息")

    async def test_stop_waits_for_result_persistence(self):
        settling, persisted = asyncio.Event(), asyncio.Event()

        async def finish(*args):
            settling.set()
            await persisted.wait()

        self.queue.finish = finish
        self.queue.enqueue(Message("回复"))
        await self.next_started()
        self.release.put_nowait(None)
        await asyncio.wait_for(settling.wait(), 2)
        stopped = asyncio.create_task(self.queue.stop())
        await asyncio.sleep(0)
        self.assertFalse(stopped.done())
        persisted.set()
        await stopped
        self.assertIsNone(self.queue.active)


class ConversationHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = patch.object(main, "SESSION_DB_PATH", str(Path(temp.name) / "db.sqlite3"))
        db.start()
        self.addCleanup(db.stop)
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        self.started, self.release = asyncio.Queue(), asyncio.Queue()

        async def execute(text, **kwargs):
            self.started.put_nowait((text, kwargs))
            await self.release.get()

        self.bot = SimpleNamespace(
            execute_text=execute, abort=AsyncMock(), arm_idle_timer=Mock(),
            llm=SimpleNamespace(messages=[], round=0, llm_client=SimpleNamespace(
                compressor=SimpleNamespace(compressing=False, cursor=0, summary=None))),
        )
        self.client.chat_bot = self.bot
        self.addAsyncCleanup(self.client.conversation.reset)

        async def playback(bot):
            await asyncio.Event().wait()

        play = patch.object(self.client, "play_audio", new=playback)
        play.start()
        self.addCleanup(play.stop)

        async def close_playback():
            if self.client._play_task:
                self.client._play_task.cancel()
                await asyncio.gather(self.client._play_task, return_exceptions=True)

        self.addAsyncCleanup(close_playback)
        self.web = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.client)), base_url="http://test")
        self.addAsyncCleanup(self.web.aclose)

    async def test_send_stop_resume_clear_and_state(self):
        for text in ("第一句", "第二句"):
            response = await self.web.post("/api/send", json={"text": text})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["message"]["text"], text)
        self.assertEqual((await asyncio.wait_for(self.started.get(), 2))[0], "第一句")
        state = (await self.web.get("/api/state")).json()["status"]["conversation"]
        self.assertEqual((state["active"]["text"], state["pending"][0]["text"]), ("第一句", "第二句"))
        output = Mock()
        self.client._output_stream = output
        response = await self.web.post("/api/stop")
        state = response.json()["conversation"]
        self.assertTrue(state["paused"])
        self.assertIsNone(state["active"])
        self.assertEqual(state["pending"][0]["text"], "第二句")
        self.bot.abort.assert_awaited_once()
        output.abort.assert_called_once()
        output.start.assert_called_once()
        response = await self.web.post("/api/queue/resume")
        self.assertTrue(response.json()["ok"])
        self.assertEqual((await asyncio.wait_for(self.started.get(), 2))[0], "第二句")
        await self.web.post("/api/send", json={"text": "第三句"})
        response = await self.web.delete("/api/queue")
        self.assertEqual(response.json()["conversation"]["pending"], [])
        self.release.put_nowait(None)
        await self.client.conversation.worker
        self.assertEqual(self.client.conversation.error, "")

    async def test_bad_input_and_closed_session(self):
        for body in ([], {"text": None}, {"text": " "}, {"text": 42}):
            response = await self.web.post("/api/send", json=body)
            self.assertEqual(response.status_code, 400)
        self.client.is_session_closed = True
        self.assertEqual((await self.web.post("/api/send", json={"text": "你好"})).status_code, 400)

    async def test_late_voice_results_join_the_queue_with_speaker_and_audio(self):
        await self.client.send_text("网页消息")
        await asyncio.wait_for(self.started.get(), 2)
        await self.client._submit_sdk_text(self.bot, "语音消息", speaker="小明", audio_url="/audio/a.wav")
        await self.client._submit_sdk_text(object(), "旧会话语音", speaker="小红")
        self.assertEqual(len(self.client.conversation.pending), 1)
        self.release.put_nowait(None)
        text, kwargs = await asyncio.wait_for(self.started.get(), 2)
        self.assertEqual((text, kwargs), ("语音消息", {"speaker": "小明", "audio_url": "/audio/a.wav"}))
        self.release.put_nowait(None)
        await self.client.conversation.worker


class PlaybackFenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_allows_followup_after_an_interrupted_farewell(self):
        bot = object.__new__(HarnessChatBot)
        bot._is_session_closed = True
        with patch.object(VoiceprintChatBot, "abort", new=AsyncMock()) as abort:
            await bot.abort()
        abort.assert_awaited_once()
        self.assertFalse(bot._is_session_closed)

    async def test_stop_unblocks_the_output_write_before_restarting_stream(self):
        client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def write(data):
            started.set()
            release.wait(2)
            finished.set()

        def start():
            self.assertTrue(finished.is_set())

        client._output_stream = SimpleNamespace(write=write, abort=release.set, start=start)
        client._play_task = asyncio.create_task(client._write_audio(b"pcm"))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        with patch.object(client, "play_audio", new=AsyncMock()):
            await client._reset_playback(SimpleNamespace(abort=AsyncMock()))
        self.assertIsNone(client._output_write)

    async def test_sdk_return_waits_for_host_playback_including_close(self):
        for closes in (False, True):
            with self.subTest(closes=closes):
                client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
                bot = object.__new__(HarnessChatBot)
                bot._queue = asyncio.Queue()
                bot.create_opus_decoder = lambda: None
                bot.receive_item = bot._queue.get
                client.chat_bot = bot
                client.conversation.active = Message("正在回复")

                async def sdk_send(*args, **kwargs):
                    if closes:
                        await bot._queue.put({"type": "close"})

                with patch.object(VoiceprintChatBot, "send_text", new=sdk_send), \
                        patch.object(client, "save_messages"):
                    reply = asyncio.create_task(bot.execute_text("回复"))
                    await asyncio.sleep(0)
                    self.assertFalse(reply.done())
                    playback = asyncio.create_task(client.play_audio(bot))
                    try:
                        await asyncio.wait_for(reply, 2)
                        self.assertEqual(client.is_session_closed, closes)
                    finally:
                        playback.cancel()
                        await asyncio.gather(playback, return_exceptions=True)

    async def test_sdk_asr_send_is_dispatched_and_busy_turn_does_not_start_idle_timer(self):
        bot = object.__new__(HarnessChatBot)
        bot.submit_text = AsyncMock()
        bot.conversation_busy = lambda: True
        with patch.object(VoiceprintChatBot, "_start_idle_timer") as idle:
            bot._start_idle_timer()
            idle.assert_not_called()
        await bot.send_text("语音", speaker="小明", audio_url="/audio/a.wav")
        bot.submit_text.assert_awaited_once_with(bot, "语音", role="user", response_instructions=None,
                                               audio_url="/audio/a.wav", speaker="小明")
