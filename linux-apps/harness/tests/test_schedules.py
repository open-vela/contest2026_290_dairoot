"""定时任务回归：临时 SQLite + 模拟 ChatBot，不访问音频、网络或真实模型。

MCP 测试把 schedule_mcp 发往 harness 的请求转给 TestClient，走真实的接口、校验和数据库。
"""

import asyncio
from contextlib import closing
import copy
import datetime
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error

from fastmcp import Client
from starlette.testclient import TestClient

with patch("dotenv.load_dotenv"):
    import main

import schedules
from server import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import schedule_mcp

TZ8 = schedules.timezone_of(8)


def local(text: str) -> datetime.datetime:
    """东八区的本地时间。"""
    return datetime.datetime.fromisoformat(text).replace(tzinfo=TZ8)


MONDAY_8AM = local("2026-10-26T08:00:00")  # 2026-10-26 是星期一


def plan(repeat="daily", time="09:00", date="", weekdays=()):
    """next_run / summary 只看排期字段。"""
    return {"repeat": repeat, "time": time, "date": date, "weekdays": list(weekdays)}


def query(path, sql, params=()):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(sql, params).fetchall()


class TempDatabase:
    def make_path(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "data" / "db.sqlite3")

    def create(self, raw, now=MONDAY_8AM):
        return schedules.create_task(self.path, raw, now=now, tz=TZ8)

    def update(self, task_id, raw, now=MONDAY_8AM):
        return schedules.update_task(self.path, task_id, raw, now=now, tz=TZ8)

    def get(self, task_id, now=MONDAY_8AM):
        return schedules.get_task(self.path, task_id, now=now, tz=TZ8)

    def collect(self, now: str):
        return schedules.collect_due(self.path, now=local(now), tz=TZ8)


class NextRunTest(unittest.TestCase):
    def run_at(self, item, after):
        return schedules.next_run(item, local(after), TZ8)

    def test_daily_weekdays_and_weekly(self):
        self.assertEqual(self.run_at(plan(), "2026-10-26T08:00:00"), local("2026-10-26T09:00:00"))
        # 正好到点不算，往后找下一次
        self.assertEqual(self.run_at(plan(), "2026-10-26T09:00:00"), local("2026-10-27T09:00:00"))
        # 周五上午之后的下一个工作日是下周一
        self.assertEqual(self.run_at(plan("weekdays"), "2026-10-30T10:00:00"), local("2026-11-02T09:00:00"))
        weekly = plan("weekly", "20:00", weekdays=[1, 3])
        self.assertEqual(self.run_at(weekly, "2026-10-26T21:00:00"), local("2026-10-28T20:00:00"))
        self.assertEqual(self.run_at(weekly, "2026-10-28T20:00:00"), local("2026-11-02T20:00:00"))

    def test_hourly_and_once(self):
        hourly = plan("hourly", "00:15")
        self.assertEqual(self.run_at(hourly, "2026-10-26T08:00:00"), local("2026-10-26T08:15:00"))
        self.assertEqual(self.run_at(hourly, "2026-10-26T08:15:00"), local("2026-10-26T09:15:00"))
        self.assertEqual(self.run_at(hourly, "2026-10-26T23:59:59"), local("2026-10-27T00:15:00"))
        once = plan("once", "15:30:20", date="2026-10-26")
        self.assertEqual(self.run_at(once, "2026-10-26T08:00:00"), local("2026-10-26T15:30:20"))
        self.assertIsNone(self.run_at(once, "2026-10-26T15:30:20"))

    def test_fractional_timezone(self):
        run = schedules.next_run(plan(), datetime.datetime(2026, 10, 26, tzinfo=datetime.timezone.utc),
                                 schedules.timezone_of(5.5))
        self.assertEqual(run, datetime.datetime(2026, 10, 26, 3, 30, tzinfo=datetime.timezone.utc))
        self.assertEqual([schedules.timezone_label(offset) for offset in (8, 5.5, -3, 0)],
                         ["UTC+8", "UTC+5:30", "UTC-3", "UTC+0"])

    def test_summary_and_spoken_times(self):
        self.assertEqual(schedules.summary(plan()), "每天 09:00")
        self.assertEqual(schedules.summary(plan("weekdays", "08:30")), "工作日 08:30")
        self.assertEqual(schedules.summary(plan("weekly", "20:00", weekdays=[1, 3, 7])), "每周一、三、日 20:00")
        self.assertEqual(schedules.summary(plan("hourly", "00:15")), "每小时第 15 分")
        self.assertEqual(schedules.summary(plan("once", "15:30", date="2026-10-26")), "10月26日 15:30")

        def describe(text):
            return schedules.describe_time(local(text), MONDAY_8AM, TZ8)

        self.assertEqual(describe("2026-10-26T09:00:00"), "今天 09:00")
        self.assertEqual(describe("2026-10-27T07:30:00"), "明天 07:30")
        self.assertEqual(describe("2026-10-25T21:00:00"), "昨天 21:00")
        self.assertEqual(describe("2026-10-30T20:00:05"), "10月30日 星期五 20:00:05")
        self.assertEqual(describe("2027-01-01T08:00:00"), "2027年1月1日 星期五 08:00")

    def test_trigger_text(self):
        tasks = [{"prompt": "提醒大家喝水"}, {"prompt": "播报天气"}]
        self.assertEqual(schedules.trigger_text(tasks[:1]), "【定时任务】提醒大家喝水")
        self.assertEqual(schedules.trigger_text(tasks), "【定时任务】\n1. 提醒大家喝水\n2. 播报天气")


