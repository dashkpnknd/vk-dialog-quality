"""Telegram interface for VK quality reports and city forum topics."""

from datetime import datetime
import hmac
from html import escape
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from .db import connect, settings as get_settings
from .report import campaign_rows, csv_text, report_data, publish_google
from .jobs import enqueue_groups
from .topics import format_project_report, publish_project_reports, report_date_range
from .vk import VKClient, VKError, group_from_token


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str):
        if not token:
            raise ValueError("Set TELEGRAM_BOT_TOKEN")
        self.base = "https://api.telegram.org/bot" + token + "/"

    def call(self, method: str, **params: Any) -> Any:
        for attempt in range(5):
            try:
                response = requests.post(self.base + method, json=params, timeout=45)
                payload = response.json()
            except (requests.RequestException, ValueError):
                # The request URL contains the bot token and must never appear in an error.
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise TelegramError("Telegram request failed") from None
            if payload.get("ok"):
                return payload.get("result")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
            if retry_after and attempt < 4:
                time.sleep(min(max(int(retry_after), 1), 120))
                continue
            raise TelegramError("Telegram API error: " + str(payload.get("description", "unknown")))
        raise TelegramError("Telegram request retries exhausted")

    def send(self, chat_id: int, text: str, keyboard: bool = False,
             thread_id: Optional[int] = None, parse_mode: Optional[str] = None,
             reply_markup: Optional[Dict[str, Any]] = None) -> Any:
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        if parse_mode:
            params["parse_mode"] = parse_mode
        if keyboard:
            params["reply_markup"] = {"keyboard": [
                [{"text": "📊 Отчёт"}, {"text": "🎯 Кампании"}],
                [{"text": "🏪 Проекты"}, {"text": "➕ Подключить VK"}],
                [{"text": "📄 Таблица"}]], "resize_keyboard": True}
        if reply_markup:
            params["reply_markup"] = reply_markup
        return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str,
             reply_markup: Dict[str, Any]) -> None:
        self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                  text=text, parse_mode="HTML", reply_markup=reply_markup)

    def edit_markup(self, chat_id: int, message_id: int,
                    reply_markup: Dict[str, Any]) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                  reply_markup=reply_markup)

    def answer_callback(self, callback_id: str, text: Optional[str] = None) -> None:
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
            params["show_alert"] = True
        self.call("answerCallbackQuery", **params)

    def pin(self, chat_id: int, message_id: int) -> None:
        self.call("pinChatMessage", chat_id=chat_id, message_id=message_id,
                  disable_notification=True)

    def create_topic(self, chat_id: int, name: str) -> int:
        topic = self.call("createForumTopic", chat_id=chat_id, name=name[:128])
        return int(topic["message_thread_id"])

    def edit_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        self.call("editForumTopic", chat_id=chat_id, message_thread_id=thread_id, name=name[:128])

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
    rows, _ = report_data(db, days, complete_only=True, include_daily=False)
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
    spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
    if spreadsheet_id:
        lines.append("\nТаблица: https://docs.google.com/spreadsheets/d/" + spreadsheet_id)
    return "\n".join(lines)


