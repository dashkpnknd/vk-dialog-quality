import csv
import io
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Tuple

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

from .db import daily_rows, rows, settings


HEADERS = ["Магазин", "ID сообщества", "ID метки", "Всего обращений", "Квалы",
           "Не квалы", "Проверить вручную", "Квалы, %"]
DAILY_HEADERS = ["Дата"] + HEADERS
FIELDS = ["project", "group_id", "label_id", "total", "qualified",
          "not_qualified", "review", "percent"]


def date_bounds(db: sqlite3.Connection, days: int) -> Tuple[int, int]:
    zone = ZoneInfo(settings(db)["timezone"])
    end = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days)
    return int(start.timestamp()), int(end.timestamp())


def report_data(db: sqlite3.Connection, days: int = 30,
                complete_only: bool = False, project_names=None,
                include_daily: bool = True) -> Tuple[List[Dict], List[Dict]]:
    start, end = date_bounds(db, days)
    summary = rows(db, start, end, project_names)
    daily = daily_rows(db, start, end, settings(db)["timezone"], project_names) if include_daily else []
    if complete_only:
        completed = {row["group_id"] for row in db.execute(
            "SELECT g.group_id FROM groups g JOIN group_sync_state s ON s.group_id=g.group_id "
            "WHERE g.enabled=1 AND s.completed_at IS NOT NULL")}
        summary = [item for item in summary if item["group_id"] in completed]
        daily = [item for item in daily if item["group_id"] in completed]
    return summary, daily


def csv_text(items: List[Dict[str, Any]], daily: bool = False) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(DAILY_HEADERS if daily else HEADERS)
    for item in items:
        values = ([item["day"]] if daily else []) + [item[field] for field in FIELDS]
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                         for value in values])
    return buffer.getvalue()


def _request(method: str, url: str, credentials, **kwargs):
    if not credentials.valid:
        credentials.refresh(Request())
    for attempt in range(4):
        response = requests.request(method, url, headers={"Authorization": "Bearer " + credentials.token},
                                    timeout=60, **kwargs)
        if response.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 ** attempt)
            continue
        response.raise_for_status()
        return response.json() if response.content else {}
    raise RuntimeError("Google Sheets request retries exhausted")


def publish_google(db: sqlite3.Connection, days: int = 30) -> str:
    credential_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
    if not credential_file or not spreadsheet_id:
        raise ValueError("Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SPREADSHEET_ID")
    credentials = service_account.Credentials.from_service_account_file(
        credential_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    base = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id
    info = _request("GET", base + "?fields=sheets.properties.title", credentials)
    existing = {item["properties"]["title"] for item in info.get("sheets", [])}
    missing = [name for name in ("Сводка", "По дням") if name not in existing]
    if missing:
        _request("POST", base + ":batchUpdate", credentials,
                 json={"requests": [{"addSheet": {"properties": {"title": name}}} for name in missing]})
    summary, daily = report_data(db, days, complete_only=True)
    values = [
        ("Сводка", [HEADERS] + [[item[field] for field in FIELDS] for item in summary]),
        ("По дням", [DAILY_HEADERS] + [[item["day"]] + [item[field] for field in FIELDS] for item in daily]),
    ]
    for title, table in values:
        clear_range = requests.utils.quote("'" + title + "'!A:Z", safe="")
        _request("POST", base + "/values/" + clear_range + ":clear", credentials, json={})
        for start in range(0, len(table), 5000):
            chunks = []
            for offset in range(start, min(start + 5000, len(table)), 1000):
                chunks.append({"range": "'{}'!A{}".format(title, offset + 1),
                               "majorDimension": "ROWS", "values": table[offset:offset + 1000]})
            _request("POST", base + "/values:batchUpdate", credentials,
                     json={"valueInputOption": "RAW", "data": chunks})
    return "https://docs.google.com/spreadsheets/d/" + spreadsheet_id
