"""SQLite、JSONL 调试日志与会话生命周期回归；不使用真实设备或外部服务。"""

from contextlib import closing
import copy
import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session

with patch("dotenv.load_dotenv"):
    import main

from session_store import ChatSession, _write_database, get_session, save_session


STARTED_AT = datetime.datetime(2026, 10, 18, 3, 4, 5, tzinfo=datetime.timezone.utc)


def make_bot(messages):
    return SimpleNamespace(
        llm=SimpleNamespace(messages=copy.deepcopy(messages)),
        initialize=AsyncMock(),
        close=AsyncMock(),
        create_opus_decoder=lambda: None,
        receive_item=AsyncMock(return_value={"type": "close"}),
    )


class SessionStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / "db.sqlite3"
        db_path = patch.object(main, "SESSION_DB_PATH", str(self.path))
        db_path.start()
        self.addCleanup(db_path.stop)
        debug = patch.object(main, "DEBUG", True)
        debug.start()
        self.addCleanup(debug.stop)
        self.messages = [
            {"role": "system", "content": "你是语音助手"},
            {"role": "user", "content": '{"text":"调低音量"}', "audio_url": "/audio/hello.wav"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "speaker_volume", "arguments": '{"action":"decrease"}',
                }},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "name": "speaker_volume", "content": '{"percent":70}'},
            {"role": "assistant", "content": "音量已调低。", "reasoning_content": "已确认工具结果",
             "usage": {"total_tokens": 42}, "time": "2026-10-18 11:04:06"},
        ]
        self.saved_messages = [
            {"role": "user", "content": "调低音量"},
            {"role": "assistant", "content": None},
            {"role": "tool", "content": '{"percent":70}'},
            {"role": "assistant", "content": "音量已调低。", "usage": {"total_tokens": 42}},
        ]
        self.client = main.AIClient(copy.deepcopy(main.DEFAULT_CONFIG))
        self.bot = make_bot(self.messages)
        self.client.chat_bot = self.bot
        self.client._session_started_at = STARTED_AT

    def query(self, sql, params=()):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql, params).fetchall()

    def stored_messages(self, session_id):
        messages = []
        for role, content, usage in self.query(
            "SELECT role, content, usage FROM messages WHERE session_id = ? ORDER BY position", (session_id,),
        ):
            message = {"role": role, "content": content}
            if usage is not None:
                message["usage"] = json.loads(usage)
            messages.append(message)
        return messages

    def debug_messages(self, session_id):
        [path] = self.path.parent.glob(f"*-{session_id}.jsonl")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    async def test_close_event_saves_non_system_messages_only_once(self):
        await self.client.play_audio(self.bot)
        self.assertTrue(self.client.is_session_closed)
        session_id = self.client._session_id
        self.assertEqual(self.stored_messages(session_id), self.saved_messages)
        self.assertEqual(self.debug_messages(session_id), self.messages)
        rows = self.query("SELECT position, role FROM messages ORDER BY position")
        self.assertEqual(rows, [(i, message["role"]) for i, message in enumerate(self.saved_messages)])
        self.assertEqual(self.bot.llm.messages, self.messages)
        [(started_at, ended_at)] = self.query("SELECT started_at, ended_at FROM sessions")
        self.assertEqual(started_at, STARTED_AT.isoformat())
        self.assertEqual(datetime.datetime.fromisoformat(ended_at).utcoffset(), datetime.timedelta(0))
        self.assertGreaterEqual(ended_at, started_at)
        with patch.object(main, "save_debug_log") as save_log:
            self.assertEqual(self.client.save_messages(), "")
            await self.client._stop_bot()
        save_log.assert_not_called()
        self.assertEqual(self.query("SELECT count(*) FROM sessions"), [(1,)])
        self.bot.close.assert_awaited_once()

    async def test_debug_disabled_saves_database_without_jsonl(self):
        with patch.object(main, "DEBUG", False):
            await self.client.play_audio(self.bot)
            await self.client._stop_bot()
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)
        self.assertEqual(list(self.path.parent.glob("*.jsonl")), [])

    async def test_restart_keeps_sessions_with_identical_start_time_separate(self):
        # 走真实 restart/_start_bot，替换 SDK 和音频入口，确保每次重建都重置会话 ID。
        self.client.chat_bot = None
        first, second = make_bot(self.messages), make_bot(self.messages[:2])
        with patch.object(main, "ChatBot", side_effect=[first, second]), \
                patch.object(main, "tts_configuration_error", return_value=""), \
                patch.object(main, "load_skills", return_value=[]), \
                patch.object(main, "register_default_tools_server"), \
                patch.object(self.client, "_open_input_stream"), \
                patch.object(self.client, "play_audio", new=AsyncMock()):
            await self.client.restart()
            first_id = self.client._session_id
            self.client._session_started_at = STARTED_AT
            await self.client.restart()
            second_id = self.client._session_id
            self.client._session_started_at = STARTED_AT
            await self.client._stop_bot()
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(self.query("SELECT started_at FROM sessions"), [(STARTED_AT.isoformat(),)] * 2)
        self.assertEqual(self.stored_messages(first_id), self.saved_messages)
        self.assertEqual(self.stored_messages(second_id), self.saved_messages[:1])
        self.assertEqual(self.debug_messages(first_id), self.messages)
        self.assertEqual(self.debug_messages(second_id), self.messages[:2])
        self.assertEqual(len(list(self.path.parent.glob("*.jsonl"))), 2)

    async def test_stop_saves_after_sdk_stops_generating_messages(self):
        self.bot.llm.messages = self.messages[:2]

        async def close():
            self.bot.llm.messages.append(self.messages[-1])

        self.bot.close.side_effect = close
        await self.client._stop_bot()
        self.assertIsNone(self.client.chat_bot)
        self.assertEqual(self.stored_messages(self.client._session_id), [self.saved_messages[0], self.saved_messages[-1]])
        self.assertEqual(self.debug_messages(self.client._session_id), [*self.messages[:2], self.messages[-1]])

    async def test_sdk_close_failure_still_saves_messages(self):
        self.bot.close.side_effect = RuntimeError("close failed")
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await self.client._stop_bot()
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)
        self.assertEqual(self.debug_messages(self.client._session_id), self.messages)

    async def test_no_bot_or_only_system_messages_creates_no_files(self):
        self.client.chat_bot = None
        self.assertEqual(self.client.save_messages(), "")
        for messages in ([], self.messages[:1], self.messages[:1] * 2):
            with self.subTest(messages=messages):
                self.client.chat_bot = make_bot(messages)
                self.client._messages_saved = False
                self.assertEqual(self.client.save_messages(), "")
                self.assertFalse(self.path.exists())
                self.assertFalse(self.path.parent.exists())

    async def test_store_filters_system_messages_anywhere_without_changing_input(self):
        messages = [*self.messages[:2], self.messages[0], *self.messages[2:], self.messages[0]]
        original = copy.deepcopy(messages)
        save_session(str(self.path), "filtered", STARTED_AT, STARTED_AT, messages)
        self.assertEqual(self.stored_messages("filtered"), self.saved_messages)
        self.assertEqual(self.query("SELECT position FROM messages ORDER BY position"), [(0,), (1,), (2,), (3,)])
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE role = 'system'"), [(0,)])
        self.assertEqual(messages, original)

    async def test_store_does_not_create_database_for_only_system_messages(self):
        for messages in ([], self.messages[:1], self.messages[:1] * 2):
            with self.subTest(messages=messages):
                save_session(str(self.path), "empty", STARTED_AT, STARTED_AT, messages)
                self.assertFalse(self.path.exists())

    async def test_single_user_message_is_saved(self):
        self.bot.llm.messages = [self.messages[1]]
        session_id = self.client.save_messages()
        self.assertTrue(session_id)
        self.assertEqual(self.stored_messages(session_id), self.saved_messages[:1])
        self.assertEqual(self.debug_messages(session_id), [self.messages[1]])

    async def test_debug_log_keeps_structured_fields_and_serializes_other_values(self):
        self.bot.llm.messages = [
            {"role": "system", "content": "你是语音助手"},
            {"role": "user", "content": [{"type": "text", "text": "你好\n世界"}], "time": STARTED_AT},
        ]
        original = copy.deepcopy(self.bot.llm.messages)
        session_id = self.client.save_messages()
        expected = copy.deepcopy(original)
        expected[1]["time"] = str(STARTED_AT)
        self.assertEqual(self.debug_messages(session_id), expected)
        self.assertEqual(self.bot.llm.messages, original)

    async def test_user_content_stores_text_and_preserves_metadata(self):
        content = '{"text": "查一下天气。", "time": "2026-10-18T18:43:21.916778+08:00", "round": 1, "speaker": "unknown"}'
        message = {"role": "user", "content": content}
        original = copy.deepcopy(message)
        save_session(str(self.path), "content", STARTED_AT, STARTED_AT, [message])
        self.assertEqual(self.query("SELECT role, content, usage FROM messages"), [("user", "查一下天气。", None)])
        self.assertEqual(self.query("SELECT time, speaker FROM messages"),
                         [("2026-10-18T18:43:21.916778+08:00", "unknown")])
        self.assertEqual(message, original)

    async def test_text_extraction_applies_only_to_user_json_objects_with_string_text(self):
        content = '{"text": "开灯", "round": 1}'
        cases = [
            ({"role": "user", "content": content}, "开灯"),
            ({"role": "user", "content": {"text": "  保留空白\n", "speaker": "张三"}}, "  保留空白\n"),
            ({"role": "user", "content": '{"text": "", "response_instructions": "继续"}'}, ""),
            ({"role": "user", "content": "普通文本"}, "普通文本"),
            ({"role": "user", "content": '{"text":'}, '{"text":'),
            ({"role": "user", "content": '{"text": null}'}, '{"text": null}'),
            ({"role": "user", "content": '{"text": 42}'}, '{"text": 42}'),
            ({"role": "user", "content": '{"round": 1}'}, '{"round": 1}'),
            ({"role": "assistant", "content": content}, content),
            ({"role": "tool", "content": content}, content),
        ]
        messages = [message for message, expected in cases]
        original = copy.deepcopy(messages)
        save_session(str(self.path), "contents", STARTED_AT, STARTED_AT, messages)
        self.assertEqual(self.query("SELECT content FROM messages ORDER BY position"),
                         [(expected,) for message, expected in cases])
        self.assertEqual(messages, original)

    async def test_message_time_preserves_source_and_handles_missing_or_structured_content(self):
        sent_at = "2026-10-18T13:09:44.584297+08:00"
        cases = [
            ({"content": "你好", "time": sent_at}, sent_at),
            ({"content": json.dumps({"text": "你好", "time": sent_at})}, sent_at),
            ({"content": {"text": "你好", "time": sent_at}}, sent_at),
            ({"content": {"time": sent_at}, "time": STARTED_AT}, STARTED_AT.isoformat()),
            ({"content": {"time": sent_at}, "time": "2026-10-18 11:04:06"}, "2026-10-18 11:04:06"),
            ({"content": {"time": sent_at}, "time": None}, sent_at),
            ({"content": {"time": sent_at}, "time": ""}, sent_at),
            ({"content": {"time": STARTED_AT}}, STARTED_AT.isoformat()),
            ({"content": None}, None),
            ({}, None),
            ({"content": ""}, None),
            ({"content": "普通文本"}, None),
            ({"content": '{"time":'}, None),
            ({"content": "null"}, None),
            ({"content": '[{"time": "2026-10-18"}]'}, None),
            ({"content": {"time": 123}, "time": False}, None),
            ({"content": {"time": ""}}, None),
        ]
        messages = [{"role": "user", **message} for message, expected in cases]
        original = copy.deepcopy(messages)
        save_session(str(self.path), "times", STARTED_AT, STARTED_AT, messages)
        self.assertEqual(self.query("SELECT time FROM messages ORDER BY position"),
                         [(expected,) for message, expected in cases])
        self.assertEqual(messages, original)

    async def test_null_missing_empty_and_structured_content(self):
        structured = [{"type": "text", "text": "你好"}]
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1"}]},
            {"role": "assistant"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": structured},
        ]
        save_session(str(self.path), "content", STARTED_AT, STARTED_AT, messages)
        rows = self.query("SELECT content FROM messages ORDER BY position")
        self.assertEqual(rows[:3], [(None,), (None,), ("",)])
        self.assertEqual(json.loads(rows[3][0]), structured)

    async def test_speaker_is_stored_separately_with_content_fallback(self):
        content = '{"text": "开灯", "speaker": "张三"}'
        cases = [
            ({"content": content}, "张三"),
            ({"content": {"text": "关灯", "speaker": "speaker_2"}}, "speaker_2"),
            ({"content": content, "speaker": "李四"}, "李四"),
            ({"content": content, "speaker": None}, "张三"),
            ({"content": content, "speaker": ""}, "张三"),
            ({"content": "普通文本", "speaker": "unknown"}, "unknown"),
            ({"content": "普通文本"}, None),
            ({"content": None}, None),
            ({}, None),
            ({"content": '{"speaker":'}, None),
            ({"content": '[{"speaker": "张三"}]'}, None),
            ({"content": {"speaker": 123}, "speaker": False}, None),
        ]
        messages = [{"role": "user", **message} for message, expected in cases]
        original = copy.deepcopy(messages)
        save_session(str(self.path), "speakers", STARTED_AT, STARTED_AT, messages)
        self.assertEqual(self.query("SELECT speaker FROM messages ORDER BY position"),
                         [(expected,) for message, expected in cases])
        self.assertEqual(self.query("SELECT content FROM messages WHERE position = 0"), [("开灯",)])
        self.assertEqual(messages, original)

    async def test_usage_is_stored_separately_from_content(self):
        session_id = self.client.save_messages()
        columns = [column[1] for column in self.query("PRAGMA table_info(messages)")]
        self.assertIn("content", columns)
        self.assertIn("usage", columns)
        self.assertNotIn("message_json", columns)
        rows = self.query("SELECT content, usage FROM messages WHERE session_id = ? ORDER BY position", (session_id,))
        expected_contents = [message["content"] for message in self.saved_messages]
        self.assertEqual([content for content, usage in rows], expected_contents)
        self.assertEqual([json.loads(usage) if usage is not None else None for content, usage in rows],
                         [None, None, None, {"total_tokens": 42}])
        self.assertEqual(self.bot.llm.messages, self.messages)

    async def test_missing_null_and_empty_usage(self):
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好", "usage": None},
            {"role": "assistant", "content": "空用量", "usage": {}},
            {"role": "assistant", "content": "零用量", "usage": {"total_tokens": 0}},
        ]
        save_session(str(self.path), "usage", STARTED_AT, STARTED_AT, messages)
        rows = self.query("SELECT content, usage FROM messages ORDER BY position")
        self.assertEqual([content for content, usage in rows], ["你好", "你好", "空用量", "零用量"])
        self.assertEqual([json.loads(usage) if usage is not None else None for content, usage in rows],
                         [None, None, {}, {"total_tokens": 0}])

    async def test_tool_metadata_is_saved_with_calls_and_results_without_changing_content(self):
        self.bot.llm.messages[3]["elapsed_time"] = "0.042s"
        original = copy.deepcopy(self.bot.llm.messages)
        session_id = self.client.save_messages()
        rows = self.query(
            "SELECT position, tool_calls, tool_call_id, name, elapsed_time "
            "FROM messages WHERE session_id = ? ORDER BY position", (session_id,),
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], (0, None, None, None, None))
        self.assertEqual(rows[1][0], 1)  # system 过滤后的位置。
        self.assertEqual(json.loads(rows[1][1]), original[2]["tool_calls"])
        self.assertEqual(rows[1][2:], (None, None, None))
        self.assertEqual(rows[2], (2, None, "call_1", "speaker_volume", "0.042s"))
        self.assertEqual(rows[3], (3, None, None, None, None))
        self.assertEqual(self.stored_messages(session_id), self.saved_messages)
        self.assertEqual(self.bot.llm.messages, original)

    async def test_tool_metadata_does_not_depend_on_debug_logging(self):
        self.bot.llm.messages[3]["elapsed_time"] = "0.000s"
        with patch.object(main, "DEBUG", False):
            session_id = self.client.save_messages()
        record = get_session(str(self.path), session_id)
        self.assertEqual(record["messages"][1]["tool_calls"], self.messages[2]["tool_calls"])
        self.assertEqual(record["messages"][2]["elapsed_time"], "0.000s")
        self.assertEqual(list(self.path.parent.glob("*.jsonl")), [])

    async def test_deleting_a_session_also_deletes_its_messages_with_tool_fields(self):
        session_id = self.client.save_messages()
        with _write_database(str(self.path)) as session:
            session.delete(session.get(ChatSession, session_id))
        self.assertEqual(self.query("SELECT count(*) FROM messages"), [(0,)])

    async def test_metadata_write_failure_rolls_back_the_entire_session(self):
        from sqlalchemy.exc import StatementError

        messages = [{"role": "tool", "content": "结果", "elapsed_time": {"invalid": "not a string"}}]
        with self.assertRaises(StatementError):
            save_session(str(self.path), "failed", STARTED_AT, STARTED_AT, messages)
        self.assertEqual(self.query("SELECT count(*) FROM sessions"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM messages"), [(0,)])

    async def test_directory_failure_does_not_interrupt_close_and_retries_on_stop(self):
        self.path.parent.write_text("blocks mkdir", encoding="utf-8")
        with self.assertLogs("web_console", level="ERROR"):
            await self.client.play_audio(self.bot)
        self.assertTrue(self.client.is_session_closed)
        self.assertFalse(self.client._messages_saved)
        ended_at = self.client._session_ended_at
        self.path.parent.unlink()
        await self.client._stop_bot()
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)
        self.assertEqual(self.query("SELECT ended_at FROM sessions"), [(ended_at.isoformat(),)])
        self.assertEqual(self.debug_messages(self.client._session_id), self.messages)

    async def test_debug_log_failure_does_not_interrupt_close_and_retries_without_duplicate_db_rows(self):
        with patch.object(main, "save_debug_log", side_effect=OSError("write failed")), \
                self.assertLogs("web_console", level="ERROR"):
            await self.client.play_audio(self.bot)
        self.assertTrue(self.client.is_session_closed)
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)
        self.assertEqual(list(self.path.parent.glob("*.jsonl")), [])
        await self.client._stop_bot()
        self.assertEqual(self.debug_messages(self.client._session_id), self.messages)
        self.assertEqual(self.query("SELECT count(*) FROM sessions"), [(1,)])

    async def test_sqlite_failure_is_logged_and_can_be_retried(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not a sqlite database")
        with self.assertLogs("web_console", level="ERROR"):
            self.assertEqual(self.client.save_messages(), "")
        self.assertFalse(self.client._messages_saved)
        self.assertEqual(self.debug_messages(self.client._session_id), self.messages)
        self.path.unlink()
        with patch.object(main, "save_debug_log") as save_log:
            self.assertTrue(self.client.save_messages())
        save_log.assert_not_called()
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)

    async def test_failed_message_insert_rolls_back_whole_session(self):
        save_session(str(self.path), "existing", STARTED_AT, STARTED_AT, self.messages[1:2])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                CREATE TRIGGER reject_message BEFORE INSERT ON messages
                WHEN NEW.position = 2 BEGIN
                    SELECT RAISE(ABORT, 'simulated write failure');
                END
            """)
        with self.assertLogs("web_console", level="ERROR"):
            self.assertEqual(self.client.save_messages(), "")
        self.assertEqual(self.query("SELECT id FROM sessions"), [("existing",)])
        self.assertEqual(self.query("SELECT count(*) FROM messages"), [(1,)])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER reject_message")
        self.assertTrue(self.client.save_messages())
        self.assertEqual(self.stored_messages(self.client._session_id), self.saved_messages)
        self.assertEqual(self.stored_messages("existing"), self.saved_messages[:1])

    async def test_orm_loads_session_and_messages_in_order(self):
        session_id = self.client.save_messages()
        engine = create_engine(URL.create("sqlite+pysqlite", database=str(self.path)))
        try:
            with Session(engine) as session:
                record = session.get(ChatSession, session_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.started_at, STARTED_AT.isoformat())
                self.assertEqual([message.position for message in record.messages], list(range(len(self.saved_messages))))
                self.assertEqual([message.content for message in record.messages],
                                 [message["content"] for message in self.saved_messages])
                self.assertEqual([message.usage for message in record.messages],
                                 [None, None, None, {"total_tokens": 42}])
                self.assertEqual([message.time for message in record.messages],
                                 [None, None, None, "2026-10-18 11:04:06"])
                self.assertTrue(all(message.session is record for message in record.messages))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
