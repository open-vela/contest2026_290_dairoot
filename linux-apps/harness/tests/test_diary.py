"""AI 日记与长期记忆回归：临时 SQLite + 模拟 LLM，不访问网络、音频或真实模型。"""

import asyncio
from contextlib import closing
import copy
import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import main

import diary
import schedules
from server import create_app
from session_store import _write_database, save_session

TZ8 = datetime.timezone(datetime.timedelta(hours=8))


def save(path, session_id, started, messages, minutes=5):
    started_at = datetime.datetime.fromisoformat(started)
    save_session(str(path), session_id, started_at, started_at + datetime.timedelta(minutes=minutes), messages)


def said(text, speaker="小明", time=None):
    message = {"role": "user", "content": text, "speaker": speaker}
    if time:
        message["time"] = time
    return message


def reply(text):
    return {"role": "assistant", "content": text}


def query(path, sql, params=()):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, params).fetchall()


def session_record(*messages, started="2026-10-23T06:00:00+00:00"):
    return {
        "id": "s", "started_at": started, "ended_at": started,
        "messages": [
            {"role": m.get("role"), "speaker": m.get("speaker"), "time": m.get("time"), "content": m.get("content")}
            for m in messages
        ],
    }


class FakeLlm:
    """按系统提示词区分日记 / 记忆调用；输出可以是文本、按输入生成文本的函数或要抛出的异常。"""

    def __init__(self, diary_text="今天和小明聊了早起的事。", memory_text="- 小明：每天七点起床"):
        self.outputs = {"diary": diary_text, "memory": memory_text}
        self.calls = []

    async def __call__(self, system, user):
        kind = "memory" if system == diary.MEMORY_PROMPT else "diary"
        self.calls.append((kind, system, user))
        output = self.outputs[kind]
        if callable(output):
            output = output(user)
        if isinstance(output, BaseException):
            raise output
        return output

    def inputs(self, kind):
        return [user for call_kind, _, user in self.calls if call_kind == kind]


class TranscriptTest(unittest.TestCase):
    def test_speaker_labels_and_skipped_messages(self):
        text = diary.render_session(session_record(
            {"role": "system", "content": "系统提示词"},
            {"role": "user", "content": "我叫小明\n住在深圳", "speaker": "小明"},
            {"role": "user", "content": "我是临时编号", "speaker": "speaker_2"},
            {"role": "user", "content": "听不清是谁", "speaker": "unknown"},
            {"role": "user", "content": "打字消息"},
            {"role": "user", "content": ""},
            {"role": "assistant", "content": None},
            {"role": "tool", "content": '{"temp": 20}'},
            {"role": "assistant", "content": "好的"},
        ), TZ8)
        self.assertEqual(text.splitlines(), [
            "【14:00 开始的对话】", "小明：我叫小明 住在深圳", "用户：我是临时编号", "用户：听不清是谁", "用户：打字消息", "助手：好的",
        ])

    def test_message_times_use_config_timezone(self):
        text = diary.render_session(session_record(
            {"role": "user", "content": "带偏移", "time": "2026-10-23T06:05:00+00:00"},
            {"role": "user", "content": "本地时间", "time": "2026-10-23 09:30:00"},
            {"role": "user", "content": "坏时间", "time": "昨天"},
        ), TZ8)
        self.assertEqual(text.splitlines()[1:], ["14:05 用户：带偏移", "09:30 用户：本地时间", "用户：坏时间"])

    def test_session_without_user_text_renders_empty(self):
        record = session_record({"role": "user", "content": ""}, {"role": "assistant", "content": "再见"})
        self.assertEqual(diary.render_session(record, TZ8), "")

    def test_schedule_triggers_are_labelled_and_not_user_speech(self):
        trigger = {"role": "user", "content": schedules.TRIGGER_PREFIX + "提醒大家喝水", "time": "2026-10-23T09:00:00+08:00"}
        # 只有提醒、没人回话的会话跟误唤醒一样不写日记
        self.assertEqual(diary.render_session(session_record(trigger, reply("该喝水啦")), TZ8), "")
        text = diary.render_session(session_record(trigger, reply("该喝水啦"), said("好，马上喝")), TZ8)
        self.assertEqual(text.splitlines()[1:], ["09:00 定时任务：提醒大家喝水", "助手：该喝水啦", "小明：好，马上喝"])

    def test_long_message_and_session_are_truncated(self):
        text = diary.render_session(session_record({"role": "user", "content": "长" * 500}), TZ8)
        self.assertEqual(text.splitlines()[1], "用户：" + "长" * diary.MESSAGE_CHARS + "…")
        messages = [{"role": "user", "content": f"第{i}句" + "话" * 20} for i in range(30)]
        with patch.object(diary, "CHUNK_CHARS", 200):
            lines = diary.render_session(session_record(*messages), TZ8).splitlines()
        self.assertIn("……（中间省略）……", lines)
        self.assertTrue(lines[1].startswith("用户：第0句"))
        self.assertTrue(lines[-1].startswith("用户：第29句"))
        self.assertLess(sum(len(line) + 1 for line in lines), 300)


class UpdateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "db.sqlite3"
        self.llm = FakeLlm()

    async def run_update(self, timezone=8, soul="你是小智"):
        return await diary.update(str(self.path), timezone=timezone, soul=soul, llm=self.llm)

    def query(self, sql):
        return query(self.path, sql)

    def seed_two_days(self):
        # UTC+8 下一场在 10 月 23 日深夜，另一场在 24 日凌晨。
        save(self.path, "late", "2026-10-23T15:30:00+00:00",
             [said("明天早上七点叫我", time="2026-10-23T23:30:00+08:00"), reply("好的")])
        save(self.path, "early", "2026-10-23T16:30:00+00:00", [said("我对花生过敏", speaker="speaker_1"), reply("记住了")])

    async def test_missing_database_is_not_created(self):
        self.assertEqual(await self.run_update(), {"dates": [], "sessions": 0, "pending": 0, "skipped": 0})
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.llm.calls, [])

    async def test_first_run_writes_each_day_then_idles(self):
        self.seed_two_days()
        summary = await self.run_update()
        self.assertEqual(summary, {"dates": ["2026-10-23", "2026-10-24"], "sessions": 2, "pending": 0, "skipped": 0})
        self.assertEqual(self.query("SELECT date, content FROM diaries ORDER BY date"),
                         [("2026-10-23", "今天和小明聊了早起的事。"), ("2026-10-24", "今天和小明聊了早起的事。")])
        self.assertEqual(self.query("SELECT id, content, source FROM memory"), [(1, "- 小明：每天七点起床", "diary")])
        self.assertEqual(self.query("SELECT session_id, date FROM diary_sessions ORDER BY session_id"),
                         [("early", "2026-10-24"), ("late", "2026-10-23")])
        first_diary = self.llm.inputs("diary")[0]
        self.assertIn("日期：2026-10-23 星期五", first_diary)
        self.assertIn("23:30 小明：明天早上七点叫我", first_diary)
        self.assertIn("<soul>\n你是小智\n</soul>", self.llm.calls[0][1])
        self.assertIn("用户：我对花生过敏", self.llm.inputs("memory")[1])
        self.assertNotIn("speaker_1", self.llm.inputs("memory")[1])
        calls = len(self.llm.calls)
        self.assertEqual(await self.run_update(), {"dates": [], "sessions": 0, "pending": 0, "skipped": 0})
        self.assertEqual(len(self.llm.calls), calls)

    async def test_timezone_decides_the_day(self):
        self.seed_two_days()
        summary = await self.run_update(timezone=0)
        self.assertEqual(summary["dates"], ["2026-10-23"])
        self.assertEqual(len(self.llm.calls), 2)  # 同一天的两场会话合成一块

    async def test_new_session_same_day_updates_existing_diary(self):
        save(self.path, "first", "2026-10-24T01:00:00+00:00", [said("周末去爬山")])
        await self.run_update()
        self.llm.outputs.update(diary="补充后的日记", memory="- 小明：每天七点起床\n- 小明：10月25日去医院")
        save(self.path, "second", "2026-10-24T03:00:00+00:00", [said("明天要去医院")])
        self.assertEqual((await self.run_update())["dates"], ["2026-10-24"])
        self.assertIn("已有日记：\n今天和小明聊了早起的事。", self.llm.inputs("diary")[-1])
        self.assertIn("现有记忆：\n- 小明：每天七点起床", self.llm.inputs("memory")[-1])
        self.assertNotIn("周末去爬山", self.llm.inputs("diary")[-1])  # 已整理的会话不再发送
        self.assertEqual(self.query("SELECT content FROM diaries"), [("补充后的日记",)])
        self.assertEqual(self.query("SELECT content FROM memory"), [("- 小明：每天七点起床\n- 小明：10月25日去医院",)])

    async def test_old_database_gains_diary_tables(self):
        save(self.path, "old", "2026-10-24T01:00:00+00:00", [said("你好")])
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript("DROP TABLE diaries; DROP TABLE memory; DROP TABLE diary_sessions;")
        self.assertEqual(diary.load_memory(str(self.path)), "")
        self.assertEqual(diary.list_diaries(str(self.path), "2026-10")["diaries"], [])
        await self.run_update()
        self.assertEqual(self.query("SELECT session_id FROM diary_sessions"), [("old",)])

    async def test_error_keeps_committed_chunks_and_retries_the_rest(self):
        self.seed_two_days()

        def diary_output(user):
            if "2026-10-24" in user:
                raise RuntimeError("network down")
            return "第一天的日记"

        self.llm.outputs["diary"] = diary_output
        with self.assertRaisesRegex(RuntimeError, "network down"):
            await self.run_update()
        self.assertEqual(self.query("SELECT date FROM diaries"), [("2026-10-23",)])
        self.assertEqual(self.query("SELECT session_id FROM diary_sessions"), [("late",)])
        self.llm.outputs["diary"] = "第二天的日记"
        self.assertEqual((await self.run_update())["dates"], ["2026-10-24"])

    async def test_rejected_chunk_is_skipped_until_it_succeeds(self):
        self.seed_two_days()

        class Rejected(Exception):
            pass

        def diary_output(user):
            if "2026-10-23" in user:
                raise Rejected("content risk")
            return "第二天的日记"

        self.llm.outputs["diary"] = diary_output
        with patch.object(diary, "BadRequestError", Rejected), self.assertLogs("web_console", level="WARNING"):
            summary = await self.run_update()
        self.assertEqual(summary, {"dates": ["2026-10-24"], "sessions": 1, "pending": 1, "skipped": 1})
        self.assertEqual(self.query("SELECT session_id FROM diary_sessions"), [("early",)])
        self.llm.outputs["diary"] = "第一天的日记"
        self.assertEqual((await self.run_update())["dates"], ["2026-10-23"])

    async def test_memory_saved_from_page_during_the_call_wins(self):
        save(self.path, "s", "2026-10-24T01:00:00+00:00", [said("我喜欢猫")])

        def memory_output(user):
            diary.save_memory(str(self.path), "- 小红：喜欢狗")  # 页面在模型调用期间保存了记忆
            return "- 小明：喜欢猫"

        self.llm.outputs["memory"] = memory_output
        summary = await self.run_update()
        self.assertEqual((summary["sessions"], summary["pending"]), (0, 1))
        self.assertEqual(diary.get_memory(str(self.path))["content"], "- 小红：喜欢狗")
        self.assertEqual(self.query("SELECT count(*) FROM diaries"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM diary_sessions"), [(0,)])
        self.llm.outputs["memory"] = "- 小红：喜欢狗\n- 小明：喜欢猫"
        await self.run_update()
        self.assertIn("现有记忆：\n- 小红：喜欢狗", self.llm.inputs("memory")[-1])
        self.assertEqual(diary.load_memory(str(self.path)), "- 小红：喜欢狗\n- 小明：喜欢猫")

    async def test_sessions_marked_by_another_runner_stop_the_round(self):
        save(self.path, "s", "2026-10-24T01:00:00+00:00", [said("我喜欢猫")])

        def memory_output(user):
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute("INSERT INTO diary_sessions VALUES ('s', '2026-10-24')")
            return "- 小明：喜欢猫"

        self.llm.outputs["memory"] = memory_output
        summary = await self.run_update()
        self.assertEqual((summary["sessions"], summary["pending"]), (0, 1))
        self.assertEqual(self.query("SELECT count(*) FROM memory"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM diaries"), [(0,)])

    async def test_chunk_budget_splits_a_busy_day(self):
        save(self.path, "a", "2026-10-24T01:00:00+00:00", [said("早上好" * 10)])
        save(self.path, "b", "2026-10-24T02:00:00+00:00", [said("中午好" * 10)])
        self.llm.outputs["diary"] = lambda user: f"日记第{len(self.llm.inputs('diary'))}版"
        with patch.object(diary, "CHUNK_CHARS", 60):
            summary = await self.run_update()
        self.assertEqual(summary, {"dates": ["2026-10-24"], "sessions": 2, "pending": 0, "skipped": 0})
        self.assertEqual(len(self.llm.inputs("diary")), 2)
        self.assertIn("已有日记：\n日记第1版", self.llm.inputs("diary")[1])
        self.assertEqual(self.query("SELECT content FROM diaries"), [("日记第2版",)])

    async def test_round_limit_leaves_sessions_pending(self):
        self.seed_two_days()
        with patch.object(diary, "MAX_CHUNKS_PER_ROUND", 1):
            summary = await self.run_update()
        self.assertEqual(summary, {"dates": ["2026-10-23"], "sessions": 1, "pending": 1, "skipped": 0})
        self.assertEqual((await self.run_update())["dates"], ["2026-10-24"])

    async def test_sessions_without_user_text_are_marked_without_model(self):
        save(self.path, "wake", "2026-10-24T01:00:00+00:00", [{"role": "user", "content": ""}, reply("再见")])
        self.assertEqual(await self.run_update(), {"dates": [], "sessions": 1, "pending": 0, "skipped": 0})
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.query("SELECT session_id, date FROM diary_sessions"), [("wake", None)])
        self.assertEqual(diary.list_diaries(str(self.path), "2026-10")["diaries"], [])

    async def test_empty_memory_output_keeps_existing_memory(self):
        save(self.path, "a", "2026-10-24T01:00:00+00:00", [said("今天天气怎么样")])
        self.llm.outputs["memory"] = "无"
        await self.run_update()
        self.assertEqual(self.query("SELECT count(*) FROM memory"), [(0,)])
        saved = diary.save_memory(str(self.path), "- 小明：喜欢猫")
        save(self.path, "b", "2026-10-24T02:00:00+00:00", [said("讲个笑话")])
        self.llm.outputs["memory"] = "<think>没什么可记的</think>"
        with self.assertLogs("web_console", level="WARNING"):
            await self.run_update()
        self.assertEqual(diary.get_memory(str(self.path)), saved)

    async def test_output_cleanup_and_limits(self):
        save(self.path, "a", "2026-10-24T01:00:00+00:00", [said("你好")])
        long_memory = "\n".join(f"- 小明：事实{i:03d}" + "很" * 30 for i in range(100))
        self.llm.outputs.update(diary="<think>先想想</think>\n```text\n今天很开心。\n```",
                                memory=f"```\n{long_memory}\n```")
        with self.assertLogs("web_console", level="WARNING"):
            await self.run_update()
        self.assertEqual(self.query("SELECT content FROM diaries"), [("今天很开心。",)])
        memory = diary.load_memory(str(self.path))
        self.assertLessEqual(len(memory), diary.MEMORY_LIMIT)
        self.assertTrue(memory.startswith("- 小明：事实000"))
        self.assertTrue(all(line.endswith("很" * 30) for line in memory.splitlines()))  # 按整行截断

        save(self.path, "b", "2026-10-24T02:00:00+00:00", [said("再见")])
        self.llm.outputs["diary"] = "很长的一句话。" * 200
        await self.run_update()
        [(content,)] = self.query("SELECT content FROM diaries")
        self.assertLessEqual(len(content), diary.DIARY_LIMIT)
        self.assertTrue(content.endswith("。"))

        save(self.path, "c", "2026-10-24T03:00:00+00:00", [said("晚安")])
        self.llm.outputs["diary"] = "<think>只有推理</think>"
        with self.assertRaisesRegex(ValueError, "日记为空"):
            await self.run_update()


class ReadWriteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "db.sqlite3"

    def test_missing_database_reads_are_empty_without_files(self):
        path = str(self.path)
        self.assertEqual(diary.get_memory(path), {"content": "", "source": "", "updated_at": ""})
        self.assertEqual(diary.load_memory(path), "")
        self.assertEqual(diary.list_diaries(path, "2026-10"), {"month": "2026-10", "diaries": [], "first": "", "last": ""})
        self.assertFalse(self.path.parent.exists())

    def test_list_diaries_by_month_with_session_counts_and_range(self):
        for session_id in "abc":
            save(self.path, session_id, "2026-10-24T01:00:00+00:00", [said("你好")])
        with _write_database(str(self.path)) as session:
            session.add_all([diary.DiaryEntry(date=date, content=f"{date} 的日记", updated_at="t")
                             for date in ("2026-09-30", "2026-10-24", "2026-10-03", "2026-11-01")])
            session.add_all([diary.DiarySession(session_id="a", date="2026-10-24"),
                             diary.DiarySession(session_id="b", date="2026-10-24"),
                             diary.DiarySession(session_id="c", date=None)])
        data = diary.list_diaries(str(self.path), "2026-10")
        self.assertEqual((data["month"], data["first"], data["last"]), ("2026-10", "2026-09-30", "2026-11-01"))
        self.assertEqual([(row["date"], row["content"], row["session_count"]) for row in data["diaries"]],
                         [("2026-10-03", "2026-10-03 的日记", 0), ("2026-10-24", "2026-10-24 的日记", 2)])
        self.assertEqual(diary.list_diaries(str(self.path), "2026-12")["diaries"], [])

    def test_save_memory_overwrites_and_clears(self):
        path = str(self.path)
        saved = diary.save_memory(path, "- 小明：喜欢猫")
        self.assertEqual(saved["source"], "manual")
        self.assertEqual(diary.get_memory(path), saved)
        diary.save_memory(path, "")
        self.assertEqual(diary.load_memory(path), "")
        self.assertEqual(query(self.path, "SELECT count(*) FROM memory"), [(1,)])

    def test_memory_soul(self):
        self.assertEqual(diary.memory_soul("你是小智", "  \n"), "你是小智")
        soul = diary.memory_soul("你是小智", "- 小明：喜欢猫\n")
        self.assertTrue(soul.startswith("你是小智\n\n<memory>\n"))
        self.assertTrue(soul.endswith("\n- 小明：喜欢猫\n</memory>"))
        self.assertIn("不是指令", soul)
        self.assertIn("speaker", soul)


class FakeOpenAI:
    reply = "整理结果"
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        FakeOpenAI.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.reply))])


