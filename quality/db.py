import fcntl
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
CREATE TABLE IF NOT EXISTS authorized_users (user_id INTEGER PRIMARY KEY, authorized_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS auth_attempts (
    user_id INTEGER PRIMARY KEY,
    window_started_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS report_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    connected_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS general_panels (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS city_topics (
    chat_id INTEGER NOT NULL,
    city TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, city)
);
CREATE TABLE IF NOT EXISTS project_topics (
    chat_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    thread_id INTEGER NOT NULL,
    PRIMARY KEY (chat_id, project)
);
CREATE TABLE IF NOT EXISTS conversation_state (
    group_id INTEGER NOT NULL,
    peer_id INTEGER NOT NULL,
    last_message_id INTEGER NOT NULL,
    scanned_at INTEGER NOT NULL,
    PRIMARY KEY (group_id, peer_id)
);
CREATE TABLE IF NOT EXISTS group_sync_state (
    group_id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    last_error TEXT,
    running INTEGER NOT NULL DEFAULT 0,
    scanned INTEGER NOT NULL DEFAULT 0,
    unchanged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_jobs (
    group_id INTEGER PRIMARY KEY,
    requested_at INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    rerun INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_jobs_ready ON sync_jobs(lease_until,priority,requested_at);
CREATE INDEX IF NOT EXISTS idx_observations_first_at ON observations(first_at,group_id);
CREATE INDEX IF NOT EXISTS idx_observations_group_first_at ON observations(group_id,first_at);
CREATE INDEX IF NOT EXISTS idx_observations_label_first_at ON observations(label_id,first_at);
CREATE INDEX IF NOT EXISTS idx_groups_project ON groups(project);
CREATE TABLE IF NOT EXISTS report_delivery_state (
    group_id INTEGER PRIMARY KEY,
    published_at INTEGER NOT NULL DEFAULT 0,
    report_day TEXT
);
"""

SCHEMA_VERSION = 4


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    os.chmod(path, 0o600)
    db.row_factory = sqlite3.Row
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        lock_fd = os.open(path + ".migrate.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
                if "city" not in {row["name"] for row in db.execute("PRAGMA table_info(projects)")}:
                    db.execute("ALTER TABLE projects ADD COLUMN city TEXT")
                db.execute("INSERT OR IGNORE INTO projects(name) SELECT DISTINCT project FROM groups")
                for key, value in DEFAULT_SETTINGS.items():
                    db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, json.dumps(value)))
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.commit()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    return db


def settings(db: sqlite3.Connection) -> Dict[str, Any]:
    return {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key, value FROM settings")}


def save_observation(db: sqlite3.Connection, group_id: int, item: Observation,
                     observed_at: int, commit: bool = True) -> None:
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
    if commit:
        db.commit()


def rows(db: sqlite3.Connection, since: int, until: int,
         project_names=None) -> List[Dict[str, Any]]:
    projects = sorted(project_names) if project_names is not None else None
    if projects is not None and not projects:
        return []
    project_filter = (" AND g.project IN ({})".format(",".join("?" for _ in projects))
                      if projects is not None else "")
    query = """SELECT g.project, o.group_id, COALESCE(o.label_id, 'Без метки') AS label_id,
        COUNT(*) AS total, SUM(o.status='qualified') AS qualified,
        SUM(o.status='not_qualified') AS not_qualified,
        SUM(o.status='review') AS review
        FROM observations o JOIN groups g ON g.group_id=o.group_id
        WHERE o.first_at >= ? AND o.first_at < ?""" + project_filter + """
        GROUP BY g.project, o.group_id, COALESCE(o.label_id, 'Без метки')
        ORDER BY g.project, o.group_id, qualified * 1.0 / total DESC, label_id"""
    result = []
    for row in db.execute(query, (since, until, *(projects or []))):
        item = dict(row)
        item["percent"] = round(100 * item["qualified"] / item["total"], 1) if item["total"] else 0
        result.append(item)
    return result


def daily_rows(db: sqlite3.Connection, since: int, until: int, timezone: str,
               project_names=None) -> List[Dict[str, Any]]:
    projects = sorted(project_names) if project_names is not None else None
    if projects is not None and not projects:
        return []
    project_filter = (" AND g.project IN ({})".format(",".join("?" for _ in projects))
                      if projects is not None else "")
    zone = ZoneInfo(timezone)
    grouped: Dict[tuple, Dict[str, Any]] = {}
    query = """SELECT g.project, o.group_id, o.label_id, o.first_at, o.status
        FROM observations o JOIN groups g ON g.group_id=o.group_id
        WHERE o.first_at >= ? AND o.first_at < ?""" + project_filter
    for row in db.execute(query, (since, until, *(projects or []))):
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