def group_summary(db: sqlite3.Connection, group_id: int) -> str:
    group = db.execute("SELECT project FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if group is None:
        return "Такое сообщество не добавлено. Команда /groups покажет список."
    rows, _ = report_data(db, 30, complete_only=True,
                          project_names={group["project"]}, include_daily=False)
    group_rows = [row for row in rows if row["group_id"] == group_id]
    if not group_rows:
        return "{}: за 30 дней данных пока нет.".format(group["project"])
    lines = ["{} · ID {} · 30 дней".format(group["project"], group_id)]
    for row in group_rows:
        lines.append("{}: {} из {} ({}%) · проверка {}".format(
            row["label_id"], row["qualified"], row["total"], row["percent"], row["review"]))
    return "\n".join(lines)


class ReportBot:
    def __init__(self, db_path: str, api: Optional[TelegramAPI] = None,
                 access_code: Optional[str] = None):
        self.db_path = db_path
        self.api = api or TelegramAPI(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
        self.access_code = access_code if access_code is not None else os.environ.get("BOT_ACCESS_CODE", "")
        if not self.access_code and api is None:
            raise ValueError("Set BOT_ACCESS_CODE")
        self.status = "Сбор ещё не запускался в этой сессии."
        self.pending: Dict[int, str] = {}

    @staticmethod
    def _authorized(db: sqlite3.Connection, user_id: Optional[int]) -> bool:
        return isinstance(user_id, int) and db.execute(
            "SELECT 1 FROM authorized_users WHERE user_id=?", (user_id,)).fetchone() is not None

    def _authorize(self, db: sqlite3.Connection, user_id: int, code: str) -> str:
        now = int(time.time())
        previous = db.execute("SELECT window_started_at,attempts FROM auth_attempts WHERE user_id=?",
                              (user_id,)).fetchone()
        recent = previous and now - previous["window_started_at"] < 3600
        if recent and previous["attempts"] >= 5:
            return "blocked"
        if self.access_code and hmac.compare_digest(code.encode("utf-8"),
                                                    self.access_code.encode("utf-8")):
            db.execute("INSERT OR REPLACE INTO authorized_users(user_id,authorized_at) VALUES (?,?)",
                       (user_id, now))
            db.execute("DELETE FROM auth_attempts WHERE user_id=?", (user_id,))
            db.commit()
            return "ok"
        started_at = previous["window_started_at"] if recent else now
        attempts = previous["attempts"] + 1 if recent else 1
        db.execute("INSERT INTO auth_attempts(user_id,window_started_at,attempts) VALUES (?,?,?) "
                   "ON CONFLICT(user_id) DO UPDATE SET window_started_at=excluded.window_started_at,"
                   "attempts=excluded.attempts", (user_id, started_at, attempts))
        db.commit()
        return "invalid"

    @staticmethod
    def _general_markup() -> Dict[str, Any]:
        return {"inline_keyboard": [
            [{"text": "📊 Отчёт", "callback_data": "g:report"},
             {"text": "🏪 Проекты", "callback_data": "g:projects"}],
            [{"text": "🎯 Кампании", "callback_data": "g:campaign"},
             {"text": "📄 Таблица", "callback_data": "g:sheet"}]]}

    def _campaign_report(self, db: sqlite3.Connection, label_id: str) -> str:
        if not label_id.isascii() or not label_id.isdecimal() or len(label_id) > 32:
            return "Отправьте числовой рекламный ID, например 248543102."
        matches = campaign_rows(db, label_id)
        if not matches:
            return ("По ID <code>{}</code> за последние 30 дней обращений пока нет. "
                    "Новый ID появится после первого собранного обращения с этой меткой.").format(label_id)
        lines = ["🎯 <b>Рекламный ID {}</b>".format(label_id),
                 "Последние 30 дней: {}".format(report_date_range(db)),
                 "━━━━━━━━━━━━━━━━━━━━"]
        for row in matches:
            percent = ("{:.1f}".format(100 * row["qualified"] / row["total"])).replace(".", ",")
            lines.extend(("", "🏪 <b>{}</b>".format(escape(row["project"])),
                          "Обращения: <b>{}</b>".format(row["total"]),
                          "Квалы: <b>{}</b> ({}%) · не квалы: <b>{}</b>".format(
                              row["qualified"], percent, row["not_qualified"])))
            if row["review"]:
                lines.append("На проверке: <b>{}</b>".format(row["review"]))
        return "\n".join(lines)

    def _send_campaign_report(self, chat_id: int, label_id: str) -> None:
        with connect(self.db_path) as db:
            report = self._campaign_report(db, label_id.strip())
        part = ""
        for line in report.splitlines():
            if len(part) + len(line) + 1 > 3500 and part:
                self.api.send(chat_id, part.rstrip(), parse_mode="HTML")
                part = ""
            part += line + "\n"
        if part:
            self.api.send(chat_id, part.rstrip(), parse_mode="HTML")

    def _ask_campaign_id(self, chat_id: int) -> None:
        self.api.send(chat_id, "Отправьте числовой рекламный ID. Покажу результаты по всем магазинам, где он встретился.",
                      reply_markup={"force_reply": True, "selective": True})

    def ensure_general_panel(self, db: sqlite3.Connection, chat_id: int) -> bool:
        """Create one persistent General-topic control message per connected chat."""
        panel = db.execute("SELECT message_id,pinned FROM general_panels WHERE chat_id=?",
                           (chat_id,)).fetchone()
        if panel is None:
            message = self.api.send(chat_id, "📊 <b>VK КВАЛЫ</b>\nОтчёты по магазинам",
                                    parse_mode="HTML", reply_markup=self._general_markup())
            if not isinstance(message, dict) or not isinstance(message.get("message_id"), int):
                raise TelegramError("Telegram не вернул ID панели General")
            message_id = message["message_id"]
            db.execute("INSERT INTO general_panels(chat_id,message_id,pinned) VALUES (?,?,0)",
                       (chat_id, message_id))
            db.commit()
        else:
            message_id = panel["message_id"]
            try:
                self.api.edit_markup(chat_id, message_id, self._general_markup())
            except TelegramError as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
            if panel["pinned"]:
                return True
        try:
            self.api.pin(chat_id, message_id)
        except TelegramError:
            return False
        db.execute("UPDATE general_panels SET pinned=1 WHERE chat_id=?", (chat_id,))
        db.commit()
        return True

    def _send_long(self, chat_id: int, text: str) -> None:
        part = ""
        for line in text.splitlines():
            if len(part) + len(line) + 1 > 3500 and part:
                self.api.send(chat_id, part)
                part = ""
            part += line + "\n"
        if part:
            self.api.send(chat_id, part)

    def start_sync(self, reply_to: Optional[int], group_ids=None, priority=None) -> bool:
        with connect(self.db_path) as db:
            queued = enqueue_groups(db, group_ids, priority=(priority if priority is not None else
                                                           (1 if reply_to is not None else 0)))
        self.status = "В очереди на обновление: {} сообществ.".format(queued)
        return queued > 0

    def _report_menu(self, db: sqlite3.Connection, page: int = 0) -> tuple:
        projects = list(db.execute(
            "SELECT project, MIN(group_id) AS group_id FROM groups GROUP BY project ORDER BY project"))
        page_count = max(1, (len(projects) + 7) // 8)
        page = min(max(page, 0), page_count - 1)
        period, _ = report_data(db, 30, complete_only=True, include_daily=False)
        completed = db.execute("SELECT COUNT(*) FROM groups g JOIN group_sync_state s "
                               "ON s.group_id=g.group_id WHERE g.enabled=1 AND s.completed_at IS NOT NULL "
                               ).fetchone()[0]
        enabled = db.execute("SELECT COUNT(*) FROM groups WHERE enabled=1").fetchone()[0]
        running = db.execute("SELECT COUNT(*) FROM group_sync_state WHERE running=1").fetchone()[0]
        scanned = db.execute("SELECT COALESCE(SUM(scanned),0) FROM group_sync_state WHERE running=1").fetchone()[0]
        failed = db.execute("SELECT COUNT(*) FROM group_sync_state WHERE last_error IS NOT NULL").fetchone()[0]
        total = sum(int(row["total"]) for row in period)
        qualified = sum(int(row["qualified"]) for row in period)
        not_qualified = sum(int(row["not_qualified"]) for row in period)
        review = sum(int(row["review"]) for row in period)
        percent = "{:.1f}".format(100 * qualified / total if total else 0).replace(".", ",")
        text = ("📊 <b>VK КВАЛЫ</b>\nПоследние 30 дней: {}\n━━━━━━━━━━━━━━━━━━━━\n"
                "🏪 Магазинов: <b>{}</b>\n👥 Обращения: <b>{}</b>\n"
                "✅ Квалы: <b>{}</b> (<b>{}%</b>)\n❌ Не квалы: <b>{}</b>").format(
                    report_date_range(db), len(projects), total, qualified, percent, not_qualified)
        if review:
            text += "\n⚠️ На проверке: <b>{}</b>".format(review)
        if completed < enabled:
            text += "\n⏳ Полный сбор завершён: <b>{} из {}</b> сообществ".format(completed, enabled)
        if running:
            text += "\n🔄 Обновляются: <b>{}</b> сообществ · проверено чатов: <b>{}</b>".format(running, scanned)
        if failed:
            text += "\n⚠️ Ошибки обновления: <b>{}</b> сообществ (повторятся автоматически)".format(failed)
        text += "\n\nВыберите магазин:"
        if not projects:
            text += "\nПока нет подключённых сообществ."
        buttons = [[{"text": row["project"][:48],
                     "callback_data": "r:g:{}:{}".format(row["group_id"], page)}]
                   for row in projects[page * 8:(page + 1) * 8]]
        if page_count > 1:
            navigation = []
            if page:
                navigation.append({"text": "◀️", "callback_data": "r:p:{}".format(page - 1)})
            navigation.append({"text": "{}/{}".format(page + 1, page_count),
                               "callback_data": "r:p:{}".format(page)})
            if page + 1 < page_count:
                navigation.append({"text": "▶️", "callback_data": "r:p:{}".format(page + 1)})
            buttons.append(navigation)
        return text, {"inline_keyboard": buttons}

    def _handle_callback(self, query: Dict[str, Any]) -> None:
        callback_id = query.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        actor_id = (query.get("from") or {}).get("id")
        if not isinstance(chat_id, int):
            return
        with connect(self.db_path) as db:
            allowed = self._authorized(db, actor_id)
            if chat.get("type") in ("group", "supergroup"):
                allowed = allowed and db.execute(
                    "SELECT 1 FROM report_chats WHERE chat_id=?", (chat_id,)).fetchone() is not None
        if not allowed:
            if callback_id:
                self.api.answer_callback(str(callback_id), "Откройте бота в личном чате и введите команду доступа.")
            return
        if callback_id:
            self.api.answer_callback(str(callback_id))
        data = str(query.get("data") or "")
        if chat.get("type") in ("group", "supergroup") and data.startswith("g:"):
            if data == "g:report":
                started = self.start_sync(chat_id)
                self.api.send(chat_id, "Обновление VK поставлено в очередь. Готовые отчёты появятся в темах."
                              if started else "Подключённых сообществ пока нет.")
                with connect(self.db_path) as db:
                    menu, markup = self._report_menu(db)
                self.api.send(chat_id, menu, parse_mode="HTML", reply_markup=markup)
            elif data == "g:projects":
                with connect(self.db_path) as db:
                    text, markup = self._report_menu(db)
                self.api.send(chat_id, text, parse_mode="HTML", reply_markup=markup)
            elif data == "g:campaign":
                self._ask_campaign_id(chat_id)
            elif data == "g:sheet":
                spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
                self.api.send(chat_id, "https://docs.google.com/spreadsheets/d/" + spreadsheet_id
                              if spreadsheet_id else "Google-таблица ещё не подключена.")
            return
        parts = str(query.get("data") or "").split(":")
        if len(parts) < 3 or parts[0] != "r":
            return
        try:
            value = int(parts[2])
        except ValueError:
            return
        with connect(self.db_path) as db:
            if parts[1] == "p":
                text, markup = self._report_menu(db, value)
                self.api.edit(chat_id, int(message["message_id"]), text, markup)
            elif parts[1] == "g":
                row = db.execute("SELECT project FROM groups WHERE group_id=?", (value,)).fetchone()
                if not row:
                    self.api.send(chat_id, "Магазин не найден. Откройте «📊 Отчёт» ещё раз.")
                    return
                page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
                period, _ = report_data(db, 30, complete_only=True,
                                        project_names={row["project"]}, include_daily=False)
                if not any(item["project"] == row["project"] for item in period):
                    self.api.send(chat_id, "Для этого магазина полный сбор ещё не завершён.")
                    return
                reports = format_project_report(
                    row["project"], [item for item in period if item["project"] == row["project"]],
                    report_date_range(db))
                back = {"inline_keyboard": [[{"text": "⬅️ К магазинам",
                                             "callback_data": "r:p:{}".format(page)}]]}
                for index, report in enumerate(reports):
                    self.api.send(chat_id, report, parse_mode="HTML",
                                  reply_markup=back if index == len(reports) - 1 else None)

    def handle(self, update: Dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            return
        text = str(message.get("text") or "").strip()
        if chat.get("type") in ("group", "supergroup"):
            command_token = text.partition(" ")[0]
            command, _, mentioned_bot = command_token.partition("@")
            if mentioned_bot and mentioned_bot.casefold() != "vk_dialog_quality_report_bot":
                return
            command = command.lower()
            if command.startswith("/start") and command != "/start":
                try:
                    self.api.delete(chat_id, int(message["message_id"]))
                except (KeyError, TelegramError):
                    pass
                self.api.send(chat_id, "Команду доступа отправьте боту в личном чате.")
                return
            campaign_reply = str((message.get("reply_to_message") or {}).get("text") or "").startswith(
                "Отправьте числовой рекламный ID.")
            if command not in ("/connect", "/report", "/campaign") and not campaign_reply:
                return
            user_id = (message.get("from") or {}).get("id")
            with connect(self.db_path) as db:
                if not self._authorized(db, user_id):
                    self.api.send(chat_id, "Сначала откройте бота в личном чате и введите команду доступа.")
                    return
                if (command == "/campaign" or campaign_reply) and not db.execute(
                        "SELECT 1 FROM report_chats WHERE chat_id=?", (chat_id,)).fetchone():
                    self.api.send(chat_id, "Сначала отправьте /connect в этом чате.")
                    return
            if command == "/connect":
                info = self.api.call("getChat", chat_id=chat_id)
                if not info.get("is_forum"):
                    self.api.send(chat_id, "Включите темы в настройках группы, затем повторите /connect.")
                    return
                me = self.api.call("getMe")
                member = self.api.call("getChatMember", chat_id=chat_id, user_id=me["id"])
                if member.get("status") not in ("administrator", "creator") or not member.get("can_manage_topics"):
                    self.api.send(chat_id, "Дайте боту права администратора «Управлять темами» и повторите /connect.")
                    return
                with connect(self.db_path) as db:
                    already_connected = bool(db.execute(
                        "SELECT 1 FROM report_chats WHERE chat_id=?", (chat_id,)).fetchone())
                    db.execute("INSERT INTO report_chats(chat_id,title,connected_at) VALUES (?,?,?) "
                               "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                               (chat_id, str(info.get("title") or chat.get("title") or "Отчёты"), int(time.time())))
                    db.commit()
                    posted = publish_project_reports(db, self.api, chat_id) if not already_connected else None
                    pinned = self.ensure_general_panel(db, chat_id)
                if posted:
                    self.api.send(chat_id, "Чат подключён. Создано тем: {}. Отправлено отчётов: {}. "
                                  "Новые отчёты будут приходить ежедневно после 10:00 МСК. Панель в General {}.".format(
                                      posted["topics"], posted["reports"],
                                      "закреплена" if pinned else "отправлена, но не закреплена"))
                else:
                    self.api.send(chat_id, "Чат уже подключён. Панель в General {}.".format(
                        "закреплена" if pinned else "отправлена, но не закреплена"))
            elif command == "/report":
                with connect(self.db_path) as db:
                    if not db.execute("SELECT 1 FROM report_chats WHERE chat_id=?", (chat_id,)).fetchone():
                        self.api.send(chat_id, "Сначала отправьте /connect в этом чате.")
                        return
                started = self.start_sync(chat_id)
                self.api.send(chat_id, "Обновление VK поставлено в очередь. Готовые отчёты появятся в темах."
                              if started else "Подключённых сообществ пока нет.")
            elif command == "/campaign" or campaign_reply:
                if command == "/campaign":
                    _, _, label_id = text.partition(" ")
                else:
                    label_id = text
                if label_id.strip():
                    self._send_campaign_report(chat_id, label_id)
                else:
                    self._ask_campaign_id(chat_id)
            return
        if chat.get("type") != "private":
            return
        user_id = (message.get("from") or {}).get("id") or chat_id
        command_token = text.partition(" ")[0].split("@", 1)[0].lower()
        if command_token.startswith("/start") and command_token != "/start":
            try:
                self.api.delete(chat_id, int(message["message_id"]))
            except (KeyError, TelegramError):
                pass
            with connect(self.db_path) as db:
                result = self._authorize(db, user_id, command_token[len("/start"):])
            if result == "ok":
                self.api.send(chat_id, "Доступ открыт. Используйте кнопки ниже.", keyboard=True)
            elif result == "blocked":
                self.api.send(chat_id, "Слишком много попыток. Повторите через час.")
            else:
                self.api.send(chat_id, "Неверная команда доступа.")
            return
        with connect(self.db_path) as db:
            if not self._authorized(db, user_id):
                self.api.send(chat_id, "Для доступа введите полученную от команды команду в личном чате бота.")
                return
        buttons = {"📊 Отчёт": "/report", "🎯 Кампании": "/campaign",
                   "🔄 Обновить": "/report",
                   "🔄 Собрать данные": "/report", "🏪 Проекты": "/groups",
                   "➕ Подключить VK": "/newgroup", "➕ Проект": "/newgroup",
                   "➕ Группа": "/newgroup", "📄 Таблица": "/sheet",
                   "ℹ️ Помощь": "/help"}
        text = buttons.get(text, text)
        if self.pending.get(chat_id) and not text.startswith("/"):
            action = self.pending.pop(chat_id)
            if action == "campaign":
                self._send_campaign_report(chat_id, text)
                return
            if action == "project":
                name, _, city = text.partition(";")
                name, city = name[:120].strip(), city[:120].strip()
                if not name:
                    self.api.send(chat_id, "Название проекта пустое. Нажмите «➕ Проект» и попробуйте ещё раз.")
                    return
                with connect(self.db_path) as db:
                    db.execute("INSERT INTO projects(name,city) VALUES (?,?) "
                               "ON CONFLICT(name) DO UPDATE SET city=COALESCE(excluded.city,projects.city)",
                               (name, city or None))
                    db.commit()
                self.api.send(chat_id, "Проект «{}» добавлен. Теперь подключите группу кнопкой «➕ Группа».".format(name), keyboard=True)
                return
            if action == "group":
                try:
                    self.api.delete(chat_id, int(message["message_id"]))
                except (KeyError, TelegramError):
                    self.api.send(chat_id, "Не удалось удалить сообщение с токеном. Удалите его вручную из чата.")
                token = text.strip()
                try:
                    group_id, name = group_from_token(token)
                    VKClient(token, group_id).call("messages.getConversations", count=1, offset=0)
                except VKError as exc:
                    self.api.send(chat_id, "Не удалось подключить сообщество: {}. Проверьте ключ с доступом к сообщениям и повторите «➕ Подключить VK».".format(exc))
                    return
                with connect(self.db_path) as db:
                    existing = db.execute("SELECT project FROM groups WHERE group_id=?", (group_id,)).fetchone()
                    project = existing["project"] if existing else name[:120]
                    if not existing and db.execute(
                            "SELECT 1 FROM groups WHERE project=? LIMIT 1", (project,)).fetchone():
                        suffix = " · VK {}".format(group_id)
                        project = project[:120 - len(suffix)] + suffix
                    db.execute("INSERT OR IGNORE INTO projects(name) VALUES (?)", (project,))
                    db.execute("INSERT INTO groups(group_id, project, token) VALUES (?, ?, ?) "
                               "ON CONFLICT(group_id) DO UPDATE SET project=excluded.project, token=excluded.token, enabled=1",
                               (group_id, project, token))
                    db.commit()
                self.start_sync(None, group_ids={group_id}, priority=2)
                self.api.send(chat_id, "Подключено: «{}» (VK ID {}). Сбор за 30 дней поставлен в очередь автоматически.".format(
                    project, group_id), keyboard=True)
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
            self.api.send(chat_id, "Нажмите «📊 Отчёт»: бот соберёт свежие данные, покажет их здесь и отправит отчёты в темы магазинов. Ежедневные отчёты в темах приходят после 10:00 МСК. Для подключения сообщества нажмите «➕ Подключить VK» и отправьте его ключ доступа; сообщение с ключом бот удалит.", keyboard=True)
        elif command == "/stop":
            with connect(self.db_path) as db:
                db.execute("DELETE FROM subscribers WHERE chat_id=?", (chat_id,))
                db.commit()
            self.api.send(chat_id, "Уведомления об ошибках отключены. Отчёт по кнопке остаётся доступен; /start снова включит уведомления.")
        elif command == "/newproject":
            self.pending[chat_id] = "project"
            self.api.send(chat_id, "Напишите: название проекта ; город. Пример: Магазин А ; Калуга")
        elif command == "/city":
            name, separator, city = argument.rpartition(";")
            if not separator or not name.strip() or not city.strip():
                self.api.send(chat_id, "Формат: /city Название проекта ; Город")
                return
            with connect(self.db_path) as db:
                changed = db.execute("UPDATE projects SET city=? WHERE name=?",
                                     (city.strip()[:120], name.strip())).rowcount
                db.commit()
            self.api.send(chat_id, "Город проекта обновлён." if changed else "Проект не найден. Проверьте название через /groups.")
        elif command == "/newgroup":
            self.pending[chat_id] = "group"
            self.api.send(chat_id, "Отправьте одним сообщением ключ доступа сообщества VK. Название и ID бот определит сам. Сообщение с ключом будет удалено после получения.")
        elif command == "/campaign":
            if argument.strip():
                self._send_campaign_report(chat_id, argument)
            else:
                self.pending[chat_id] = "campaign"
                self._ask_campaign_id(chat_id)
        elif command in ("/report", "/sync"):
            self.api.send(chat_id, "Обновление VK поставлено в очередь. Ниже последняя доступная сводка; новые данные появятся в темах."
                          if self.start_sync(chat_id) else "Подключённых сообществ пока нет.")
            with connect(self.db_path) as db:
                menu, markup = self._report_menu(db)
            self.api.send(chat_id, menu, parse_mode="HTML", reply_markup=markup)
        elif command == "/today":
            with connect(self.db_path) as db:
                self.api.send(chat_id, summary(db, 1))
                detail, _ = report_data(db, 1, complete_only=True)
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
            self._send_long(chat_id, "\n".join(lines) if lines else "Проектов пока нет. Нажмите «➕ Подключить VK».")
        elif command == "/sheet":
            spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID")
            self.api.send(chat_id, "https://docs.google.com/spreadsheets/d/" + spreadsheet_id if spreadsheet_id else "Google-таблица ещё не подключена.")
        elif command == "/status":
            with connect(self.db_path) as db:
                total = db.execute("SELECT COUNT(*) FROM groups WHERE enabled=1").fetchone()[0]
                ready = db.execute("SELECT COUNT(*) FROM groups g JOIN group_sync_state s ON s.group_id=g.group_id "
                                   "WHERE g.enabled=1 AND s.completed_at IS NOT NULL").fetchone()[0]
                queued = db.execute("SELECT COUNT(*) FROM sync_jobs").fetchone()[0]
                running = db.execute("SELECT COUNT(*) FROM group_sync_state WHERE running=1").fetchone()[0]
                failed = db.execute("SELECT COUNT(*) FROM group_sync_state WHERE last_error IS NOT NULL").fetchone()[0]
            self.api.send(chat_id, "Сообществ: {} · готово: {} · в очереди: {} · обрабатывается: {} · ошибок: {}.\n{}".format(
                total, ready, queued, running, failed, self.status))

    def _schedule(self) -> None:
        with connect(self.db_path) as db:
            config = get_settings(db)
            now = datetime.now(ZoneInfo(config["timezone"]))
            day = now.strftime("%Y-%m-%d")
            if not db.execute("SELECT 1 FROM groups WHERE enabled=1 LIMIT 1").fetchone():
                return
            if now.hour >= 3 and _get_setting(db, "last_daily_run") != day:
                queued = enqueue_groups(db)
                _set_setting(db, "last_daily_run", day)
                self.status = "Ежедневное обновление поставлено в очередь: {} сообществ.".format(queued)

    def _publish_pending_once(self) -> bool:
        with connect(self.db_path) as db:
            now = datetime.now(ZoneInfo(get_settings(db)["timezone"]))
            day = now.strftime("%Y-%m-%d")
            row = db.execute(
                "SELECT g.group_id,g.project,s.completed_at FROM groups g "
                "JOIN group_sync_state s ON s.group_id=g.group_id "
                "LEFT JOIN report_delivery_state d ON d.group_id=g.group_id "
                "LEFT JOIN sync_jobs j ON j.group_id=g.group_id "
                "WHERE g.enabled=1 AND s.completed_at IS NOT NULL AND j.group_id IS NULL "
                "AND (s.completed_at>COALESCE(d.published_at,0) "
                "OR (? AND COALESCE(d.report_day,'')!=?)) "
                "ORDER BY s.completed_at,g.group_id LIMIT 1",
                (int(now.hour >= 10), day)).fetchone()
            if row:
                publish_project_reports(db, self.api, project_names={row["project"]})
                db.execute("INSERT INTO report_delivery_state(group_id,published_at,report_day) "
                           "VALUES (?,?,?) ON CONFLICT(group_id) DO UPDATE SET "
                           "published_at=excluded.published_at,report_day=excluded.report_day",
                           (row["group_id"], row["completed_at"], day if now.hour >= 10 else None))
                db.commit()
                return True
            last_sheet = int(_get_setting(db, "last_sheet_published", 0))
            last_sheet_marker = int(_get_setting(db, "last_sheet_completed_at", 0))
            latest = db.execute("SELECT COALESCE(MAX(completed_at),0) FROM group_sync_state").fetchone()[0]
            if (latest > last_sheet_marker and int(time.time()) - last_sheet >= 900 and
                    os.environ.get("GOOGLE_SPREADSHEET_ID") and
                    os.path.isfile(os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""))):
                publish_google(db)
                _set_setting(db, "last_sheet_completed_at", latest)
                _set_setting(db, "last_sheet_published", int(time.time()))
        return False

    def _publisher_loop(self) -> None:
        while True:
            try:
                published = self._publish_pending_once()
                time.sleep(1 if published else 5)
            except Exception as exc:
                self.status = "Ошибка публикации отчётов: " + str(exc)
                time.sleep(10)

    def run(self) -> None:
        with connect(self.db_path) as db:
            offset = int(_get_setting(db, "telegram_offset", 0))
            missing = [row["group_id"] for row in db.execute(
                "SELECT g.group_id FROM groups g "
                "LEFT JOIN group_sync_state s ON s.group_id=g.group_id "
                "LEFT JOIN sync_jobs j ON j.group_id=g.group_id "
                "WHERE g.enabled=1 AND j.group_id IS NULL "
                "AND (s.completed_at IS NULL OR s.last_error IS NOT NULL)")]
            now = datetime.now(ZoneInfo(get_settings(db)["timezone"]))
            day = now.strftime("%Y-%m-%d")
            daily_due = now.hour >= 3 and _get_setting(db, "last_daily_run") != day
            if daily_due:
                enqueue_groups(db)
                _set_setting(db, "last_daily_run", day)
        if missing and not daily_due:
            self.start_sync(None, group_ids=missing)
        threading.Thread(target=self._publisher_loop, daemon=True).start()
        while True:
            self._schedule()
            try:
                updates = self.api.call("getUpdates", offset=offset, timeout=30,
                                        allowed_updates=["message", "callback_query"])
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
