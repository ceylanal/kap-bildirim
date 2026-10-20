"""Upgrade the existing KAP service to shadow mode with a recoverable snapshot."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


APP = Path("/opt/kap-bildirim")
STAGE = Path("/opt/kap-bildirim-staging-smartmail-20261012")
ENV = Path("/etc/kap-bildirim.env")
DB = Path("/var/lib/kap-bildirim/kap.sqlite3")


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    if os.geteuid() != 0 or not (STAGE / "kapwatch/detail.py").exists():
        raise SystemExit("Root ve hazırlanmış staging dizini gerekli.")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q", cwd=STAGE)
    backup = Path("/opt/kap-bildirim-backups") / ("smartmail-20261012-" + str(int(time.time())))
    backup.mkdir(parents=True, mode=0o700)
    os.chmod(backup.parent, 0o700)
    shutil.copytree(APP, backup / "app", ignore=shutil.ignore_patterns("__pycache__", "data", "artifacts"))
    shutil.copy2(ENV, backup / "kap-bildirim.env")
    os.chmod(backup / "kap-bildirim.env", 0o600)
    original = ENV.read_text()
    updates = {"SMARTMAIL_ENABLED": "true", "SMARTMAIL_MODE": "shadow",
               "SMARTMAIL_RULES_FILE": "/opt/kap-bildirim/config/smartmail_rules.json",
               "SMARTMAIL_DETAILS_ENABLED": "true", "SMARTMAIL_DETAIL_STREAMS": "members",
               "SMARTMAIL_DETAIL_INTERVAL_SECONDS": "5", "SMARTMAIL_DETAIL_DAILY_LIMIT": "2000"}
    pending = dict(updates)
    lines = []
    for line in original.splitlines():
        key = line.partition("=")[0].strip()
        if key in updates:
            line = key + "=" + updates[key]
            pending.pop(key, None)
        lines.append(line)
    lines.extend(key + "=" + value for key, value in pending.items())
    run("systemctl", "stop", "kap-bildirim.service")
    try:
        source = sqlite3.connect(DB)
        target = sqlite3.connect(backup / "kap.sqlite3")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(backup / "kap.sqlite3", 0o600)
        for name in ("kapwatch", "config", "tests", "tools", "deploy", "reports"):
            shutil.copytree(STAGE / name, APP / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
            os.chmod(APP / name, 0o755)
            for path in (APP / name).rglob("*"):
                os.chmod(path, 0o755 if path.is_dir() else 0o644)
        for name in ("README.md", "PLAN.md", "SMARTMAIL.md", "VERIFICATION.md", "RESEND_SETUP.md", ".env.example"):
            shutil.copy2(STAGE / name, APP / name)
        temporary = ENV.with_suffix(".env.smartmail-new")
        temporary.write_text("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(ENV)
        seed = run("runuser", "-u", "kap-bildirim", "--", sys.executable, "-c",
                   "from kapwatch.store import Store; print(Store('/var/lib/kap-bildirim').seed_details(10))",
                   cwd=APP, capture_output=True, text=True)
        run("systemctl", "start", "kap-bildirim.service")
        time.sleep(2)
        active = run("systemctl", "is-active", "kap-bildirim.service", capture_output=True, text=True)
        print(json.dumps({"service": active.stdout.strip(), "smartmail_mode": "shadow",
                          "historical_detail_jobs_added": int(seed.stdout.strip()),
                          "historical_email": False, "backup": str(backup)}, ensure_ascii=False))
    except BaseException:
        # Additive DB schema is compatible with the previous app; preserve new records.
        shutil.copytree(backup / "app", APP, dirs_exist_ok=True)
        shutil.copy2(backup / "kap-bildirim.env", ENV)
        run("systemctl", "start", "kap-bildirim.service")
        raise


if __name__ == "__main__":
    main()
