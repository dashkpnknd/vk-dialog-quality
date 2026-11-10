import sqlite3
import time
from typing import Dict, List
from .core import episodes
from .db import save_observation, settings as get_settings
from .report import date_bounds
from .vk import VKClient


def sync_group(db: sqlite3.Connection, group: sqlite3.Row, since: int,
               client: VKClient = None) -> Dict[str, int]:
    config = get_settings(db)
    client = client or VKClient(group["token"], group["group_id"])
    counts = {"scanned": 0, "new": 0, "old": 0, "review": 0}
    now = int(time.time())
    for conversation in client.conversations_since(since):
        peer_id = int(conversation["conversation"]["peer"]["id"])
        counts["scanned"] += 1
        history, has_older, truncated = client.history_since(peer_id, since)
        if not history:
            continue
        items = episodes(peer_id, history, config, since, has_older, truncated)
        if not items:
            counts["old"] += 1
            continue
        for observation in items:
            save_observation(db, group["group_id"], observation, now)
            counts["new"] += 1
            if observation.status == "review":
                counts["review"] += 1
    return counts


def sync_all(db: sqlite3.Connection, days: int = 30) -> List[Dict]:
    since, _ = date_bounds(db, days)
    results = []
    for group in db.execute("SELECT * FROM groups WHERE enabled=1 ORDER BY project, group_id"):
        try:
            counts = sync_group(db, group, since)
            results.append({"project": group["project"], "group_id": group["group_id"], **counts})
        except Exception as exc:
            results.append({"project": group["project"], "group_id": group["group_id"],
                            "error": str(exc)})
    return results
