import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from quality.db import connect
from quality.report import report_data
from quality.sync import sync_all, sync_group


class SyncTests(unittest.TestCase):
    def test_unchanged_conversation_skips_history_and_new_reply_refreshes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "quality.sqlite3"))
            try:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'Магазин','secret')")
                db.commit()
                group = db.execute("SELECT * FROM groups WHERE group_id=42").fetchone()
                class Client:
                    marker = 1
                    reads = 0
                    def conversations_since(self, since):
                        return iter([{"conversation": {"peer": {"id": 123}},
                                      "last_message": {"id": self.marker}}])
                    def history_since(self, peer_id, since):
                        self.reads += 1
                        return ([{"id": 1, "date": since + 1, "out": 0,
                                 "text": "Начать", "ref": "111111111"}] +
                                ([{"id": 2, "date": since + 2, "out": 0, "text": "Цена?"}]
                                 if self.marker == 2 else [])), False, False
                client = Client()
                since = int(time.time()) - 86400
                sync_group(db, group, since, client)
                second = sync_group(db, group, since, client)
                self.assertEqual(client.reads, 1)
                self.assertEqual(second["unchanged"], 1)
                client.marker = 2
                sync_group(db, group, since, client)
                self.assertEqual(client.reads, 2)
                self.assertEqual(db.execute("SELECT status FROM observations").fetchone()[0], "qualified")
            finally:
                db.close()

    def test_groups_run_concurrently_and_only_completed_groups_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "quality.sqlite3"))
            try:
                for group_id in (1, 2, 3):
                    db.execute("INSERT INTO groups(group_id,project,token) VALUES (?,?,?)",
                               (group_id, "Магазин " + str(group_id), "secret"))
                db.commit()
                barrier = threading.Barrier(3)
                class Client:
                    def __init__(self, token, group_id):
                        self.group_id = group_id
                    def conversations_since(self, since):
                        barrier.wait(timeout=5)
                        return iter([{"conversation": {"peer": {"id": self.group_id + 100}},
                                      "last_message": {"id": 10}}])
                    def histories_since(self, peers):
                        peer_id, since = peers[0]
                        return {peer_id: ([{"id": 10, "date": since + 1, "out": 0,
                                           "text": "Цена?", "ref": "111111111"}], False, False)}
                with patch("quality.sync.VKClient", Client), patch.dict(os.environ, {"VK_SYNC_WORKERS": "3"}):
                    result = sync_all(db, 30)
                self.assertEqual(len(result), 3)
                self.assertTrue(all("error" not in item for item in result))
                self.assertEqual(len(report_data(db, 30, complete_only=True)[0]), 3)
                db.execute("UPDATE group_sync_state SET completed_at=NULL WHERE group_id=2")
                db.commit()
                self.assertEqual(len(report_data(db, 30, complete_only=True)[0]), 2)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
