import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quality.core import DEFAULT_SETTINGS, episodes
from quality.db import connect, save_observation
from quality.telegram_bot import ReportBot, group_summary, summary


class FakeTelegram:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, keyboard=False):
        self.messages.append((chat_id, text))


class BotTests(unittest.TestCase):
    def test_private_admin_report_and_group_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "quality.sqlite3")
            with connect(db_path) as db:
                db.execute("INSERT INTO groups(group_id, project, token) VALUES (42, 'Тестовый магазин', 'secret')")
                # Use the current day so the rolling report includes this dialog.
                from time import time
                now = int(time())
                item = episodes(777, [{"id": 1, "date": now, "out": 0, "text": "Сколько стоит?", "ref": "Из VK Рекламы 232577648"}], DEFAULT_SETTINGS, now)[0]
                save_observation(db, 42, item, now)
                self.assertIn("Тестовый магазин", summary(db))
                self.assertIn("232577648", group_summary(db, 42))
            api = FakeTelegram()
            bot = ReportBot(db_path, api)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/report"}})
            self.assertIn("квалов: 1", api.messages[-1][1])
            bot.handle({"message": {"chat": {"id": 123, "type": "private"}, "text": "/report"}})
            self.assertIn("квалов: 1", api.messages[-1][1])
            bot.handle({"message": {"chat": {"id": 123, "type": "private"}, "text": "/group 42"}})
            self.assertIn("232577648", api.messages[-1][1])

    def test_start_subscribes_and_stop_unsubscribes(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeTelegram()
            db_path = str(Path(directory) / "quality.sqlite3")
            bot = ReportBot(db_path, api)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/start"}})
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 1)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/stop"}})
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)

    def test_project_and_group_added_through_buttons(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "quality.sqlite3")
            api = FakeTelegram()
            api.delete = lambda chat_id, message_id: None
            bot = ReportBot(db_path, api)
            chat = {"id": 123, "type": "private"}
            bot.handle({"message": {"chat": chat, "text": "➕ Проект"}})
            bot.handle({"message": {"chat": chat, "text": "Магазин А"}})
            bot.handle({"message": {"chat": chat, "text": "➕ Группа"}})
            bot.handle({"message": {"chat": chat, "message_id": 42, "text": "Магазин А | 123456789 | secret"}})
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT project FROM groups").fetchone()[0], "Магазин А")
            self.assertTrue(all("secret" not in text for _, text in api.messages))


if __name__ == "__main__":
    unittest.main()
