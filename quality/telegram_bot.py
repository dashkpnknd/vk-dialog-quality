"""Small administrator-only Telegram interface for the VK report."""

from datetime import datetime
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from .db import connect, settings as get_settings
from .report import csv_text, report_data, publish_google
from .sync import sync_all


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN")
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method: str, **params: Any) -> Any:
        try:
            response = requests.post(self.base + method, json=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Do not include the request URL: it contains the bot token.
            raise TelegramError("Telegram request failed") from None
        if not payload.get("ok"):
            raise TelegramError("Telegram API error: " + str(payload.get("description", "unknown")))
        return payload.get("result")

    def send(self, chat_id: int, text: str, keyboard: bool = False) -> None:
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if keyboard:
            params["reply_markup"] = {"keyboard": [
                [{"text": "📊 Отчёт"}, {"text": "🔄 Обновить"}],
                [{"text": "🏪 Проекты"}, {"text": "➕ Проект"}, {"text": "➕ Группа"}],
                [{"text": "📄 Таблица"}, {"text": "ℹ️ Помощь"}]], "resize_keyboard": True}
        self.call("sendMessage", **params)

    def delete(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def send_document(self, chat_id: int, filename: str, content: str, caption: str) -> None:
        try:
            response = requests.post(self.base + "sendDocument",
                                     data={"chat_id": chat_id, "caption": caption},
                                     files={"document": (filename, ("\ufeff" + content).encode("utf-8"), "text/csv")},
                                     timeout=45)
            response.raise_for_status()
            if not response.json().get("ok"):
                raise TelegramError("Telegram rejected the report file")
        except (requests.RequestException, ValueError):
            raise TelegramError("Could not send the report file") from None


def _get_setting(db: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set_setting(db: sqlite3.Connection, key: str, value: Any) -> None:
    db.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
    db.commit()


def summary(db: sqlite3.Connection, days: int = 30) -> str:
    rows, _ = report_data(db, days)
    if not rows:
        return "За выбранный период данных пока нет. Добавьте сообщества и запустите /sync."
    total = sum(row["total"] for row in rows)
    qualified = sum(row["qualified"] for row in rows)
    review = sum(row["review"] for row in rows)
    unlabeled = sum(row["total"] for row in rows if row["label_id"] == "Без метки")
    projects: Dict[str, Dict[str, int]] = {}
    for row in rows:
        item = projects.setdefault(row["project"], {"total": 0, "qualified": 0})
        item["total"] += row["total"]
        item["qualified"] += row["qualified"]
    lines = ["Отчёт за {} дн.".format(days),
             "Диалогов: {} · квалов: {} ({}%)".format(total, qualified, round(100 * qualified / total, 1)),
             "Без метки: {} · проверить: {}".format(unlabeled, review), ""]
    for name, item in sorted(projects.items(), key=lambda pair: (-pair[1]["total"], pair[0]))[:30]:
        lines.append("{}: {} из {} ({}%)".format(name, item["qualified"], item["total"],
                                                 round(100 * item["qualified"] / item["total"], 1)))
    if len(projects) > 30:
        lines.append("Ещё {} магазинов — в Google-таблице.".format(len(projects) - 30))
    lines.append("\nРезультат пересчитывается, если клиент ответит позже.")
    if review:
        lines.append("Часть обращений требует проверки из-за кнопок бота.")
    spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
    if spreadsheet_id:
        lines.append("\nТаблица: https://docs.google.com/spreadsheets/d/" + spreadsheet_id)
    return "\n".join(lines)


def group_summary(db: sqlite3.Connection, group_id: int) -> str:
    group = db.execute("SELECT project FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if group is None:
        return "Такое сообщество не добавлено. Команда /groups покажет список."
    rows, _ = report_data(db, 30)
    group_rows = [row for row in rows if row["group_id"] == group_id]
    if not group_rows:
        return "{}: за 30 дней данных пока нет.".format(group["project"])
    lines = ["{} · ID {} · 30 дней".format(group["project"], group_id)]
    for row in group_rows:
        lines.append("{}: {} из {} ({}%) · проверка {}".format(
            row["label_id"], row["qualified"], row["total"], row["percent"], row["review"]))
    return "\n".join(lines)


class ReportBot:
    def __init__(self, db_path: str, api: Optional[TelegramAPI] = None):
        self.db_path = db_path
        self.api = api or TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        self.sync_lock = threading.Lock()
        self.status = "Сбор ещё не запускался в этой сессии."
        self.pending: Dict[int, str] = {}

    def _send_long(self, chat_id: int, text: str) -> None:
        part = ""
        for line in text.splitlines():
            if len(part) + len(line) + 1 > 3500 and part:
                self.api.send(chat_id, part)
                part = ""
            part += line + "\n"
        if part:
            self.api.send(chat_id, part)

    def _send_subscribers(self, text: str) -> None:
        with connect(self.db_path) as db:
            subscribers = [row["chat_id"] for row in db.execute("SELECT chat_id FROM subscribers")]
        for chat_id in subscribers:
            try:
                self.api.send(chat_id, text)
            except TelegramError:
                pass

    def _run_sync(self, reply_to: Optional[int]) -> None:
        try:
            with connect(self.db_path) as db:
                result = sync_all(db)
                errors = [item for item in result if "error" in item]
                if not result:
                    text = "Сообщества пока не добавлены."
                else:
                    text = "Сбор завершён: {} сообществ, {} ошибок.".format(len(result), len(errors))
                    if errors:
                        text += "\n" + "\n".join("{}: {}".format(item["project"], item["error"]) for item in errors[:5])
                    if (not errors and os.environ.get("GOOGLE_SPREADSHEET_ID") and
                            os.path.isfile(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""))):
                        try:
                            publish_google(db)
                            text += "\nGoogle-таблица обновлена."
                        except Exception as exc:
                            text += "\nОшибка записи в Google-таблицу: " + str(exc)
                    text += "\n\n" + summary(db, 30)
            self.status = text.split("\n", 1)[0]
            if reply_to is None:
                self._send_subscribers(text)
            else:
                self.api.send(reply_to, text)
        except Exception as exc:
            self.status = "Ошибка сбора: " + str(exc)
            if reply_to is None:
                self._send_subscribers(self.status)
            else:
                self.api.send(reply_to, self.status)
        finally:
            self.sync_lock.release()

    def start_sync(self, reply_to: Optional[int]) -> bool:
        if not self.sync_lock.acquire(blocking=False):
            return False
        self.status = "Сбор выполняется."
        threading.Thread(target=self._run_sync, args=(reply_to,), daemon=True).start()
        return True

    def handle(self, update: Dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or chat.get("type") != "private":
            return
        text = str(message.get("text") or "").strip()
        buttons = {"📊 Отчёт": "/report", "🔄 Обновить": "/sync", "🏪 Проекты": "/groups",
                   "➕ Проект": "/newproject", "➕ Группа": "/newgroup", "📄 Таблица": "/sheet",
                   "ℹ️ Помощь": "/help"}
        text = buttons.get(text, text)
        if self.pending.get(chat_id) and not text.startswith("/"):
            action = self.pending.pop(chat_id)
            if action == "project":
                name = text[:120].strip()
                if not name:
                    self.api.send(chat_id, "Название проекта пустое. Нажмите «➕ Проект» и попробуйте ещё раз.")
                    return
                with connect(self.db_path) as db:
                    db.execute("INSERT OR IGNORE INTO projects(name) VALUES (?)", (name,))
                    db.commit()
                self.api.send(chat_id, "Проект «{}» добавлен. Теперь подключите группу кнопкой «➕ Группа».".format(name), keyboard=True)
                return
            if action == "group":
                try:
                    self.api.delete(chat_id, int(message["message_id"]))
                except (KeyError, TelegramError):
                    self.api.send(chat_id, "Не удалось удалить сообщение с токеном. Удалите его вручную из чата.")
                parts = [part.strip() for part in text.split("|", 2)]
                if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2]:
                    self.api.send(chat_id, "Формат: название проекта | ID группы | токен. Нажмите «➕ Группа» и повторите.")
                    return
                project, group_id, token = parts
                with connect(self.db_path) as db:
                    if not db.execute("SELECT 1 FROM projects WHERE name=?", (project,)).fetchone():
                        self.api.send(chat_id, "Сначала добавьте проект «{}» кнопкой «➕ Проект».".format(project))
                        return
                    db.execute("INSERT INTO groups(group_id, project, token) VALUES (?, ?, ?) "
                               "ON CONFLICT(group_id) DO UPDATE SET project=excluded.project, token=excluded.token, enabled=1",
                               (int(group_id), project, token))
                    db.commit()
                self.api.send(chat_id, "Группа {} подключена к проекту «{}». Запустите «🔄 Обновить».".format(group_id, project), keyboard=True)
                return
        if not text.startswith("/"):
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in ("/start", "/help"):
            if command == "/start":
                with connect(self.db_path) as db:
                    db.execute("INSERT OR IGNORE INTO subscribers(chat_id, subscribed_at) VALUES (?, ?)", (chat_id, int(time.time())))
                    db.commit()
            self.api.send(chat_id, "Кнопки: отчёт, обновление, проекты, добавление проекта и группы, таблица.\nКоманды: /report, /today, /group ID, /groups, /sheet, /sync, /status.\nПри добавлении группы бот попросит название проекта, числовой ID сообщества и токен VK. Сообщение с токеном будет удалено.", keyboard=True)
        elif command == "/stop":
            with connect(self.db_path) as db:
                db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
                db.commit()
            self.api.send(chat_id, "Ежедневные сообщения отключены. Команды бота остаются доступны. Для подписки отправьте /start.")
        elif command == "/newproject":
            self.pending[chat_id] = "project"
            self.api.send(chat_id, "Напишите название нового проекта одним сообщением.")
        elif command == "/newgroup":
            self.pending[chat_id] = "group"
            self.api.send(chat_id, "Отправьте одним сообщением: название проекта | числовой ID группы VK | токен группы.\nПример: Магазин А | 123456789 | токен\nСообщение с токеном бот удалит после получения.")
        elif command == "/report":
            with connect(self.db_path) as db:
                self.api.send(chat_id, summary(db, 30))
                detail, _ = report_data(db, 30)
                if detail:
                    self.api.send_document(chat_id, "vk-quality-30-days.csv", csv_text(detail),
                                           "Полная статистика по каждому проекту и ID рекламы")
        elif command == "/today":
            with connect(self.db_path) as db:
                self.api.send(chat_id, summary(db, 1))
                detail, _ = report_data(db, 1)
                if detail:
                    self.api.send_document(chat_id, "vk-quality-today.csv", csv_text(detail),
                                           "Полная статистика за сегодня")
        elif command == "/group":
            try:
                group_id = int(argument.strip())
            except ValueError:
                self.api.send(chat_id, "Укажите числовой ID: /group 123456")
                return
            with connect(self.db_path) as db:
                self._send_long(chat_id, group_summary(db, group_id))
        elif command == "/groups":
            with connect(self.db_path) as db:
                projects = [row["name"] for row in db.execute("SELECT name FROM projects ORDER BY name")]
                groups = list(db.execute("SELECT project, group_id, enabled FROM groups ORDER BY project, group_id"))
            lines = ["{}: {}".format(name, ", ".join(str(g["group_id"]) + (" (отключено)" if not g["enabled"] else "") for g in groups if g["project"] == name) or "группы пока нет") for name in projects]
            self.api.send(chat_id, "\n".join(lines) if lines else "Проектов пока нет. Нажмите «➕ Проект».")
        elif command == "/sheet":
            spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
            self.api.send(chat_id, "https://docs.google.com/spreadsheets/d/" + spreadsheet_id if spreadsheet_id else "Google-таблица ещё не подключена.")
        elif command == "/status":
            self.api.send(chat_id, self.status)
        elif command == "/sync":
            self.api.send(chat_id, "Запускаю сбор." if self.start_sync(chat_id) else "Сбор уже выполняется.")

    def _schedule(self) -> None:
        with connect(self.db_path) as db:
            config = get_settings(db)
            now = datetime.now(ZoneInfo(config["timezone"]))
            day = now.strftime("%Y-%m-%d")
            if now.hour < 3 or _get_setting(db, "last_daily_run") == day:
                return
            if not db.execute("SELECT 1 FROM groups WHERE enabled=1 LIMIT 1").fetchone():
                return
            if not self.sync_lock.acquire(blocking=False):
                return
            _set_setting(db, "last_daily_run", day)
            self.status = "Ежедневный сбор выполняется."
            threading.Thread(target=self._run_sync, args=(None,), daemon=True).start()

    def run(self) -> None:
        with connect(self.db_path) as db:
            offset = int(_get_setting(db, "telegram_offset", 0))
        while True:
            self._schedule()
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
                for update in updates:
                    try:
                        self.handle(update)
                    except Exception as exc:
                        self.status = "Ошибка обработки команды: " + str(exc)
                    offset = max(offset, int(update["update_id"]) + 1)
                    with connect(self.db_path) as db:
                        _set_setting(db, "telegram_offset", offset)
            except TelegramError:
                time.sleep(5)
