"""Queue explicitly selected historical examples in the normal message format.

Run as the service user with EnvironmentFile loaded by systemd. Each example
is keyed separately from live disclosures; rerunning does not send duplicates.
"""
import argparse
from datetime import date
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kapwatch.config import Config
from kapwatch.source import Disclosure, ISTANBUL
from kapwatch.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--ids", required=True, nargs="+", type=int)
    args = parser.parse_args()
    if not 1 <= len(args.ids) <= 5 or len(set(args.ids)) != len(args.ids):
        parser.error("Select between one and five distinct disclosure IDs.")
    os.umask(0o077)
    config = Config.from_env()
    config.validate_delivery()
    if not config.telegram_enabled:
        raise ValueError("Telegram delivery must be enabled.")
    store = Store(config.data_dir)
    with store.connection() as db:
        examples = []
        for index in args.ids:
            row = db.execute("SELECT * FROM disclosures WHERE disclosure_id=?", (index,)).fetchone()
            if row is None or row["blocked"]:
                raise ValueError("Selected disclosure is missing or blocked.")
            from datetime import datetime
            published = datetime.fromisoformat(row["published_at"]).astimezone(ISTANBUL)
            if published.date() != args.date:
                raise ValueError("Selected disclosure has a different publication date.")
            record = Disclosure(index=index, stream=row["stream"],
                                published_at=row["published_at"], company=row["company"],
                                code=row["code"], title=row["title"], summary=row["summary"],
                                blocked=False, raw_json=row["raw_json"])
            body = "ÖRNEK — GEÇMİŞ BİLDİRİM\n\n" + record.message()
            examples.append((index, body))
        for index, body in examples:
            store.enqueue(db, "sample:historical:v1:" + str(index), body,
                          {"telegram": config.telegram_chat_id}, time.time())
            print("EXAMPLE_QUEUED", index)


if __name__ == "__main__":
    main()
