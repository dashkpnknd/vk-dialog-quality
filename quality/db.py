import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List
from datetime import datetime
from zoneinfo import ZoneInfo
from .core import DEFAULT_SETTINGS, Observation


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY,
    project TEXT NOT NULL,
    token TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS observations (
    group_id INTEGER NOT NULL,
    peer_id INTEGER NOT NULL,
    episode_key TEXT NOT NULL,
    first_at INTEGER NOT NULL,
    label_id TEXT,
    label_source TEXT,
    qualified INTEGER NOT NULL,
    inbound_count INTEGER NOT NULL,
    button_count INTEGER NOT NULL,
    uncertain_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, peer_id, episode_key)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY, subscribed_at INTEGER NOT NULL);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=30000")
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO projects(name) SELECT DISTINCT project FROM groups")
    for key, value in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
    db.commit()
    return db


def settings(db: sqlite3.Connection) -> Dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM settings")}


def save_observation(db: sqlite3.Connection, group_id: int, item: Observation, observed_at: int) -> None:
    db.execute("""INSERT INTO observations
        (group_id, peer_id, episode_key, first_at, label_id, label_source, qualified, inbound_count,
         button_count, uncertain_count, status, observed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(group_id, peer_id, episode_key) DO UPDATE SET
          first_at=excluded.first_at, label_id=excluded.label_id,
          label_source=excluded.label_source, qualified=excluded.qualified,
          inbound_count=excluded.inbound_count, button_count=excluded.button_count,
          uncertain_count=excluded.uncertain_count, status=excluded.status,
          observed_at=excluded.observed_at""",
        (group_id, item.peer_id, item.episode_key, item.first_at, item.label_id, item.label_source,
         int(item.qualified), item.inbound_count, item.button_count,
         item.uncertain_count, item.status, observed_at))
    db.commit()


def rows(db: sqlite3.Connection, since: int, until: int) -> List[Dict[str, Any]]:
    query = """SELECT g.project, o.group_id, COALESCE(o.label_id, 'Без метки') AS label_id,
        COUNT(*) AS total, SUM(o.status='qualified') AS qualified,
        SUM(o.status='not_qualified') AS not_qualified,
        SUM(o.status='review') AS review
        FROM observations o JOIN groups g ON g.group_id=o.group_id
        WHERE o.first_at >= ? AND o.first_at < ?
        GROUP BY g.project, o.group_id, COALESCE(o.label_id, 'Без метки')
        ORDER BY g.project, o.group_id, qualified * 1.0 / total DESC, label_id"""
    result = []
    for row in db.execute(query, (since, until)):
        item = dict(row)
        item["percent"] = round(100 * item["qualified"] / item["total"], 1) if item["total"] else 0
        result.append(item)
    return result


def daily_rows(db: sqlite3.Connection, since: int, until: int, timezone: str) -> List[Dict[str, Any]]:
    zone = ZoneInfo(timezone)
    grouped: Dict[tuple, Dict[str, Any]] = {}
    query = """SELECT g.project, o.group_id, o.label_id, o.first_at, o.status
        FROM observations o JOIN groups g ON g.group_id=o.group_id
        WHERE o.first_at >= ? AND o.first_at < ?"""
    for row in db.execute(query, (since, until)):
        day = datetime.fromtimestamp(row["first_at"], zone).strftime("%Y-%m-%d")
        label = row["label_id"] or "Без метки"
        key = (day, row["project"], row["group_id"], label)
        item = grouped.setdefault(key, {"day": day, "project": row["project"],
                                        "group_id": row["group_id"], "label_id": label,
                                        "total": 0, "qualified": 0, "not_qualified": 0,
                                        "review": 0})
        item["total"] += 1
        item[row["status"]] += 1
    result = list(grouped.values())
    for item in result:
        item["percent"] = round(100 * item["qualified"] / item["total"], 1)
    result.sort(key=lambda item: (item["day"], item["project"], item["group_id"], -item["percent"], item["label_id"]))
    return result
