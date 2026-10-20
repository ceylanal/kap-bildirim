"""One-off commissioning message through the actual persistent delivery queue.

Run as the kap-bildirim service user with EnvironmentFile loaded by systemd.
Does not print credentials, replay disclosures or send email.
"""
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kapwatch.config import Config
from kapwatch.store import Store


def main():
    os.umask(0o077)
    config = Config.from_env()
    config.validate_delivery()
    if not config.telegram_enabled:
        raise ValueError("Önce Telegram ayarlarını etkinleştirin.")
    interval = str(config.poll_seconds // 60) + " dakika"
    body = (
        "Bağlantı testi başarılı. KAP bildirim takibi etkin.\n\n"
        "Şirket, kurum ve fon bildirimleri " + interval + " aralıkla kontrol ediliyor. "
        "Yeni bildirimler bu kanala iletilecek."
    )
    store = Store(config.data_dir)
    with store.connection() as db:
        store.enqueue(db, "setup:telegram:activation:v1", body,
                      {"telegram": config.telegram_chat_id}, time.time())
    print("Canlı gönderim kuyruğuna tekil bağlantı testi kaydedildi.")


if __name__ == "__main__":
    main()
