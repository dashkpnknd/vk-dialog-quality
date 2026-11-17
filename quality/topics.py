"""Compact VK quality reports in a separate Telegram topic per store."""

from collections import defaultdict
from datetime import datetime
from html import escape
import sqlite3
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .db import settings
from .report import date_bounds, report_data


def city_for_project(db: sqlite3.Connection, project: str) -> str:
    record = db.execute("SELECT city FROM projects WHERE name=?", (project,)).fetchone()
    if record and record["city"] and record["city"].strip():
        return record["city"].strip()
    return project.rsplit("|", 1)[-1].strip() or project


def topic_title(db: sqlite3.Connection, project: str) -> str:
    city = city_for_project(db, project)
    title = project if city.casefold() in project.casefold() else "{} · {}".format(city, project)
    return title[:128]


def _total(items: List[Dict[str, Any]]) -> Dict[str, int]:
    return {field: sum(int(row[field]) for row in items)
            for field in ("total", "qualified", "not_qualified", "review")}


def _percent(qualified: int, total: int) -> str:
    return ("{:.1f}".format(100 * qualified / total) if total else "0.0").replace(".", ",")


def report_date_range(db: sqlite3.Connection, days: int = 30) -> str:
    start, end = date_bounds(db, days)
    zone = ZoneInfo(settings(db)["timezone"])
    return "{}–{}".format(datetime.fromtimestamp(start, zone).strftime("%d.%m"),
                          datetime.fromtimestamp(end - 1, zone).strftime("%d.%m"))


def format_project_report(project: str, period: List[Dict[str, Any]],
                          date_range: str) -> List[str]:
    """Return compact, self-contained HTML messages below Telegram's limit."""
    month = _total(period)
    header = "📊 <b>VK КВАЛЫ</b>\n<b>{}</b>\nПоследние 30 дней: {}\n━━━━━━━━━━━━━━━━━━━━\n".format(
        escape(project), escape(date_range))
    lines = [
        "👥 Обращения: <b>{}</b>".format(month["total"]),
        "✅ Квалы: <b>{}</b> (<b>{}%</b>)".format(
            month["qualified"], _percent(month["qualified"], month["total"])),
        "❌ Не квалы: <b>{}</b>".format(month["not_qualified"]),
    ]
    if month["review"]:
        lines.append("⚠️ На проверке: <b>{}</b>".format(month["review"]))
    labels: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in period:
        labels[item["label_id"]].append(item)
    campaign_ids = sorted((key for key in labels if key != "Без метки"),
                          key=lambda key: (-_total(labels[key])["total"], key))
    if labels:
        lines.extend(("", "🎯 <b>Рекламные ID</b>", "Квалы из всех обращений:"))
    for label in campaign_ids:
        item = _total(labels[label])
        lines.append("• <code>{}</code> — <b>{} из {}</b> ({}%)".format(
            escape(label), item["qualified"], item["total"],
            _percent(item["qualified"], item["total"])))
    if "Без метки" in labels:
        item = _total(labels["Без метки"])
        lines.append("• Без метки — <b>{} из {}</b> ({}%)".format(
            item["qualified"], item["total"], _percent(item["qualified"], item["total"])))
    parts: List[str] = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 3700 and current != header:
            parts.append(current.rstrip())
            current = header + "📋 <b>Продолжение</b>\n"
        current += line + "\n"
    parts.append(current.rstrip())
    return parts


def publish_project_reports(db: sqlite3.Connection, api: Any,
                            chat_id: Optional[int] = None,
                            project_names=None) -> Dict[str, int]:
    """Reuse existing topics, renaming old city topics to store names."""
    chats = list(db.execute("SELECT chat_id FROM report_chats WHERE chat_id=?", (chat_id,))) if chat_id else \
        list(db.execute("SELECT chat_id FROM report_chats ORDER BY connected_at"))
    period, _ = report_data(db, 30, complete_only=True,
                            project_names=project_names, include_daily=False)
    date_range = report_date_range(db)
    projects = sorted({item["project"] for item in period})
    if project_names is not None:
        projects = [project for project in projects if project in project_names]
    result = {"topics": 0, "reports": 0}
    for chat in chats:
        target = chat["chat_id"]
        for project in projects:
            title = topic_title(db, project)
            stored = db.execute("SELECT thread_id FROM project_topics WHERE chat_id=? AND project=?",
                                (target, project)).fetchone()
            if stored:
                thread_id = stored["thread_id"]
            else:
                city = city_for_project(db, project)
                legacy = db.execute("SELECT thread_id FROM city_topics WHERE chat_id=? AND city=?",
                                    (target, city)).fetchone()
                already_used = legacy and db.execute(
                    "SELECT 1 FROM project_topics WHERE chat_id=? AND thread_id=?",
                    (target, legacy["thread_id"])).fetchone()
                if legacy and not already_used:
                    thread_id = legacy["thread_id"]
                    api.edit_topic(target, thread_id, title)
                else:
                    thread_id = api.create_topic(target, title)
                    result["topics"] += 1
                db.execute("INSERT INTO project_topics(chat_id,project,thread_id) VALUES (?,?,?)",
                           (target, project, thread_id))
                db.commit()
            for text in format_project_report(
                    project, [item for item in period if item["project"] == project], date_range):
                api.send(target, text, thread_id=thread_id, parse_mode="HTML")
                result["reports"] += 1
    return result
