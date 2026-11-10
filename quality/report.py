import csv
import io
import os
import sqlite3
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


def report_data(db: sqlite3.Connection, days: int = 30) -> Tuple[List[Dict], List[Dict]]:
    start, end = date_bounds(db, days)
    return rows(db, start, end), daily_rows(db, start, end, settings(db)["timezone"])


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
    response = requests.request(method, url, headers={"Authorization": "Bearer " + credentials.token},
                                timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else {}


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
    summary, daily = report_data(db, days)
    values = [
        ("Сводка", [HEADERS] + [[item[field] for field in FIELDS] for item in summary]),
        ("По дням", [DAILY_HEADERS] + [[item["day"]] + [item[field] for field in FIELDS] for item in daily]),
    ]
    for title, table in values:
        clear_range = requests.utils.quote("'" + title + "'!A:Z", safe="")
        _request("POST", base + "/values/" + clear_range + ":clear", credentials, json={})
        encoded = requests.utils.quote("'" + title + "'!A1", safe="")
        _request("PUT", base + "/values/" + encoded + "?valueInputOption=RAW", credentials,
                 json={"range": "'" + title + "'!A1", "majorDimension": "ROWS", "values": table})
    return "https://docs.google.com/spreadsheets/d/" + spreadsheet_id
