"""City-based Telegram forum reports."""

from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import rows, settings
from .report import date_bounds, report_data


def city_for_project(db: sqlite3.Connection, project: str) -> str:
    record = db.execute("SELECT city FROM projects WHERE name=?", (project,)).fetchone()
    if record and record["city"] and record["city"].strip():
        return record["city"].strip()
    return project.rsplit("|", 1)[-1].strip() or project


def _total(items: List[Dict[str, Any]]) -> Dict[str, int]:
    return {field: sum(int(row[field]) for row in items)
            for field in ("total", "qualified", "not_qualified", "review")}


def _percent(qualified: int, total: int) -> str:
    return ("{:.1f}".format(100 * qualified / total) if total else "0.0").replace(".", ",")


def format_project_report(city: str, project: str, yesterday: List[Dict[str, Any]],
                          period: List[Dict[str, Any]], report_date: str) -> List[str]:
    """Return self-contained HTML messages, each below Telegram's text limit."""
    daily = _total(yesterday)
    month = _total(period)
    header = "📊 <b>VK КВАЛЫ · {}</b>\n🏪 {}\n━━━━━━━━━━━━━━━━━━━━\n".format(
        escape(city.upper()), escape(project))
    lines = [
        "📅 <b>Вчера ({})</b>".format(report_date),
        "Обращений: <b>{}</b> · квалов: <b>{}</b> · не квалов: <b>{}</b>".format(
            daily["total"], daily["qualified"], daily["not_qualified"]),
        "Доля квалов: <b>{}%</b>".format(_percent(daily["qualified"], daily["total"])),
        "",
        "📆 <b>Последние 30 дней</b>",
        "Обращений: <b>{}</b> · квалов: <b>{}</b> · не квалов: <b>{}</b>".format(
            month["total"], month["qualified"], month["not_qualified"]),
        "Доля квалов: <b>{}%</b>".format(_percent(month["qualified"], month["total"])),
    ]
    if month["review"]:
        lines.append("⚠️ Проверить вручную: <b>{}</b>".format(month["review"]))
    labels: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in period:
        labels[item["label_id"]].append(item)
    campaign_ids = sorted((key for key in labels if key != "Без метки"),
                          key=lambda key: -_total(labels[key])["total"])
    if campaign_ids:
        lines.extend(("", "🎯 <b>Рекламные ID · 30 дней</b>"))
        for label in campaign_ids:
            item = _total(labels[label])
            lines.append("• <code>{}</code> — {}/{} · {}%".format(
                escape(label), item["qualified"], item["total"],
                _percent(item["qualified"], item["total"])))
    if "Без метки" in labels:
        item = _total(labels["Без метки"])
        lines.extend(("", "▫️ Без метки — {}/{} · {}%".format(
            item["qualified"], item["total"], _percent(item["qualified"], item["total"]))))
    lines.extend(("", "Поздний ответ клиента обновит результат обращения."))
    parts: List[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 3700 and current != header:
            parts.append(current.rstrip())
            current = header + "📋 <b>Продолжение</b>\n"
        current += line + "\n"
    parts.append(current.rstrip())
    return parts


def publish_city_reports(db: sqlite3.Connection, api: Any,
                         chat_id: Optional[int] = None) -> Dict[str, int]:
    """Create missing city topics and post one report per project with data."""
    chats = list(db.execute("SELECT chat_id FROM report_chats WHERE chat_id=?", (chat_id,))) if chat_id else \
        list(db.execute("SELECT chat_id FROM report_chats ORDER BY connected_at"))
    period, _ = report_data(db, 30)
    yesterday_start, _ = date_bounds(db, 2)
    today_start, _ = date_bounds(db, 1)
    daily = rows(db, yesterday_start, today_start)
    zone = ZoneInfo(settings(db)["timezone"])
    date_text = (datetime.fromtimestamp(today_start, zone) - timedelta(days=1)).strftime("%d.%m")
    projects = sorted({item["project"] for item in period})
    result = {"topics": 0, "reports": 0}
    for chat in chats:
        target = chat["chat_id"]
        for project in projects:
            city = city_for_project(db, project)
            stored = db.execute("SELECT thread_id FROM city_topics WHERE chat_id=? AND city=?",
                                (target, city)).fetchone()
            if stored:
                thread_id = stored["thread_id"]
            else:
                thread_id = api.create_topic(target, city)
                db.execute("INSERT INTO city_topics(chat_id,city,thread_id) VALUES (?,?,?)",
                           (target, city, thread_id))
                db.commit()
                result["topics"] += 1
            for text in format_project_report(city, project,
                                              [item for item in daily if item["project"] == project],
                                              [item for item in period if item["project"] == project], date_text):
                api.send(target, text, thread_id=thread_id, parse_mode="HTML")
                result["reports"] += 1
    return result
