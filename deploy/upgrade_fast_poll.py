"""Deploy the fast-poll pilot while preserving credentials and active P0 mail."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


APP = Path('/opt/kap-bildirim')
STAGE = Path('/opt/kap-bildirim-staging-fast-20261012')
ENV = Path('/etc/kap-bildirim.env')
DB = Path('/var/lib/kap-bildirim/kap.sqlite3')
DIRECTORIES = ('kapwatch', 'config', 'tests', 'tools', 'deploy', 'reports')
DOCUMENTS = ('README.md', 'PLAN.md', 'SMARTMAIL.md', 'VERIFICATION.md', 'RESEND_SETUP.md', '.env.example')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def settings(text, updates):
    remaining = dict(updates)
    result = []
    for line in text.splitlines():
        key = line.partition('=')[0].strip()
        if key in updates:
            line = key+'='+updates[key]
            remaining.pop(key, None)
        result.append(line)
    result.extend(key+'='+value for key,value in remaining.items())
    return '\n'.join(result)+'\n'


def main():
    if os.geteuid() != 0 or not (STAGE/'tests/test_fast_poll.py').is_file():
        raise ValueError('Root ve hazırlanmış hızlı pilot staging dizini gerekli.')
    os.umask(0o077)
    run(sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q', cwd=STAGE)
    backup = Path('/opt/kap-bildirim-backups')/('fast-20261012-'+str(int(time.time())))
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    shutil.copytree(APP, backup/'app', ignore=shutil.ignore_patterns('__pycache__', 'data', 'artifacts'))
    shutil.copy2(ENV, backup/'kap-bildirim.env')
    (backup/'kap-bildirim.env').chmod(0o600)
    updates = {'POLL_SECONDS':'15', 'POLL_REQUEST_TIMEOUT_SECONDS':'8',
               'POLL_RECONCILE_SECONDS':'180', 'SMARTMAIL_DETAIL_INTERVAL_SECONDS':'2'}
    updated = settings(ENV.read_text(), updates)
    run('systemctl', 'stop', 'kap-bildirim.service')
    try:
        source, target = sqlite3.connect(DB), sqlite3.connect(backup/'kap.sqlite3')
        try:
            source.backup(target)
            # Shorten only ordinary scheduled polling; never shorten an error cooldown.
            source.execute('UPDATE sources SET next_poll_at=MIN(next_poll_at,?) WHERE last_error IS NULL', (time.time(),))
            source.commit()
        finally:
            source.close()
            target.close()
        (backup/'kap.sqlite3').chmod(0o600)
        for name in DIRECTORIES:
            shutil.copytree(STAGE/name, APP/name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
            (APP/name).chmod(0o755)
            for path in (APP/name).rglob('*'):
                path.chmod(0o755 if path.is_dir() else 0o644)
        for name in DOCUMENTS:
            shutil.copy2(STAGE/name, APP/name)
            (APP/name).chmod(0o644)
        temporary = ENV.with_name(ENV.name+'.fast-new')
        temporary.write_text(updated)
        temporary.chmod(0o600)
        temporary.replace(ENV)
        run('systemctl', 'start', 'kap-bildirim.service')
        time.sleep(2)
        run('systemctl', 'is-active', '--quiet', 'kap-bildirim.service')
        print(json.dumps({'service':'active', 'poll_seconds':15, 'poll_timeout_seconds':8,
                          'detail_spacing_seconds':2, 'delivery_settings_preserved':True,
                          'historical_alerts_added':False, 'backup':str(backup)}))
    except BaseException:
        run('systemctl', 'stop', 'kap-bildirim.service')
        shutil.copytree(backup/'app', APP, dirs_exist_ok=True)
        shutil.copy2(backup/'kap-bildirim.env', ENV)
        run('systemctl', 'start', 'kap-bildirim.service')
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Dağıtım hatası sınıfı: '+type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
