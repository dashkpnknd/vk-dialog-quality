import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List
from .core import episodes
from .db import connect, save_observation, settings as get_settings
from .report import date_bounds
from .vk import VKClient


def sync_group(db: sqlite3.Connection, group: sqlite3.Row, since: int,
               client: VKClient = None, progress_callback=None) -> Dict[str, int]:
    config = get_settings(db)
    client = client or VKClient(group["token"], group["group_id"])
    counts = {"scanned": 0, "unchanged": 0, "new": 0, "old": 0, "review": 0, "incomplete": 0}
    now = int(time.time())

    def checkpoint() -> None:
        db.execute("UPDATE group_sync_state SET scanned=?,unchanged=? WHERE group_id=?",
                   (counts["scanned"], counts["unchanged"], group["group_id"]))
        db.commit()
        if progress_callback:
            progress_callback()

    def save_batch(pending: List[tuple]) -> None:
        requests = [(peer_id, cutoff) for peer_id, cutoff, _, _ in pending]
        histories = (client.histories_since(requests) if hasattr(client, "histories_since") else
                     {peer_id: client.history_since(peer_id, cutoff) for peer_id, cutoff in requests})
        for peer_id, cutoff, anchor, marker in pending:
            history, has_older, truncated = histories[peer_id]
            if truncated:
                counts["incomplete"] += 1
                continue
            items = episodes(peer_id, history, config, cutoff, has_older, truncated, anchor) if history else []
            if history and not items:
                counts["old"] += 1
            for observation in items:
                save_observation(db, group["group_id"], observation, now, commit=False)
                counts["new"] += 1
                if observation.status == "review":
                    counts["review"] += 1
            if marker is not None:
                db.execute("INSERT INTO conversation_state(group_id,peer_id,last_message_id,scanned_at) "
                           "VALUES (?,?,?,?) ON CONFLICT(group_id,peer_id) DO UPDATE SET "
                           "last_message_id=excluded.last_message_id,scanned_at=excluded.scanned_at",
                           (group["group_id"], peer_id, marker, now))
        checkpoint()

    pending: List[tuple] = []
    for conversation in client.conversations_since(since):
        peer_id = int(conversation["conversation"]["peer"]["id"])
        counts["scanned"] += 1
        last_message = conversation.get("last_message") or {}
        marker = last_message.get("id") or conversation["conversation"].get("last_message_id")
        marker = int(marker) if marker else None
        if marker is not None:
            prior = db.execute("SELECT last_message_id FROM conversation_state "
                               "WHERE group_id=? AND peer_id=?", (group["group_id"], peer_id)).fetchone()
            if prior and prior["last_message_id"] == marker:
                counts["unchanged"] += 1
                if counts["unchanged"] % 200 == 0:
                    checkpoint()
                continue
        anchor_row = db.execute("""SELECT episode_key, first_at, label_id, label_source
            FROM observations WHERE group_id=? AND peer_id=? AND status!='qualified'
            AND first_at < ? ORDER BY first_at LIMIT 1""",
            (group["group_id"], peer_id, since)).fetchone()
        anchor = dict(anchor_row) if anchor_row else None
        cutoff = min(since, anchor["first_at"]) if anchor else since
        pending.append((peer_id, cutoff, anchor, marker))
        if len(pending) >= 20:
            save_batch(pending)
            pending.clear()
    if pending:
        save_batch(pending)
    return counts


def _sync_one(db: sqlite3.Connection, group: sqlite3.Row, since: int,
              progress_callback=None) -> Dict:
    group_id = group["group_id"]
    started_at = int(time.time())
    db.execute("INSERT INTO group_sync_state(group_id,started_at,completed_at,last_error,running) "
               "VALUES (?,?,NULL,NULL,1) ON CONFLICT(group_id) DO UPDATE SET "
               "started_at=excluded.started_at,last_error=NULL,running=1,scanned=0,unchanged=0",
               (group_id, started_at))
    db.commit()
    try:
        counts = sync_group(db, group, since, progress_callback=progress_callback)
        previous = db.execute("SELECT completed_at FROM group_sync_state WHERE group_id=?",
                              (group_id,)).fetchone()
        completed_at = max(int(time.time()), int(previous["completed_at"] or 0) + 1)
        db.execute("UPDATE group_sync_state SET completed_at=?,last_error=NULL,running=0,scanned=?,unchanged=? "
                   "WHERE group_id=?", (completed_at, counts["scanned"], counts["unchanged"], group_id))
        db.commit()
        return {"project": group["project"], "group_id": group_id, **counts}
    except Exception as exc:
        db.rollback()
        db.execute("UPDATE group_sync_state SET last_error=?,running=0 WHERE group_id=?", (str(exc), group_id))
        db.commit()
        return {"project": group["project"], "group_id": group_id, "error": str(exc)}


def _sync_one_path(path: str, group: Dict, since: int) -> Dict:
    db = connect(path)
    try:
        return _sync_one(db, group, since)
    finally:
        db.close()


def sync_all(db: sqlite3.Connection, days: int = 30, group_ids=None) -> List[Dict]:
    since, _ = date_bounds(db, days)
    groups = list(db.execute("SELECT * FROM groups WHERE enabled=1 ORDER BY project, group_id"))
    if group_ids is not None:
        selected = {int(group_id) for group_id in group_ids}
        groups = [group for group in groups if group["group_id"] in selected]
    if not groups:
        return []
    path = db.execute("PRAGMA database_list").fetchone()["file"]
    workers = max(1, min(12, int(os.environ.get("VK_SYNC_WORKERS", "6"))))
    if not path or workers == 1:
        return [_sync_one(db, group, since) for group in groups]
    results = []
    with ThreadPoolExecutor(max_workers=min(workers, len(groups))) as pool:
        futures = {pool.submit(_sync_one_path, path, dict(group), since): group for group in groups}
        for future in as_completed(futures):
            group = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"project": group["project"], "group_id": group["group_id"],
                                "error": str(exc)})
    return sorted(results, key=lambda item: (item["project"], item["group_id"]))
