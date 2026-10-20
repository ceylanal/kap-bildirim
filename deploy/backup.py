"""Consistent, compressed local SQLite backup; keep the last three copies."""
from datetime import datetime, timezone
import gzip
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from kapwatch.config import Config
from kapwatch.store import Store


def main():
    os.umask(0o077)
    config = Config.from_env()
    folder = config.data_dir / "backups"
    folder.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("kap-%Y%m%dT%H%M%SZ.sqlite3")
    temporary = folder / name
    compressed = folder / (name + ".gz")
    free = shutil.disk_usage(folder).free
    database_bytes = (config.data_dir / "kap.sqlite3").stat().st_size
    if free < database_bytes * 2 + 128 * 1024 * 1024:
        raise RuntimeError("Yedek için yeterli boş disk alanı yok.")
    Store(config.data_dir).backup(temporary)
    try:
        with temporary.open("rb") as source, gzip.open(str(compressed) + ".tmp", "wb") as target:
            shutil.copyfileobj(source, target)
        Path(str(compressed) + ".tmp").replace(compressed)
        for old in sorted(folder.glob("kap-*.sqlite3.gz"))[:-3]:
            old.unlink()
    finally:
        temporary.unlink(missing_ok=True)
        Path(str(compressed) + ".tmp").unlink(missing_ok=True)
    print("SQLite yedeği oluşturuldu; son üç sıkıştırılmış kopya saklanıyor.")


if __name__ == "__main__":
    main()
