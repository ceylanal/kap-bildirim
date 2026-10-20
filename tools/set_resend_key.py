"""Enter a Resend key privately on the VPS; optionally test and enable P0 mail."""
import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kapwatch.config import Config, load_env
from kapwatch.notify import DeliveryError, email_sender


def write_settings(path, updates):
    pending = dict(updates)
    lines = []
    for line in path.read_text().splitlines():
        key = line.partition('=')[0].strip()
        if key in updates:
            line = key + '=' + updates[key]
            pending.pop(key, None)
        lines.append(line)
    lines.extend(key + '=' + value for key, value in pending.items())
    temporary = path.with_name(path.name + '.resend-new')
    temporary.write_text('\n'.join(lines) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', default='/etc/kap-bildirim.env')
    parser.add_argument('--activate-p0', action='store_true')
    args = parser.parse_args()
    if not sys.stdin.isatty():
        raise ValueError('API anahtarı için etkileşimli SSH terminali gerekli (ssh -t).')
    os.umask(0o077)
    target = Path(args.env_file)
    load_env(target)
    if not os.environ.get('EMAIL_TO'):
        raise ValueError('Önce EMAIL_TO alıcısı tanımlanmalı.')
    secret = getpass.getpass('Resend API key (ekranda görünmez): ').strip()
    if not secret.startswith('re_') or any(c.isspace() or c in '\"\'\\#' for c in secret):
        raise ValueError('Geçerli bir Resend API anahtarı gerekli.')
    backup = target.with_name(target.name + '.before-resend')
    shutil.copy2(target, backup)
    backup.chmod(0o600)
    updates = {'RESEND_API_KEY': secret, 'EMAIL_PROVIDER': 'resend',
               'EMAIL_ENABLED': 'true', 'SMARTMAIL_MODE': 'shadow'}
    write_settings(target, updates)
    os.environ.update(updates)
    if args.activate_p0:
        config = Config.from_env()
        config.validate_delivery()
        email_sender(config).send(config.email_to,
            'KAP SmartMail: Resend bağlantı testi\n\nBu bir bağlantı testidir; gerçek bir KAP alarmı değildir.')
        write_settings(target, {'SMARTMAIL_MODE': 'p0'})
        subprocess.run(['systemctl', 'restart', 'kap-bildirim.service'], check=True)
        print('Test e-postası sağlayıcı tarafından kabul edildi; P0 e-posta modu etkin.')
    else:
        print('Anahtar gizli ayarlara kaydedildi; gölge mod korunuyor.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, DeliveryError) as error:
        print('Hata: ' + str(error), file=sys.stderr)
        raise SystemExit(2)
    except Exception as error:
        print('Beklenmeyen hata sınıfı: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(3)
