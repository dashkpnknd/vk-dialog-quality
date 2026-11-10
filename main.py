import argparse
import getpass
import json
import os
import time

from quality.db import connect
from quality.report import csv_text, publish_google, report_data
from quality.sync import sync_all
from quality.telegram_bot import ReportBot
from quality.vk import VKClient


def main() -> None:
    parser = argparse.ArgumentParser(description="VK dialog quality Telegram bot")
    parser.add_argument("--db", default=os.environ.get("DATABASE_PATH", "data/quality.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("bot")
    add = commands.add_parser("add-group")
    add.add_argument("project", help="Название магазина")
    add.add_argument("group_id", type=int, help="Числовой ID сообщества")
    probe = commands.add_parser("probe")
    probe.add_argument("group_id", type=int)
    probe.add_argument("--limit", type=int, default=10)
    sync = commands.add_parser("sync")
    sync.add_argument("--days", type=int, default=30)
    report = commands.add_parser("report")
    report.add_argument("--days", type=int, default=30)
    report.add_argument("--daily", action="store_true")
    publish = commands.add_parser("publish")
    publish.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if args.command == "bot":
        ReportBot(args.db).run()
        return
    with connect(args.db) as db:
        if args.command == "add-group":
            token = getpass.getpass("Токен сообщества VK: ").strip()
            if not token or args.group_id <= 0 or not args.project.strip():
                parser.error("Project, group ID and token must be set")
            db.execute("INSERT INTO groups(group_id, project, token) VALUES (?, ?, ?) "
                       "ON CONFLICT(group_id) DO UPDATE SET project=excluded.project, token=excluded.token, enabled=1",
                       (args.group_id, args.project.strip(), token))
            db.commit()
            db.execute("INSERT OR IGNORE INTO projects(name) VALUES (?)", (args.project.strip(),))
            db.commit()
            print("Сообщество добавлено.")
        elif args.command == "probe":
            group = db.execute("SELECT * FROM groups WHERE group_id=?", (args.group_id,)).fetchone()
            if group is None:
                parser.error("Group not found")
            client = VKClient(group["token"], group["group_id"])
            checked = 0
            for item in client.conversations_since(int(time.time()) - 7 * 86400):
                peer_id = item["conversation"]["peer"]["id"]
                history, has_older, truncated = client.history_since(peer_id, int(time.time()) - 30 * 86400, 200)
                print(json.dumps({"history_truncated": truncated,
                                  "message_count_sample": len(history),
                                  "refs": [{"ref": m.get("ref"), "ref_source": m.get("ref_source"),
                                            "has_payload": bool(m.get("payload")),
                                            "has_keyboard": bool(m.get("keyboard"))}
                                           for m in history if m.get("ref") or m.get("ref_source") or
                                           m.get("payload") or m.get("keyboard")][:20]}, ensure_ascii=False))
                checked += 1
                if checked >= args.limit:
                    break
        elif args.command == "sync":
            print(json.dumps(sync_all(db, args.days), ensure_ascii=False, indent=2))
        elif args.command == "report":
            summary, daily = report_data(db, args.days)
            print(csv_text(daily if args.daily else summary, args.daily), end="")
        elif args.command == "publish":
            print(publish_google(db, args.days))


if __name__ == "__main__":
    main()
