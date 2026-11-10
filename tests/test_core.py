import json
import tempfile
import unittest
from pathlib import Path

from quality.core import DEFAULT_SETTINGS, episodes
from quality.db import connect, rows, save_observation
from quality.sync import sync_group


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.base = 1_700_000_000
        self.keyboard = {"buttons": [[{"action": {"type": "text", "label": "iPhone"}}]]}

    def test_buttons_do_not_qualify_but_text_does(self):
        messages = [
            {"id": 1, "date": self.base, "out": 1, "text": "Выберите", "keyboard": self.keyboard,
             "ref": "Из VK Рекламы 232577648"},
            {"id": 2, "date": self.base + 5, "out": 0, "text": "iPhone", "payload": json.dumps({"command": "iphone"})},
            {"id": 3, "date": self.base + 7, "out": 0, "text": "Сколько стоит 17 Pro?"},
        ]
        result = episodes(123, messages, DEFAULT_SETTINGS, self.base)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, "qualified")
        self.assertEqual(result[0].button_count, 1)
        self.assertEqual(result[0].label_id, "232577648")

    def test_new_campaign_keeps_old_result(self):
        messages = [
            {"id": 1, "date": self.base, "out": 0, "text": "Начать", "payload": "{}", "ref": "Из рекламы 111111111"},
            {"id": 2, "date": self.base + 86400, "out": 0, "text": "Цена?"},
            {"id": 3, "date": self.base + 10 * 86400, "out": 0, "text": "Начать", "payload": "{}", "ref_source": "Из VK Рекламы 222222222"},
            {"id": 4, "date": self.base + 12 * 86400, "out": 0, "text": "", "attachments": [{"type": "audio_message"}]},
        ]
        result = episodes(123, messages, DEFAULT_SETTINGS, self.base)
        self.assertEqual([(o.label_id, o.status) for o in result],
                         [("111111111", "qualified"), ("222222222", "qualified")])

    def test_late_reply_reclassifies_same_visit(self):
        initial = [{"id": 1, "date": self.base, "out": 0, "text": "Начать", "payload": "{}", "ref": "111111111"}]
        first = episodes(123, initial, DEFAULT_SETTINGS, self.base)[0]
        self.assertEqual(first.status, "not_qualified")
        later = initial + [{"id": 2, "date": self.base + 10 * 86400, "out": 0, "text": "Ответ"}]
        second = episodes(123, later, DEFAULT_SETTINGS, self.base)[0]
        self.assertEqual(second.episode_key, first.episode_key)
        self.assertEqual(second.status, "qualified")

    def test_old_dialog_without_new_marker_is_not_new_visit(self):
        messages = [{"id": 10, "date": self.base + 5, "out": 0, "text": "Привет"}]
        self.assertEqual(episodes(123, messages, DEFAULT_SETTINGS, self.base, has_older=True), [])

    def test_ambiguous_button_text_needs_review(self):
        messages = [{"id": 1, "date": self.base, "out": 1, "text": "Выберите", "keyboard": self.keyboard},
                    {"id": 2, "date": self.base + 1, "out": 0, "text": "iPhone"}]
        self.assertEqual(episodes(123, messages, DEFAULT_SETTINGS, self.base)[0].status, "review")

    def test_two_visits_from_same_person_are_both_in_report(self):
        with tempfile.TemporaryDirectory() as directory:
            with connect(str(Path(directory) / "quality.sqlite3")) as db:
                db.execute("INSERT INTO groups(group_id, project, token) VALUES (1, 'Магазин', 'secret')")
                messages = [{"id": 1, "date": self.base, "out": 0, "text": "Цена?", "ref": "111111111"},
                            {"id": 2, "date": self.base + 4, "out": 0, "text": "Цена?", "ref": "222222222"}]
                for item in episodes(123, messages, DEFAULT_SETTINGS, self.base):
                    save_observation(db, 1, item, self.base)
                report = rows(db, self.base, self.base + 86400)
                self.assertEqual({row["label_id"] for row in report}, {"111111111", "222222222"})

    def test_sync_is_idempotent_and_late_reply_updates_one_visit(self):
        with tempfile.TemporaryDirectory() as directory:
            with connect(str(Path(directory) / "quality.sqlite3")) as db:
                db.execute("INSERT INTO groups(group_id, project, token) VALUES (1, 'Магазин', 'secret')")
                group = db.execute("SELECT * FROM groups WHERE group_id=1").fetchone()
                class FakeVK:
                    messages = [{"id": 1, "date": 1_700_000_000, "out": 0,
                                 "text": "Начать", "payload": "{}", "ref": "111111111"}]
                    def conversations_since(self, since):
                        return iter([{"conversation": {"peer": {"id": 123}}}])
                    def history_since(self, peer_id, since):
                        return self.messages, False, False
                client = FakeVK()
                sync_group(db, group, self.base, client)
                sync_group(db, group, self.base, client)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)
                client.messages.append({"id": 2, "date": self.base + 7 * 86400,
                                        "out": 0, "text": "Есть в наличии?"})
                sync_group(db, group, self.base, client)
                self.assertEqual(db.execute("SELECT status FROM observations").fetchone()[0], "qualified")


if __name__ == "__main__":
    unittest.main()
