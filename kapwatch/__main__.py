import argparse
import json
import os
import sys

from .app import App, configure_logging, process_lock
from .config import Config, load_env
from .notify import DeliveryError, Telegram, email_sender, senders
from .store import Store
from .smartmail import RuleEngine
from .backtest import replay


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="KAP bildirim pilotu")
    parser.add_argument("--env-file", help="Yerel KEY=value ayar dosyası")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "collect-once", "status", "health", "preview", "telegram-info", "send-test", "send-test-email"):
        commands.add_parser(name)
    backup = commands.add_parser("backup")
    backup.add_argument("destination")
    backtest = commands.add_parser("smartmail-backtest")
    backtest.add_argument("dataset")
    backtest.add_argument("--rules", default="config/smartmail_rules.json")
    backtest.add_argument("--financial-context")
    backtest.add_argument("--title-summary-only", action="store_true")
    backtest.add_argument("--output")
    smartmail_report = commands.add_parser("smartmail-report")
    smartmail_report.add_argument("--limit", type=int, default=20)
    detail_seed = commands.add_parser("smartmail-seed-details")
    detail_seed.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        if args.command == "smartmail-backtest":
            result = replay(args.dataset, RuleEngine(args.rules, args.financial_context), args.title_summary_only)
            report = json.dumps(result, ensure_ascii=False, indent=2)
            if args.output:
                from pathlib import Path
                Path(args.output).write_text(report + "\n", encoding="utf-8")
                print(json.dumps({k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False, indent=2))
            else:
                print(report)
            return 0
        load_env(args.env_file)
        config = Config.from_env()
        if args.command == "smartmail-report":
            if not 1 <= args.limit <= 200:
                raise ValueError("Rapor sınırı 1–200 olmalı.")
            print(json.dumps(Store(config.data_dir).smartmail_report(args.limit), ensure_ascii=False, indent=2))
            return 0
        if args.command == "smartmail-seed-details":
            if not config.smartmail_enabled or not 1 <= args.limit <= 100:
                raise ValueError("SmartMail etkin ve tarihsel örnek sınırı 1–100 olmalı.")
            with process_lock(config.data_dir):
                print(json.dumps({"historical_detail_jobs_added": Store(config.data_dir).seed_details(args.limit, config.smartmail_detail_streams), "historical_email": False}))
            return 0
        if args.command in {"run", "collect-once"}:
            config.validate_delivery()
            configure_logging(config.data_dir)
            with process_lock(config.data_dir):
                app = App(config)
                if args.command == "run":
                    app.run()
                else:
                    return 0 if app.collect_once() else 1
        elif args.command in {"status", "health"}:
            status = Store(config.data_dir).status(config)
            print(json.dumps(status, indent=2, ensure_ascii=False))
            if args.command == "health":
                return 0 if status["healthy"] else 1
        elif args.command == "preview":
            print(json.dumps(Store(config.data_dir).previews(), indent=2, ensure_ascii=False))
        elif args.command == "backup":
            Store(config.data_dir).backup(args.destination)
            print("Tutarlı SQLite yedeği oluşturuldu.")
        elif args.command == "telegram-info":
            if not config.telegram_token:
                raise ValueError("Önce bot tokenını ayar dosyasına girin.")
            telegram = Telegram(config)
            me = telegram.call("getMe", {})
            updates = telegram.call("getUpdates", {"timeout": 0, "limit": 100,
                                   "allowed_updates": ["channel_post", "my_chat_member"]})
            chats = {}
            for update in updates or []:
                event = update.get("channel_post") or update.get("my_chat_member") or {}
                chat = event.get("chat", {})
                if chat.get("type") == "channel":
                    chats[chat["id"]] = {"id": chat["id"], "title": chat.get("title")}
            print(json.dumps({"bot_username": me.get("username"), "channels": list(chats.values())}, ensure_ascii=False, indent=2))
        elif args.command == "send-test":
            config.validate_delivery()
            if not config.destinations:
                raise ValueError("En az bir gönderim kanalı etkin olmalı.")
            for channel, client in senders(config).items():
                client.send(config.destinations[channel], "KAP bildirim pilotu: bağlantı testi başarılı. Yeni bildirimler bu kanala gönderilecek.")
                print(channel + ": deneme mesajı gönderildi.")
        elif args.command == "send-test-email":
            config.validate_delivery()
            if not config.email_enabled:
                raise ValueError("EMAIL_ENABLED ve SMTP ayarları gerekli.")
            email_sender(config).send(config.email_to, "KAP SmartMail: e-posta bağlantı testi\n\nBu bir bağlantı testidir; gerçek bir KAP alarmı değildir.")
            print("email: deneme mesajı sağlayıcı tarafından kabul edildi.")
        return 0
    except (ValueError, DeliveryError) as error:
        print("Hata: " + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        # No raw exceptions or traceback from credential-bearing transports.
        print("Beklenmeyen hata sınıfı: " + type(error).__name__, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