class StoreTest(TempDatabase, unittest.TestCase):
    def setUp(self):
        self.make_path()

    def test_missing_database_is_not_created(self):
        self.assertEqual(schedules.list_tasks(self.path, now=MONDAY_8AM, tz=TZ8), [])
        self.assertEqual(schedules.collect_due(self.path, now=MONDAY_8AM, tz=TZ8), ([], None))
        self.assertIsNone(self.get(1))
        with self.assertRaises(schedules.TaskNotFound):
            self.update(1, {"enabled": False})
        with self.assertRaises(schedules.TaskNotFound):
            schedules.delete_task(self.path, 1)
        self.assertFalse(Path(self.path).parent.exists())

    def test_database_without_the_table_lists_nothing(self):
        Path(self.path).parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
        self.assertEqual(schedules.list_tasks(self.path, now=MONDAY_8AM, tz=TZ8), [])
        self.assertEqual(self.collect("2026-10-26T09:00:00"), ([], None))
        self.assertIsNone(self.get(1))

    def test_create_fills_in_the_schedule(self):
        daily = self.create({"prompt": "  提醒我喝水\r\n多喝点 ", "repeat": "daily", "time": "9:00", "title": " 喝水 "})
        self.assertEqual(
            {key: daily[key] for key in ("id", "title", "prompt", "repeat", "time", "date", "weekdays", "enabled")},
            {"id": 1, "title": "喝水", "prompt": "提醒我喝水\n多喝点", "repeat": "daily", "time": "09:00",
             "date": "", "weekdays": [], "enabled": True},
        )
        self.assertEqual((daily["summary"], daily["status_text"], daily["next_run"]),
                         ("每天 09:00", "下次 今天 09:00", "2026-10-26T09:00:00+08:00"))
        # 不填日期：今天还没到就是今天，过了就是明天
        self.assertEqual(self.create({"prompt": "开会", "time": "15:00"})["date"], "2026-10-26")
        self.assertEqual(self.create({"prompt": "早起", "time": "07:00"})["date"], "2026-10-27")
        timer = self.create({"prompt": "关火", "in_minutes": 10}, now=MONDAY_8AM + datetime.timedelta(seconds=30.5))
        self.assertEqual((timer["repeat"], timer["date"], timer["time"], timer["status_text"]),
                         ("once", "2026-10-26", "08:10:30", "下次 今天 08:10:30"))
        weekly = self.create({"prompt": "跑步", "repeat": "weekly", "weekdays": [5, 1, 3, 1], "time": "20:00",
                              "date": "2026-10-30"})
        self.assertEqual((weekly["weekdays"], weekly["date"], weekly["summary"]), ([1, 3, 5], "", "每周一、三、五 20:00"))
        hourly = self.create({"prompt": "起来活动一下", "repeat": "hourly", "time": "13:45"})
        self.assertEqual((hourly["time"], hourly["status_text"]), ("00:45", "下次 今天 08:45"))
        self.assertEqual(
            query(self.path, "SELECT repeat, time, date, weekdays, enabled, created_at, last_run_at FROM scheduled_tasks "
                             "WHERE id = 1"),
            [("daily", "09:00", None, "[]", 1, "2026-10-26T00:00:00+00:00", None)],
        )

    def test_invalid_input_is_rejected_without_saving(self):
        for raw, message in (
            ([], "JSON 对象"),
            ({"repeat": "daily", "time": "09:00"}, "说明"),
            ({"prompt": "  ", "time": "09:00"}, "说明"),
            ({"prompt": "长" * 501, "time": "09:00"}, "500"),
            ({"prompt": "a\x00b", "time": "09:00"}, "控制字符"),
            ({"prompt": "x", "time": "09:00", "title": "两行\n名称"}, "名称"),
            ({"prompt": "x", "time": "09:00", "title": "名" * 41}, "名称"),
            ({"prompt": "x", "repeat": "monthly", "time": "09:00"}, "repeat"),
            ({"prompt": "x", "repeat": "daily"}, "time"),
            ({"prompt": "x", "repeat": "daily", "time": "24:00"}, "time"),
            ({"prompt": "x", "repeat": "daily", "time": "9点"}, "time"),
            ({"prompt": "x", "repeat": "weekly", "time": "09:00"}, "星期"),
            ({"prompt": "x", "repeat": "weekly", "time": "09:00", "weekdays": [0]}, "星期"),
            ({"prompt": "x", "repeat": "weekly", "time": "09:00", "weekdays": [True]}, "星期"),
            ({"prompt": "x", "time": "09:00", "date": "2026-02-30"}, "date"),
            ({"prompt": "x", "time": "07:59", "date": "2026-10-26"}, "过去"),
            ({"prompt": "x", "in_minutes": 0}, "in_minutes"),
            ({"prompt": "x", "in_minutes": 1.5}, "in_minutes"),
            ({"prompt": "x", "in_minutes": True}, "in_minutes"),
            ({"prompt": "x", "repeat": "daily", "in_minutes": 5}, "不重复"),
            ({"prompt": "x", "time": "09:00", "enabled": "yes"}, "enabled"),
        ):
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, message):
                self.create(raw)
        self.assertFalse(Path(self.path).exists())

    def test_task_limit(self):
        with patch.object(schedules, "MAX_TASKS", 2):
            for prompt in ("a", "b"):
                self.create({"prompt": prompt, "repeat": "daily", "time": "09:00"})
            with self.assertRaisesRegex(ValueError, "最多 2 个"):
                self.create({"prompt": "c", "repeat": "daily", "time": "09:00"})
        self.assertEqual(len(schedules.list_tasks(self.path, now=MONDAY_8AM, tz=TZ8)), 2)

    def test_update_changes_only_the_given_fields(self):
        created = self.create({"prompt": "开会", "time": "15:00", "date": "2026-10-27"})
        later = MONDAY_8AM + datetime.timedelta(hours=1)
        moved = self.update(created["id"], {"time": "16:30"}, now=later)  # 没给日期：沿用原来还没到的那天
        self.assertEqual((moved["date"], moved["time"], moved["prompt"]), ("2026-10-27", "16:30", "开会"))
        self.assertEqual(moved["updated_at"], "2026-10-26T01:00:00+00:00")
        daily = self.update(created["id"], {"repeat": "daily"}, now=later)
        self.assertEqual((daily["date"], daily["summary"]), ("", "每天 16:30"))
        paused = self.update(created["id"], {"enabled": False, "title": "例会"}, now=later)
        self.assertEqual((paused["enabled"], paused["title"], paused["status_text"]), (False, "例会", "已暂停"))
        with self.assertRaises(schedules.TaskNotFound):
            self.update(99, {"enabled": True})

    def test_finished_once_task_can_be_edited_and_rescheduled(self):
        created = self.create({"prompt": "关火", "in_minutes": 5})
        schedules.mark_runs(self.path, [(created["id"], local("2026-10-26T08:05:00"))], status="done")
        later = MONDAY_8AM + datetime.timedelta(hours=1)
        renamed = self.update(created["id"], {"prompt": "关燃气"}, now=later)  # 不动时间就不校验时间
        self.assertEqual((renamed["prompt"], renamed["status_text"], renamed["last_run_text"]),
                         ("关燃气", "已完成", "今天 08:05"))
        with self.assertRaisesRegex(ValueError, "过去"):
            self.update(created["id"], {"date": "2026-10-26", "time": "08:30"}, now=later)
        again = self.update(created["id"], {"time": "10:00"}, now=later)
        self.assertEqual((again["date"], again["status_text"]), ("2026-10-26", "下次 今天 10:00"))

    def test_delete(self):
        created = self.create({"prompt": "a", "repeat": "daily", "time": "09:00"})
        schedules.delete_task(self.path, created["id"])
        self.assertEqual(schedules.list_tasks(self.path, now=MONDAY_8AM, tz=TZ8), [])
        with self.assertRaises(schedules.TaskNotFound):
            schedules.delete_task(self.path, created["id"])


