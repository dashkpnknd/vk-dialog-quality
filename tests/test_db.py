import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from quality.db import SCHEMA_VERSION, connect


class DatabaseTests(unittest.TestCase):
    def test_bot_and_worker_can_start_on_new_database_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            def open_database(_):
                db = connect(path)
                try:
                    return db.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    db.close()
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(list(pool.map(open_database, range(8))), [SCHEMA_VERSION] * 8)

    def test_existing_database_migrates_without_losing_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            db = connect(path)
            db.execute("INSERT INTO groups(group_id,project,token) VALUES (42,'Магазин','secret')")
            db.execute("DROP TABLE sync_jobs")
            db.execute("DROP TABLE report_delivery_state")
            db.execute("PRAGMA user_version=2")
            db.commit()
            db.close()
            db = connect(path)
            try:
                self.assertEqual(db.execute("SELECT project FROM groups WHERE group_id=42").fetchone()[0],
                                 "Магазин")
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 0)
            finally:
                db.close()

    def test_reopening_database_does_not_write_during_active_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            writer = connect(path)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(writer.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("INSERT INTO projects(name) VALUES ('Магазин')")
                reader = connect(path)
                try:
                    self.assertEqual(reader.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
                finally:
                    reader.close()
                writer.commit()
            finally:
                writer.close()


if __name__ == "__main__":
    unittest.main()
