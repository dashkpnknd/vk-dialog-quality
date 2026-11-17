import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from quality.core import DEFAULT_SETTINGS, episodes
from quality.db import connect, save_observation
from quality.telegram_bot import ReportBot, TelegramAPI, group_summary, summary


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.markups = []
        self.edits = []
        self.documents = []
        self.callback_answers = []
        self.deleted = []

    def send(self, chat_id, text, keyboard=False, parse_mode=None, reply_markup=None):
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)

    def edit(self, chat_id, message_id, text, reply_markup):
        self.edits.append((chat_id, message_id, text, reply_markup))

    def answer_callback(self, callback_id, text=None):
        self.callback_answers.append((callback_id, text))

    def delete(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def send_document(self, chat_id, filename, content, caption):
        self.documents.append(filename)
        self.messages.append((chat_id, caption + "\n" + content))


class BotTests(unittest.TestCase):
    def test_telegram_retries_rate_limit_for_many_topic_reports(self):
        class Response:
            def __init__(self, data):
                self.data = data
            def json(self):
                return self.data
        responses = [Response({"ok": False, "parameters": {"retry_after": 2}}),
                     Response({"ok": True, "result": {"message_id": 10}})]
        with patch("quality.telegram_bot.requests.post", side_effect=responses) as post, \
                patch("quality.telegram_bot.time.sleep") as sleep:
            self.assertEqual(TelegramAPI("secret").call("sendMessage", chat_id=1, text="report"),
                             {"message_id": 10})
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_shared_command_unlocks_bot_and_limits_guesses(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            api = FakeTelegram()
            bot = ReportBot(path, api, access_code="123")
            chat = {"id": 999, "type": "private"}
            with patch.object(bot, "start_sync", return_value=True) as sync:
                bot.handle({"message": {"chat": chat, "text": "/report"}})
                sync.assert_not_called()
                self.assertIn("команду", api.messages[-1][1])
                bot.handle({"message": {"chat": chat, "text": "/start123"}})
                self.assertIn("Доступ открыт", api.messages[-1][1])
                bot.handle({"message": {"chat": chat, "text": "/report"}})
                sync.assert_called_once_with(999)
            with connect(path) as db:
                self.assertTrue(bot._authorized(db, 999))
                for _ in range(5):
                    self.assertEqual(bot._authorize(db, 888, "wrong"), "invalid")
                self.assertEqual(bot._authorize(db, 888, "123"), "blocked")

    def test_general_buttons_require_private_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO report_chats(chat_id,title,connected_at) VALUES (-100123,'Отчёт',1)")
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1)")
                db.commit()
            api = FakeTelegram()
            bot = ReportBot(path, api, access_code="123")
            query = {"id": "callback", "from": {"id": 777}, "data": "g:report",
                     "message": {"chat": {"id": -100123, "type": "supergroup"}}}
            with patch.object(bot, "start_sync", return_value=True) as sync:
                bot.handle({"callback_query": query})
                sync.assert_not_called()
                self.assertIn("команду доступа", api.callback_answers[-1][1])
                query["from"]["id"] = 999
                bot.handle({"callback_query": query})
                sync.assert_called_once_with(-100123)

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
                db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at) VALUES (42,1,2)")
                db.commit()
                self.assertIn("Тестовый магазин", summary(db))
                self.assertIn("232577648", group_summary(db, 42))
            api = FakeTelegram()
            bot = ReportBot(db_path, api)
            with connect(db_path) as db:
                menu, markup = bot._report_menu(db)
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1),(123,1)")
                db.commit()
            self.assertIn("Выберите магазин", menu)
            self.assertIn("Тестовый магазин", markup["inline_keyboard"][0][0]["text"])
            self.assertEqual(api.documents, [])
            bot.handle({"callback_query": {"id": "answer", "from": {"id": 999}, "data": "r:g:42:0",
                                           "message": {"message_id": 11,
                                                       "chat": {"id": 999, "type": "private"}}}})
            self.assertIn("📊 <b>VK КВАЛЫ</b>", api.messages[-1][1])
            self.assertIn("232577648", api.messages[-1][1])
            bot.handle({"message": {"chat": {"id": 123, "type": "private"}, "text": "/group 42"}})
            self.assertIn("232577648", api.messages[-1][1])

    def test_report_menu_paginates_many_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "quality.sqlite3")
            with connect(db_path) as db:
                for group_id in range(1, 18):
                    db.execute("INSERT INTO groups(group_id,project,token) VALUES (?,?,?)",
                               (group_id, "Магазин {:02d}".format(group_id), "secret"))
                db.commit()
            api = FakeTelegram()
            bot = ReportBot(db_path, api)
            with connect(db_path) as db:
                _, markup = bot._report_menu(db)
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1)")
                db.commit()
            self.assertEqual(len(markup["inline_keyboard"]), 9)
            bot.handle({"callback_query": {"id": "answer", "from": {"id": 999}, "data": "r:p:2",
                                           "message": {"message_id": 11,
                                                       "chat": {"id": 999, "type": "private"}}}})
            self.assertIn("Магазин 17", api.edits[-1][3]["inline_keyboard"][0][0]["text"])

    def test_report_button_starts_fresh_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeTelegram()
            bot = ReportBot(str(Path(directory) / "quality.sqlite3"), api)
            with connect(bot.db_path) as db:
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1)")
                db.commit()
            with patch.object(bot, "start_sync", return_value=True) as sync:
                bot.handle({"message": {"chat": {"id": 999, "type": "private"},
                                        "text": "📊 Отчёт"}})
            sync.assert_called_once_with(999)
            self.assertIn("поставлено в очередь", api.messages[-2][1])
            self.assertIn("Выберите магазин", api.messages[-1][1])

    def test_campaign_id_search_keeps_same_id_separate_by_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            from time import time
            now = int(time())
            with connect(path) as db:
                for group_id, project, peer, message in (
                    (42, "Магазин А", 100, {"text": "Сколько стоит?"}),
                    (43, "Магазин Б", 101, {"text": "Узнать цену", "payload": "{}"}),
                ):
                    db.execute("INSERT INTO groups(group_id,project,token) VALUES (?,?,?)",
                               (group_id, project, "secret"))
                    db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at) VALUES (?,?,?)",
                               (group_id, 1, 2))
                    item = episodes(peer, [{"id": 1, "date": now, "out": 0,
                                           "ref": "Из VK Рекламы 248543102", **message}],
                                    DEFAULT_SETTINGS, now)[0]
                    save_observation(db, group_id, item, now)
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1)")
                db.commit()
            api = FakeTelegram()
            bot = ReportBot(path, api)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"},
                                    "text": "🎯 Кампании"}})
            self.assertIn("Отправьте числовой рекламный ID", api.messages[-1][1])
            bot.handle({"message": {"chat": {"id": 999, "type": "private"},
                                    "text": "248543102"}})
            result = api.messages[-1][1]
            self.assertIn("Магазин А", result)
            self.assertIn("Магазин Б", result)
            self.assertIn("Квалы: <b>1</b> (100,0%) · не квалы: <b>0</b>", result)
            self.assertIn("Квалы: <b>0</b> (0,0%) · не квалы: <b>1</b>", result)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"},
                                    "text": "/campaign 111"}})
            self.assertIn("обращений пока нет", api.messages[-1][1])

    def test_campaign_search_in_connected_general_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO report_chats(chat_id,title,connected_at) VALUES (-100123,'Отчёт',1)")
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (999,1)")
                db.commit()
            api = FakeTelegram()
            bot = ReportBot(path, api)
            bot.handle({"callback_query": {"id": "callback", "from": {"id": 999},
                                           "data": "g:campaign",
                                           "message": {"chat": {"id": -100123, "type": "supergroup"}}}})
            self.assertTrue(api.markups[-1]["force_reply"])
            bot.handle({"message": {"chat": {"id": -100123, "type": "supergroup"},
                                    "from": {"id": 999}, "text": "248543102",
                                    "reply_to_message": {"text": api.messages[-1][1]}}})
            self.assertIn("обращений пока нет", api.messages[-1][1])

    def test_report_requested_during_daily_sync_is_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'Магазин','secret')")
                db.commit()
            bot = ReportBot(path, FakeTelegram())
            self.assertTrue(bot.start_sync(None))
            with connect(path) as db:
                db.execute("UPDATE sync_jobs SET lease_token='worker',lease_until=9999999999")
                db.commit()
            self.assertTrue(bot.start_sync(999))
            with connect(path) as db:
                job = db.execute("SELECT priority,rerun FROM sync_jobs WHERE group_id=42").fetchone()
                self.assertEqual(tuple(job), (1, 1))

    def test_daily_collection_enqueues_once_after_three_moscow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            with connect(path) as db:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'Магазин','secret')")
                db.execute("INSERT INTO settings(key,value) VALUES ('last_daily_run','\"2026-11-15\"')")
                db.commit()
            bot = ReportBot(path, FakeTelegram())
            zone = ZoneInfo("Europe/Moscow")
            class BeforeThree:
                @staticmethod
                def now(timezone):
                    return datetime(2026, 11, 16, 2, 59, tzinfo=zone)
            class AtThree:
                @staticmethod
                def now(timezone):
                    return datetime(2026, 11, 16, 3, 0, tzinfo=zone)
            with patch("quality.telegram_bot.datetime", BeforeThree):
                bot._schedule()
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 0)
            with patch("quality.telegram_bot.datetime", AtThree):
                bot._schedule()
                bot._schedule()
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 1)
                self.assertEqual(json.loads(db.execute(
                    "SELECT value FROM settings WHERE key='last_daily_run'").fetchone()[0]), "2026-11-16")

    def test_start_subscribes_and_stop_unsubscribes(self):
        with tempfile.TemporaryDirectory() as directory:
            api = FakeTelegram()
            db_path = str(Path(directory) / "quality.sqlite3")
            bot = ReportBot(db_path, api)
            bot = ReportBot(db_path, api, access_code="123")
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/start123"}})
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/start"}})
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 1)
            bot.handle({"message": {"chat": {"id": 999, "type": "private"}, "text": "/stop"}})
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0], 0)

    @patch("quality.telegram_bot.VKClient")
    @patch("quality.telegram_bot.group_from_token", return_value=(123456789, "9:41 Store | Калуга"))
    def test_group_added_from_token_only(self, discover, client):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "quality.sqlite3")
            api = FakeTelegram()
            api.delete = lambda chat_id, message_id: None
            bot = ReportBot(db_path, api)
            chat = {"id": 123, "type": "private"}
            with connect(db_path) as db:
                db.execute("INSERT INTO authorized_users(user_id,authorized_at) VALUES (123,1)")
                db.commit()
            bot.handle({"message": {"chat": chat, "text": "➕ Подключить VK"}})
            with patch.object(bot, "start_sync", return_value=True) as sync:
                bot.handle({"message": {"chat": chat, "message_id": 42, "text": "secret"}})
            sync.assert_called_once_with(None, group_ids={123456789}, priority=2)
            with connect(db_path) as db:
                self.assertEqual(db.execute("SELECT project FROM groups").fetchone()[0], "9:41 Store | Калуга")
            discover.assert_called_once_with("secret")
            client.return_value.call.assert_called_once_with("messages.getConversations", count=1, offset=0)
            self.assertTrue(all("secret" not in text for _, text in api.messages))


if __name__ == "__main__":
    unittest.main()