class CollectTest(TempDatabase, unittest.TestCase):
    def setUp(self):
        self.make_path()

    def test_due_task_runs_once_then_waits_for_the_next(self):
        daily = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        self.assertEqual(self.collect("2026-10-26T08:30:00"), ([], local("2026-10-26T09:00:00")))
        due, wake = self.collect("2026-10-26T09:00:30")
        self.assertEqual([(task["id"], run) for task, run in due], [(daily["id"], local("2026-10-26T09:00:00"))])
        self.assertEqual(wake, local("2026-10-27T09:00:00"))
        self.assertEqual(self.get(daily["id"], now=local("2026-10-26T09:00:30"))["status_text"], "马上执行")
        schedules.mark_runs(self.path, [(daily["id"], local("2026-10-26T09:00:00"))], status="done")
        self.assertEqual(self.collect("2026-10-26T09:01:00"), ([], local("2026-10-27T09:00:00")))

    def test_runs_missed_while_harness_was_down_are_recorded(self):
        once = self.create({"prompt": "关火", "in_minutes": 10})  # 今天 08:10
        daily = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        # 三天后 09:05 才开机：每天的任务只补今天这一次，一次性的早过了宽限，记为错过
        due, wake = self.collect("2026-10-29T09:05:00")
        self.assertEqual([(task["id"], run) for task, run in due], [(daily["id"], local("2026-10-29T09:00:00"))])
        self.assertEqual(wake, local("2026-10-30T09:00:00"))
        missed = self.get(once["id"], now=local("2026-10-29T09:05:00"))
        self.assertEqual((missed["last_status"], missed["status_text"], missed["enabled"]), ("missed", "已错过", True))
        self.assertEqual(missed["last_run_at"], "2026-10-26T00:10:00+00:00")

    def test_paused_and_resumed_tasks_do_not_catch_up(self):
        daily = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        self.update(daily["id"], {"enabled": False})
        self.assertEqual(self.collect("2026-10-26T09:01:00"), ([], None))
        # 09:05 才恢复：从恢复的时刻往后找，今天 09:00 那次不补
        self.update(daily["id"], {"enabled": True}, now=local("2026-10-26T09:05:00"))
        self.assertEqual(self.collect("2026-10-26T09:06:00"), ([], local("2026-10-27T09:00:00")))

    def test_wake_time_is_the_earliest_task(self):
        self.create({"prompt": "a", "repeat": "daily", "time": "21:00"})
        self.create({"prompt": "b", "repeat": "hourly", "time": "00:20"})
        self.assertEqual(self.collect("2026-10-26T08:00:00")[1], local("2026-10-26T08:20:00"))


