import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from quality.core import DEFAULT_SETTINGS, episodes
from quality.db import connect, save_observation
from quality.report import date_bounds
from quality.telegram_bot import ReportBot
from quality.topics import format_project_report, publish_project_reports


class FakeForumAPI:
    def __init__(self):
        self.topics = []
        self.edits = []
        self.messages = []
        self.pins = []
        self.callbacks = []

    def call(self, method, **kwargs):
        if method == "getChat":
            return {"id": kwargs["chat_id"], "title": "VK КВАЛ ЛИДЫ", "is_forum": True}
        if method == "getMe":
            return {"id": 999}
        if method == "getChatMember":
            return {"status": "administrator", "can_manage_topics": True}
        raise AssertionError(method)

    def create_topic(self, chat_id, name):
        self.topics.append((chat_id, name))
        return 100 + len(self.topics)

    def edit_topic(self, chat_id, thread_id, name):
        self.edits.append((chat_id, thread_id, name))

    def edit_markup(self, chat_id, message_id, reply_markup):
        self.edits.append((chat_id, message_id, reply_markup))

    def send(self, chat_id, text, keyboard=False, thread_id=None, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text, thread_id, parse_mode))
        return {"message_id": 1000 + len(self.messages)}

    def pin(self, chat_id, message_id):
        self.pins.append((chat_id, message_id))

    def answer_callback(self, callback_id, text=None):
        self.callbacks.append((callback_id, text))


class ForumTests(unittest.TestCase):
    def test_completed_report_waits_for_queued_group(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'Магазин','secret')")
                db.execute("INSERT INTO report_chats(chat_id,title,connected_at) VALUES (-100123,'Отчёт',1)")
                db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at) VALUES (42,1,10)")
                db.execute("INSERT INTO sync_jobs(group_id,requested_at) VALUES (42,1)")
                db.commit()
            api = FakeForumAPI()
            bot = ReportBot(path, api)
            with patch.dict(os.environ, {"GOOGLE_SPREADSHEET_ID": ""}):
                self.assertFalse(bot._publish_pending_once())
                with connect(path) as db:
                    db.execute("DELETE FROM sync_jobs WHERE group_id=42")
                    db.commit()
                self.assertTrue(bot._publish_pending_once())
                self.assertFalse(bot._publish_pending_once())
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT published_at FROM report_delivery_state").fetchone()[0], 10)

    def test_unlabeled_row_uses_same_format_as_campaign_rows(self):
        report = format_project_report("9:41 Store | Калуга", [
            {"label_id": "248543102", "total": 28, "qualified": 18,
             "not_qualified": 10, "review": 0},
            {"label_id": "Без метки", "total": 145, "qualified": 111,
             "not_qualified": 34, "review": 0},
        ], "18.10–16.11")[0]
        self.assertIn("• <code>248543102</code> — <b>18 из 28</b> (64,3%)", report)
        self.assertIn("• Без метки — <b>111 из 145</b> (76,6%)", report)
        self.assertNotIn("▫️", report)

    def test_connect_creates_store_topic_and_does_not_repeat_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (123,1)")
                db.execute("INSERT INTO projects(name,city) VALUES ('9:41 Store | Калуга','Калуга')")
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'9:41 Store | Калуга','secret')")
                since, _ = date_bounds(db, 30)
                item = episodes(777, [{"id": 1, "date": since + 3600,
                                       "out": 0, "text": "Цена?", "ref": "248543102"}],
                                DEFAULT_SETTINGS, since)[0]
                save_observation(db, 42, item, since + 3600)
                db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at) VALUES (42,1,2)")
                db.commit()
            api = FakeForumAPI()
            bot = ReportBot(path, api)
            update = {"message": {"chat": {"id": -100123, "type": "supergroup"},
                                  "from": {"id": 123},
                                  "text": "/connect"}}
            bot.handle(update)
            self.assertEqual(api.topics, [(-100123, "9:41 Store | Калуга")])
            report = next(message for message in api.messages if message[2] == 101)
            self.assertIn("248543102", report[1])
            self.assertIn("Последние 30 дней", report[1])
            self.assertIn("📊 <b>VK КВАЛЫ</b>\n<b>9:41 Store | Калуга</b>\nПоследние 30 дней:", report[1])
            self.assertIn("• <code>248543102</code> — <b>1 из 1</b> (100,0%)", report[1])
            self.assertNotIn("🏪", report[1])
            self.assertNotIn("📅", report[1])
            self.assertNotIn("Вчера", report[1])
            self.assertNotIn("Поздний ответ", report[1])
            self.assertEqual(report[3], "HTML")
            bot.handle(update)
            self.assertEqual(len(api.topics), 1)
            self.assertEqual(len([message for message in api.messages if message[2] == 101]), 1)
            self.assertEqual(len(api.pins), 1)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_topics").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM general_panels").fetchone()[0], 1)
            with patch.object(bot, "start_sync", return_value=True) as sync:
                bot.handle({"callback_query": {"id": "action", "from": {"id": 123},
                                               "data": "g:report",
                                               "message": {"chat": {"id": -100123,
                                                                    "type": "supergroup"}}}})
            sync.assert_called_once_with(-100123)
            self.assertIn("поставлено в очередь", api.messages[-2][1])

    def test_existing_city_topic_is_renamed_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO projects(name,city) VALUES ('9:41 Store | Калуга','Калуга')")
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'9:41 Store | Калуга','secret')")
                db.execute("INSERT INTO report_chats(chat_id,title,connected_at) VALUES (-100123,'Отчёт',1)")
                db.execute("INSERT INTO city_topics(chat_id,city,thread_id) VALUES (-100123,'Калуга',77)")
                since, _ = date_bounds(db, 30)
                item = episodes(777, [{"id": 1, "date": since + 3600, "out": 0,
                                       "text": "Цена?", "ref": "248543102"}], DEFAULT_SETTINGS, since)[0]
                save_observation(db, 42, item, since + 3600)
                db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at) VALUES (42,1,2)")
                db.commit()
            api = FakeForumAPI()
            with connect(path) as db:
                publish_project_reports(db, api, -100123)
            self.assertEqual(api.edits, [(-100123, 77, "9:41 Store | Калуга")])
            self.assertEqual(api.topics, [])
            self.assertEqual(len([message for message in api.messages if message[2] == 77]), 1)

    def test_non_forum_chat_is_not_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (123,1)")
                db.commit()
            api = FakeForumAPI()
            api.call = lambda method, **kwargs: {"is_forum": False}
            ReportBot(path, api).handle({"message": {"chat": {"id": -100123, "type": "supergroup"},
                                                    "from": {"id": 123},
                                                    "text": "/connect"}})
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM report_chats").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
