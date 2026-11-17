"""Persistent per-community VK collection queue."""

import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

from .db import connect
from .report import date_bounds
from .sync import _sync_one


LEASE_SECONDS = 600


def enqueue_groups(db: sqlite3.Connection, group_ids: Optional[Iterable[int]] = None,
                   priority: int = 0) -> int:
    selected = None if group_ids is None else {int(group_id) for group_id in group_ids}
    groups = [row["group_id"] for row in db.execute("SELECT group_id FROM groups WHERE enabled=1")
              if selected is None or row["group_id"] in selected]
    now = int(time.time())
    for group_id in groups:
        db.execute("INSERT INTO sync_jobs(group_id,requested_at,priority) VALUES (?,?,?) "
                   "ON CONFLICT(group_id) DO UPDATE SET "
                   "requested_at=excluded.requested_at,priority=MAX(priority,excluded.priority),"
                   "rerun=CASE WHEN sync_jobs.lease_token IS NOT NULL THEN 1 ELSE sync_jobs.rerun END",
                   (group_id, now, priority))
    db.commit()
    return len(groups)


def claim_job(db: sqlite3.Connection):
    now = int(time.time())
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT j.group_id FROM sync_jobs j JOIN groups g ON g.group_id=j.group_id "
                         "WHERE g.enabled=1 AND j.lease_until<=? "
                         "ORDER BY j.priority DESC,j.requested_at,j.group_id LIMIT 1", (now,)).fetchone()
        if row is None:
            db.commit()
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE sync_jobs SET lease_until=?,lease_token=?,attempts=attempts+1 "
                   "WHERE group_id=?", (now + LEASE_SECONDS, token, row["group_id"]))
        db.commit()
        return row["group_id"], token
    except Exception:
        db.rollback()
        raise


def work_once(db: sqlite3.Connection) -> Optional[dict]:
    claimed = claim_job(db)
    if claimed is None:
        return None
    group_id, token = claimed
    group = db.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if group is None:
        db.execute("DELETE FROM sync_jobs WHERE group_id=? AND lease_token=?", (group_id, token))
        db.commit()
        return {"group_id": group_id, "error": "Group removed"}

    def heartbeat() -> None:
        updated = db.execute("UPDATE sync_jobs SET lease_until=? WHERE group_id=? AND lease_token=?",
                             (int(time.time()) + LEASE_SECONDS, group_id, token)).rowcount
        db.commit()
        if not updated:
            raise RuntimeError("Sync job lease lost")

    since, _ = date_bounds(db, 30)
    try:
        result = _sync_one(db, group, since, heartbeat)
    except Exception as exc:
        db.rollback()
        result = {"project": group["project"], "group_id": group_id, "error": str(exc)}
    if "error" in result:
        attempts = db.execute("SELECT attempts FROM sync_jobs WHERE group_id=? AND lease_token=?",
                              (group_id, token)).fetchone()
        backoff = min(3600, 60 * (2 ** min((attempts["attempts"] if attempts else 1) - 1, 6)))
        db.execute("UPDATE sync_jobs SET lease_until=?,lease_token=NULL,last_error=?,priority=0 "
                   "WHERE group_id=? AND lease_token=?",
                   (int(time.time()) + backoff, result["error"], group_id, token))
    else:
        db.execute("DELETE FROM sync_jobs WHERE group_id=? AND lease_token=? AND rerun=0",
                   (group_id, token))
        db.execute("UPDATE sync_jobs SET rerun=0,lease_token=NULL,lease_until=0,attempts=0 "
                   "WHERE group_id=? AND lease_token=?", (group_id, token))
    db.commit()
    return result


def run_worker(db_path: str) -> None:
    workers = max(1, min(32, int(os.environ.get("VK_SYNC_WORKERS", "8"))))
    initial = connect(db_path)
    initial.close()

    def loop() -> None:
        db = connect(db_path)
        try:
            while True:
                try:
                    result = work_once(db)
                    if result is None:
                        time.sleep(5)
                    else:
                        print("VK sync", result, flush=True)
                except Exception as exc:
                    db.rollback()
                    print("VK worker error", str(exc), flush=True)
                    time.sleep(5)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            pool.submit(loop)
