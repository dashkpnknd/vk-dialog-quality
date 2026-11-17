import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from quality.db import connect
from quality.jobs import claim_job, enqueue_groups, work_once


class JobTests(unittest.TestCase):
    def test_repeated_request_during_active_job_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            db = connect(str(Path(directory) / "quality.sqlite3"))
            try:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (1,'Магазин','secret')")
                db.commit()
                enqueue_groups(db)
                def finish(connection, group, since, heartbeat):
                    heartbeat()
                    if runs[0] == 0:
                        enqueue_groups(connection, [1], priority=1)
                    runs[0] += 1
                    return {"project": group["project"], "group_id": group["group_id"]}
                runs = [0]
                with patch("quality.jobs._sync_one", side_effect=finish):
                    self.assertNotIn("error", work_once(db))
                    job = db.execute("SELECT priority,rerun,lease_token FROM sync_jobs").fetchone()
                    self.assertEqual(tuple(job), (1, 0, None))
                    self.assertNotIn("error", work_once(db))
                self.assertEqual(runs[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 0)
            finally:
                db.close()

    def test_failed_job_retries_after_backoff_and_lease_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            db = connect(path)
            try:
                db.execute("INSERT INTO groups(group_id,project,token) VALUES (1,'Магазин','secret')")
                db.commit()
                enqueue_groups(db)
                claimed = claim_job(db)
                self.assertIsNotNone(claimed)
                db.close()
                db = connect(path)
                self.assertIsNone(claim_job(db))
                db.execute("UPDATE sync_jobs SET lease_until=0")
                db.commit()
                with patch("quality.jobs._sync_one", return_value={"group_id": 1, "error": "VK unavailable"}):
                    self.assertIn("error", work_once(db))
                job = db.execute("SELECT lease_until,last_error FROM sync_jobs").fetchone()
                self.assertGreater(job["lease_until"], int(time.time()))
                self.assertEqual(job["last_error"], "VK unavailable")
            finally:
                db.close()

    def test_thousand_groups_use_bounded_concurrent_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "quality.sqlite3")
            db = connect(path)
            try:
                db.executemany("INSERT INTO groups(group_id,project,token) VALUES (?,?,?)",
                               ((group_id, "Магазин " + str(group_id), "secret")
                                for group_id in range(1, 1001)))
                db.commit()
                self.assertEqual(enqueue_groups(db), 1000)
                self.assertEqual(enqueue_groups(db), 1000)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 1000)
                processed = []
                lock = threading.Lock()
                def finish(connection, group, since, heartbeat):
                    with lock:
                        processed.append(group["group_id"])
                    return {"project": group["project"], "group_id": group["group_id"]}
                def worker():
                    connection = connect(path)
                    try:
                        while work_once(connection) is not None:
                            pass
                    finally:
                        connection.close()
                with patch("quality.jobs._sync_one", side_effect=finish):
                    with ThreadPoolExecutor(max_workers=8) as pool:
                        futures = [pool.submit(worker) for _ in range(8)]
                        for future in futures:
                            future.result(timeout=30)
                self.assertEqual(len(processed), 1000)
                self.assertEqual(len(set(processed)), 1000)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0], 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