class FakeBot:
    """只记下 send_text 收到的消息。"""

    def __init__(self):
        self.sent = []
        self.llm = SimpleNamespace(messages=[], round=0)

    async def send_text(self, text, role="user", response_instructions=None, **kwargs):
        self.sent.append((text, response_instructions))


class MainScheduleTest(TempDatabase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_path()
        db_path = patch.object(main, "SESSION_DB_PATH", self.path)
        db_path.start()
        self.addCleanup(db_path.stop)
        config = copy.deepcopy(main.DEFAULT_CONFIG)
        config["timezone"] = 8
        self.client = main.AIClient(config)
        self.bot = FakeBot()

    async def fake_restart(self):
        self.client.chat_bot, self.client.is_session_closed = self.bot, False

    @staticmethod
    def now():
        return datetime.datetime.now(datetime.timezone.utc)

    async def settle(self):
        await asyncio.gather(*list(self.client._text_tasks))

    async def wait_until(self, condition):
        for _ in range(300):
            if condition():
                return
            await asyncio.sleep(0.01)
        self.fail("条件一直没有满足")

    async def run_loop(self, until):
        task = asyncio.create_task(self.client._schedule_loop())
        try:
            await self.wait_until(until)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_due_tasks_reopen_a_closed_session_in_one_message(self):
        first = self.create({"prompt": "提醒大家喝水", "repeat": "daily", "time": "09:00"})
        second = self.create({"prompt": "播报天气", "repeat": "daily", "time": "09:00"})
        self.client.chat_bot, self.client.is_session_closed = FakeBot(), True  # 上一轮会话已经结束
        run = local("2026-10-26T09:00:00")
        with patch.object(self.client, "restart", AsyncMock(side_effect=self.fake_restart)) as restart:
            await self.client._run_scheduled([(first, run), (second, run)])
            await self.settle()
        restart.assert_awaited_once()
        self.assertEqual(self.bot.sent, [(schedules.trigger_text([first, second]), schedules.TRIGGER_INSTRUCTIONS)])
        self.assertEqual(query(self.path, "SELECT last_run_at, last_status, last_error FROM scheduled_tasks"),
                         [("2026-10-26T01:00:00+00:00", "done", None)] * 2)
        self.assertEqual(self.client.schedule_state["revision"], 1)

    async def test_open_session_waits_for_the_current_reply(self):
        task = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        self.client.chat_bot, self.client.is_playing = self.bot, True
        events = []

        async def finish_reply():
            await asyncio.sleep(0.05)
            events.append("回复说完")
            self.client.is_playing = False

        async def send_text(*args, **kwargs):
            events.append("定时任务")

        self.bot.send_text = send_text
        with patch.object(self.client, "restart", AsyncMock()) as restart:
            await asyncio.gather(finish_reply(), self.client._run_scheduled([(task, local("2026-10-26T09:00:00"))]))
            await self.settle()
        restart.assert_not_awaited()  # 会话开着就接在当前会话里
        self.assertEqual(events, ["回复说完", "定时任务"])

    async def test_busy_session_is_interrupted_after_the_wait(self):
        task = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        self.client.chat_bot, self.client.is_playing = self.bot, True  # 一直在说（例如放歌）
        with patch.object(main, "SCHEDULE_IDLE_WAIT", 0):
            await self.client._run_scheduled([(task, local("2026-10-26T09:00:00"))])
            await self.settle()
        self.assertEqual(len(self.bot.sent), 1)

    async def test_failed_start_is_recorded_on_the_task(self):
        task = self.create({"prompt": "喝水", "repeat": "daily", "time": "09:00"})

        async def failed_restart():
            self.client.last_error = "TTS（豆包 v1）尚未配置：请在 .env 中填写 BYTEDANCE_TTS_APP_ID"

        with patch.object(self.client, "restart", AsyncMock(side_effect=failed_restart)), \
                self.assertLogs("web_console", level="ERROR"):
            await self.client._run_scheduled([(task, local("2026-10-26T09:00:00"))])
        saved = self.get(task["id"])
        self.assertEqual((saved["last_status"], saved["last_error"]), ("error", self.client.last_error))
        self.assertEqual(self.bot.sent, [])

    async def test_loop_runs_tasks_when_they_are_due(self):
        now = self.now()
        clock = (now.astimezone(TZ8) - datetime.timedelta(minutes=1)).strftime("%H:%M")
        task = self.create({"prompt": "喝水", "repeat": "daily", "time": clock}, now=now - datetime.timedelta(hours=1))
        with patch.object(self.client, "restart", AsyncMock(side_effect=self.fake_restart)):
            await self.run_loop(lambda: self.bot.sent)
            await self.settle()
        self.assertEqual(self.bot.sent, [(schedules.TRIGGER_PREFIX + "喝水", schedules.TRIGGER_INSTRUCTIONS)])
        saved = self.get(task["id"], now=self.now())
        self.assertEqual(saved["last_status"], "done")
        self.assertTrue(saved["status_text"].startswith("下次 明天"))

    async def test_run_now_wakes_the_loop_and_keeps_the_schedule(self):
        clock = (self.now().astimezone(TZ8) + datetime.timedelta(hours=3)).strftime("%H:%M")
        task = self.create({"prompt": "播报天气", "repeat": "daily", "time": clock}, now=self.now())
        with patch.object(self.client, "restart", AsyncMock(side_effect=self.fake_restart)) as restart:
            loop = asyncio.create_task(self.client._schedule_loop())
            try:
                await asyncio.sleep(0.05)  # 循环算完下一次执行时间后睡下
                self.assertEqual(self.bot.sent, [])
                self.assertEqual((await self.client.run_schedule_now(task["id"]))["id"], task["id"])
                await self.wait_until(lambda: self.bot.sent)
            finally:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            await self.settle()
        restart.assert_awaited_once()  # 还没有会话：跟唤醒词一样开一轮
        self.assertEqual(self.bot.sent, [(schedules.TRIGGER_PREFIX + "播报天气", schedules.TRIGGER_INSTRUCTIONS)])
        saved = self.get(task["id"], now=self.now())
        self.assertEqual(saved["last_status"], "done")
        self.assertTrue(saved["status_text"].startswith("下次 "))
        with self.assertRaises(schedules.TaskNotFound):
            await self.client.run_schedule_now(999)

    async def test_changes_bump_revision_and_wake_the_loop(self):
        created = await self.client.create_schedule({"prompt": "喝水", "repeat": "daily", "time": "09:00"})
        self.assertEqual(self.client.live_status()["schedules"], {"revision": 1, "error": ""})
        self.assertTrue(self.client._schedule_wakeup.is_set())
        await self.client.update_schedule(created["id"], {"enabled": False})
        await self.client.delete_schedule(created["id"])
        self.assertEqual(self.client.schedule_state["revision"], 3)
        status = self.client.live_status()
        status["schedules"]["revision"] = 99  # 推给页面的是副本
        self.assertEqual(self.client.schedule_state["revision"], 3)

    async def test_loop_reports_an_unreadable_database(self):
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_bytes(b"not a database" * 100)
        with self.assertLogs("web_console", level="ERROR"):
            await self.run_loop(lambda: self.client.schedule_state["error"])
        self.assertEqual(self.client.schedule_state["error"], "定时任务读取或保存失败，详见服务日志")


class ScheduleHttpTest(TempDatabase, unittest.TestCase):
    def setUp(self):
        self.make_path()
        db_path = patch.object(main, "SESSION_DB_PATH", self.path)
        db_path.start()
        self.addCleanup(db_path.stop)
        config = copy.deepcopy(main.DEFAULT_CONFIG)
        config["timezone"] = 8
        self.client = main.AIClient(config)
        self.web = TestClient(create_app(self.client))
        self.web.__enter__()
        self.addCleanup(self.web.__exit__, None, None, None)

    def test_console_has_the_schedule_section_and_dialog(self):
        page = self.web.get("/").text
        for marker in ('id="schedule-group"', 'id="schedule-dialog"', "设置定时任务", 'placeholder="说明…"', "高级"):
            self.assertIn(marker, page)
        self.assertIn("定时任务", main.DEFAULT_CONFIG["mcp"]["mcpServers"])

    def test_create_list_update_run_and_delete(self):
        response = self.web.get("/api/schedules")
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual((data["ok"], data["tasks"], data["timezone"]), (True, [], "UTC+8"))
        self.assertRegex(data["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertFalse(Path(self.path).parent.exists())

        response = self.web.post("/api/schedules", json={"prompt": "提醒我喝水", "repeat": "daily", "time": "09:00"})
        self.assertEqual(response.status_code, 200)
        task = response.json()["task"]
        self.assertEqual((task["summary"], task["enabled"]), ("每天 09:00", True))
        self.assertEqual([item["id"] for item in self.web.get("/api/schedules").json()["tasks"]], [task["id"]])
        self.assertEqual(self.web.get("/api/state").json()["status"]["schedules"], {"revision": 1, "error": ""})

        response = self.web.patch(f"/api/schedules/{task['id']}", json={"enabled": False})
        self.assertEqual((response.status_code, response.json()["task"]["status_text"]), (200, "已暂停"))
        response = self.web.post(f"/api/schedules/{task['id']}/run")
        self.assertEqual((response.status_code, response.json()["task"]["id"]), (200, task["id"]))
        self.assertEqual(self.client._manual_runs, [task["id"]])

        self.assertEqual(self.web.delete(f"/api/schedules/{task['id']}").json(), {"ok": True})
        for method, url in (("PATCH", f"/api/schedules/{task['id']}"), ("DELETE", f"/api/schedules/{task['id']}"),
                            ("POST", f"/api/schedules/{task['id']}/run")):
            with self.subTest(method=method, url=url):
                response = self.web.request(method, url, json={"enabled": True} if method == "PATCH" else None)
                self.assertEqual((response.status_code, response.json()), (404, {"ok": False, "error": "定时任务不存在"}))

    def test_invalid_requests_are_rejected(self):
        for body in ([], {"prompt": ""}, {"prompt": "x", "repeat": "daily", "time": "25:00"}):
            with self.subTest(body=body):
                response = self.web.post("/api/schedules", json=body)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()["ok"])
        response = self.web.post("/api/schedules", content=b"{bad", headers={"Content-Type": "application/json"})
        self.assertEqual((response.status_code, response.json()["error"]), (400, "请求需要 JSON 对象"))
        self.assertEqual(self.web.patch("/api/schedules/abc", json={}).status_code, 404)
        self.assertFalse(Path(self.path).exists())

    def test_unreadable_database_returns_503(self):
        Path(self.path).parent.mkdir(parents=True)
        Path(self.path).write_bytes(b"not a database" * 100)
        with self.assertLogs("web_console", level="ERROR"):
            self.assertEqual(self.web.get("/api/schedules").status_code, 503)
            response = self.web.post("/api/schedules", json={"prompt": "x", "repeat": "daily", "time": "09:00"})
            self.assertEqual(response.status_code, 503)


class HarnessOpener:
    """把 schedule_mcp 发往 harness 的请求转给 TestClient，走真实的 api() 和错误处理。"""

    def __init__(self, web):
        self.web = web

    def open(self, request, timeout):
        response = self.web.request(
            request.get_method(), request.full_url.removeprefix(schedule_mcp.HARNESS_URL),
            content=request.data, headers=dict(request.header_items()),
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "error", None,
                                         io.BytesIO(response.content))
        return io.BytesIO(response.content)


class ScheduleMcpTest(TempDatabase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.make_path()
        db_path = patch.object(main, "SESSION_DB_PATH", self.path)
        db_path.start()
        self.addCleanup(db_path.stop)
        config = copy.deepcopy(main.DEFAULT_CONFIG)
        config["timezone"] = 8
        self.client = main.AIClient(config)
        self.web = TestClient(create_app(self.client))
        self.web.__enter__()
        self.addCleanup(self.web.__exit__, None, None, None)
        opener = patch.object(schedule_mcp, "_opener", HarnessOpener(self.web))
        opener.start()
        self.addCleanup(opener.stop)

    async def test_tools_manage_the_same_tasks_as_the_console(self):
        async with Client(schedule_mcp.mcp) as mcp:
            self.assertEqual(sorted(tool.name for tool in await mcp.list_tools()), [
                "create_scheduled_task", "delete_scheduled_task", "list_scheduled_tasks", "update_scheduled_task",
            ])
            timer = (await mcp.call_tool("create_scheduled_task", {"prompt": "提醒用户关火", "in_minutes": 10})).data
            self.assertFalse(timer["isError"])
            self.assertEqual(set(timer["task"]), {"id", "title", "prompt", "enabled", "schedule", "status"})
            self.assertTrue(timer["task"]["status"].startswith("下次 "))
            weekly = (await mcp.call_tool("create_scheduled_task", {
                "prompt": "提醒用户去跑步", "repeat": "weekly", "weekdays": [1, 3, 5], "time": "20:00", "title": "跑步",
            })).data["task"]
            self.assertEqual((weekly["schedule"], weekly["title"]), ("每周一、三、五 20:00", "跑步"))
            listed = (await mcp.call_tool("list_scheduled_tasks")).data
            self.assertEqual(([task["id"] for task in listed["tasks"]], listed["timezone"]),
                             ([timer["task"]["id"], weekly["id"]], "UTC+8"))
            paused = (await mcp.call_tool("update_scheduled_task", {"task_id": weekly["id"], "enabled": False})).data
            self.assertEqual(paused["task"]["status"], "已暂停")
            deleted = (await mcp.call_tool("delete_scheduled_task", {"task_id": timer["task"]["id"]})).data
            self.assertEqual(deleted, {"isError": False, "deleted": timer["task"]["id"]})
        # 页面看到的是同一份数据，语音的每次改动也都推给了页面
        self.assertEqual([task["id"] for task in self.web.get("/api/schedules").json()["tasks"]], [weekly["id"]])
        self.assertEqual(self.client.schedule_state["revision"], 4)

    async def test_errors_are_returned_to_the_model(self):
        async with Client(schedule_mcp.mcp) as mcp:
            with self.assertLogs("schedule", level="WARNING"):
                bad = (await mcp.call_tool("create_scheduled_task",
                                           {"prompt": "x", "repeat": "daily", "time": "25:00"})).data
                missing = (await mcp.call_tool("delete_scheduled_task", {"task_id": 42})).data
                with patch.object(schedule_mcp._opener, "open",
                                  side_effect=urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))):
                    down = (await mcp.call_tool("list_scheduled_tasks")).data
            self.assertEqual(bad, {"isError": True, "error": "time 需为 24 小时制的 HH:MM，例如 07:30"})
            self.assertEqual(missing, {"isError": True, "error": "定时任务不存在"})
            self.assertTrue(down["isError"])
            self.assertIn("连不上 harness", down["error"])
            nothing = (await mcp.call_tool("update_scheduled_task", {"task_id": 42})).data
            self.assertEqual(nothing, {"isError": True, "error": "没有要修改的字段"})
            for args in ({"prompt": "x", "repeat": "monthly"}, {"prompt": "x", "in_minutes": 0},
                         {"prompt": "x", "repeat": "weekly", "weekdays": [8], "time": "09:00"}):
                with self.subTest(args=args):
                    self.assertTrue((await mcp.call_tool("create_scheduled_task", args, raise_on_error=False)).is_error)
        self.assertFalse(Path(self.path).exists())


if __name__ == "__main__":
    unittest.main()
