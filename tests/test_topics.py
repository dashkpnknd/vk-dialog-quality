import tempfile
import unittest
from pathlib import Path

from quality.core import DEFAULT_SETTINGS, episodes
from quality.db import connect, save_observation
from quality.report import date_bounds
from quality.telegram_bot import ReportBot


class FakeForumAPI:
    def __init__(self):
        self.topics = []
        self.edits = []
        self.messages = []

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

    def send(self, chat_id, text, keyboard=False, thread_id=None, parse_mode=None):
        self.messages.append((chat_id, text, thread_id, parse_mode))


class ForumTests(unittest.TestCase):
    def test_connect_creates_store_topic_and_does_not_repeat_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO projects(name,city) VALUES ('9:41 Store | Калуга','Калуга')")
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'9:41 Store | Калуга','secret')")
                since, _ = date_bounds(db, 30)
                item = episodes(777, [{"id": 1, "date": since + 3600,
                                       "out": 0, "text": "Цена?", "ref": "248543102"}],
                                DEFAULT_SETTINGS, since)[0]
                save_observation(db, 42, item, since + 3600)
            api = FakeForumAPI()
            bot = ReportBot(path, api)
            update = {"message": {"chat": {"id": -100123, "type": "supergroup"},
                                  "text": "/connect"}}
            bot.handle(update)
            self.assertEqual(api.topics, [(-100123, "9:41 Store | Калуга")])
            report = next(message for message in api.messages if message[2] == 101)
            self.assertIn("248543102", report[1])
            self.assertIn("Последние 30 дней", report[1])
            self.assertNotIn("Вчера", report[1])
            self.assertNotIn("Поздний ответ", report[1])
            self.assertEqual(report[3], "HTML")
            bot.handle(update)
            self.assertEqual(len(api.topics), 1)
            self.assertEqual(len([message for message in api.messages if message[2] == 101]), 1)
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_topics").fetchone()[0], 1)

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
            api = FakeForumAPI()
            ReportBot(path, api).handle({"message": {"chat": {"id": -100123, "type": "supergroup"},
                                                   "text": "/report"}})
            self.assertEqual(api.edits, [(-100123, 77, "9:41 Store | Калуга")])
            self.assertEqual(api.topics, [])
            self.assertEqual(len([message for message in api.messages if message[2] == 77]), 1)

    def test_non_forum_chat_is_not_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            api = FakeForumAPI()
            api.call = lambda method, **kwargs: {"is_forum": False}
            ReportBot(path, api).handle({"message": {"chat": {"id": -100123, "type": "supergroup"},
                                                    "text": "/connect"}})
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM report_chats").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