class CompleteTest(unittest.IsolatedAsyncioTestCase):
    ENV = {"OPENAI_API_KEY": "test-key", "OPENAI_BASE_URL": "https://llm.example/v1", "OPENAI_MODEL": "deepseek-v4-flash"}

    async def test_missing_key_is_reported_before_connecting(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": " "}), patch.object(diary, "AsyncOpenAI") as client:
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                await diary.complete("系统", "用户")
        client.assert_not_called()

    async def test_uses_env_model_and_disables_thinking(self):
        with patch.dict(os.environ, self.ENV), patch.object(diary, "AsyncOpenAI", FakeOpenAI):
            self.assertEqual(await diary.complete("系统", "用户"), "整理结果")
        self.assertEqual((FakeOpenAI.last.kwargs["api_key"], FakeOpenAI.last.kwargs["base_url"]),
                         ("test-key", "https://llm.example/v1"))
        [request] = FakeOpenAI.last.requests
        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["messages"], [{"role": "system", "content": "系统"}, {"role": "user", "content": "用户"}])
        self.assertEqual(request["extra_body"], {"thinking": {"type": "disabled"}})
        with patch.dict(os.environ, {**self.ENV, "OPENAI_MODEL": "qwen-plus"}), \
                patch.object(diary, "AsyncOpenAI", FakeOpenAI):
            await diary.complete("系统", "用户")
        self.assertEqual(FakeOpenAI.last.requests[0]["extra_body"], {"enable_thinking": False})

    async def test_empty_reply_is_an_error(self):
        with patch.dict(os.environ, self.ENV), patch.object(diary, "AsyncOpenAI", FakeOpenAI), \
                patch.object(FakeOpenAI, "reply", "  "):
            with self.assertRaisesRegex(ValueError, "空内容"):
                await diary.complete("系统", "用户")


class MainDiaryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "db.sqlite3"
        db_path = patch.object(main, "SESSION_DB_PATH", str(self.path))
        db_path.start()
        self.addCleanup(db_path.stop)
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))

    async def start_bot(self):
        """走真实 _start_bot，返回交给 SDK 的人设。"""
        bot = SimpleNamespace(llm=SimpleNamespace(messages=[]), initialize=AsyncMock(), close=AsyncMock())
        with patch.object(main, "ChatBot", return_value=bot) as constructor, \
                patch.object(main, "tts_configuration_error", return_value=""), \
                patch.object(main, "load_skills", return_value=[]), \
                patch.object(main, "register_default_tools_server"), \
                patch.object(self.client, "play_audio", new=AsyncMock()):
            await self.client._start_bot()
            self.assertIs(self.client.chat_bot, bot)
            await self.client._stop_bot()
        return constructor.call_args.kwargs["agent_info"]["soul"]

    async def test_memory_is_appended_to_soul_for_new_sessions(self):
        diary.save_memory(str(self.path), "- 小明：喜欢猫")
        soul = await self.start_bot()
        self.assertEqual(soul, diary.memory_soul(main.DEFAULT_CONFIG["soul"], "- 小明：喜欢猫"))
        self.assertEqual(self.client.config["soul"], main.DEFAULT_CONFIG["soul"])

    async def test_missing_database_keeps_plain_soul_without_files(self):
        self.assertEqual(await self.start_bot(), main.DEFAULT_CONFIG["soul"])
        self.assertFalse(self.path.parent.exists())

    async def test_unreadable_database_still_starts_the_session(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"not a database" * 100)
        with self.assertLogs("web_console", level="ERROR"):
            soul = await self.start_bot()
        self.assertEqual(soul, main.DEFAULT_CONFIG["soul"])

    async def test_update_diary_reports_results_and_hides_raw_errors(self):
        summary = {"dates": ["2026-10-24"], "sessions": 2, "pending": 1, "skipped": 0}
        with patch.object(diary, "update", AsyncMock(return_value=summary)) as update:
            await self.client.update_diary()
        update.assert_awaited_once_with(
            str(self.path), timezone=self.client.config["timezone"], soul=self.client.config["soul"],
        )
        state = self.client.diary_state
        self.assertEqual({key: state[key] for key in summary}, summary)
        self.assertEqual((state["running"], state["error"]), (False, ""))
        self.assertTrue(state["finished_at"])

        with patch.object(diary, "update", AsyncMock(side_effect=ValueError("未配置 OPENAI_API_KEY，无法整理日记"))), \
                self.assertLogs("web_console", level="WARNING"):
            await self.client.update_diary()
        self.assertEqual(self.client.diary_state["error"], "未配置 OPENAI_API_KEY，无法整理日记")
        self.assertEqual(self.client.diary_state["dates"], [])

        with patch.object(diary, "update", AsyncMock(side_effect=RuntimeError("401 bad key sk-secret"))), \
                self.assertLogs("web_console", level="ERROR"):
            await self.client.update_diary()
        self.assertEqual(self.client.diary_state["error"], "整理日记失败，详见服务日志")
        self.assertNotIn("sk-secret", json.dumps(self.client.live_status(), ensure_ascii=False))

    async def test_request_wakes_the_loop_unless_running(self):
        state = self.client.request_diary_update()
        self.assertFalse(state["running"])
        self.assertTrue(self.client._diary_wakeup.is_set())
        self.client.diary_state = {**self.client.diary_state, "running": True}
        with self.assertRaisesRegex(ValueError, "正在整理"):
            self.client.request_diary_update()

    async def test_loop_runs_after_first_delay_and_on_request(self):
        async def wait_for(count):
            for _ in range(200):
                if update.await_count >= count:
                    return
                await asyncio.sleep(0.01)

        with patch.object(main, "DIARY_FIRST_DELAY", 0.01), patch.object(main, "DIARY_INTERVAL", 3600), \
                patch.object(self.client, "update_diary", AsyncMock()) as update:
            task = asyncio.create_task(self.client._diary_loop())
            await wait_for(1)
            self.assertEqual(update.await_count, 1)
            self.client.request_diary_update()
            await wait_for(2)
            self.assertEqual(update.await_count, 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_live_status_carries_a_copy_of_the_diary_state(self):
        status = self.client.live_status()
        status["diary"]["running"] = True
        self.assertFalse(self.client.diary_state["running"])


class DiaryHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "db.sqlite3"
        db_path = patch.object(main, "SESSION_DB_PATH", str(self.path))
        db_path.start()
        self.addCleanup(db_path.stop)
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        self.web = TestClient(create_app(self.client))
        self.web.__enter__()
        self.addCleanup(self.web.__exit__, None, None, None)

    def test_diary_page_is_served_and_linked_from_console(self):
        response = self.web.get("/diary")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertIn("<title>AI 日记", response.text)
        self.assertIn('href="/diary"', self.web.get("/").text)

    def test_missing_database_returns_empty_without_files(self):
        response = self.web.get("/api/diaries", params={"month": "2026-10"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"ok": True, "month": "2026-10", "diaries": [], "first": "", "last": ""})
        response = self.web.get("/api/memory")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"ok": True, "memory": {"content": "", "source": "", "updated_at": ""}})
        self.assertIn("diary", self.web.get("/api/state").json()["status"])
        self.assertFalse(self.path.parent.exists())

    def test_diaries_are_queried_by_valid_month(self):
        save(self.path, "a", "2026-10-24T01:00:00+00:00", [said("你好")])
        with _write_database(str(self.path)) as session:
            session.add_all([diary.DiaryEntry(date=date, content=f"{date} 的日记", updated_at="t")
                             for date in ("2026-09-30", "2026-10-24")])
            session.add(diary.DiarySession(session_id="a", date="2026-10-24"))
        data = self.web.get("/api/diaries", params={"month": "2026-10"}).json()
        self.assertEqual(data, {
            "ok": True, "month": "2026-10", "first": "2026-09-30", "last": "2026-10-24",
            "diaries": [{"date": "2026-10-24", "content": "2026-10-24 的日记", "updated_at": "t", "session_count": 1}],
        })
        for month in (None, "", "2026-1", "2026-13", "2026-00", "202610", "2026-10-01", "2026_10"):
            with self.subTest(month=month):
                response = self.web.get("/api/diaries", params={} if month is None else {"month": month})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])

    def test_memory_put_trims_saves_and_clears(self):
        response = self.web.put("/api/memory", json={"content": "  - 小明：喜欢猫\r\n- 小红：喜欢狗  "})
        self.assertEqual(response.status_code, 200)
        memory = response.json()["memory"]
        self.assertEqual((memory["content"], memory["source"]), ("- 小明：喜欢猫\n- 小红：喜欢狗", "manual"))
        self.assertEqual(self.web.get("/api/memory").json()["memory"], memory)
        self.assertEqual(self.web.put("/api/memory", json={"content": "长" * diary.MEMORY_LIMIT}).status_code, 200)
        self.assertEqual(self.web.put("/api/memory", json={"content": ""}).json()["memory"]["content"], "")
        self.assertEqual(diary.load_memory(str(self.path)), "")

    def test_memory_put_rejects_invalid_bodies(self):
        for body in (["- 小明：喜欢猫"], {"content": None}, {"content": 1}, {"other": "x"},
                     {"content": "长" * (diary.MEMORY_LIMIT + 1)}):
            with self.subTest(body=body):
                response = self.web.put("/api/memory", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        response = self.web.put("/api/memory", content=b"{bad json", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path.exists())

    def test_unreadable_database_returns_503(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"not a database" * 100)
        with self.assertLogs("web_console", level="ERROR"):
            self.assertEqual(self.web.get("/api/diaries", params={"month": "2026-10"}).status_code, 503)
            self.assertEqual(self.web.get("/api/memory").status_code, 503)
            self.assertEqual(self.web.put("/api/memory", json={"content": "x"}).status_code, 503)

    def test_update_request_wakes_the_loop_or_reports_running(self):
        response = self.web.post("/api/diaries/update")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertFalse(response.json()["diary"]["running"])
        self.assertTrue(self.client._diary_wakeup.is_set())
        self.client.diary_state = {**self.client.diary_state, "running": True}
        response = self.web.post("/api/diaries/update")
        self.assertEqual(response.status_code, 400)
        self.assertIn("正在整理", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
