#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf 'Kurulum root yetkisi gerektirir.\n' >&2
    exit 1
fi

app_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$app_dir" != /opt/kap-bildirim ]; then
    printf 'Önce projeyi /opt/kap-bildirim dizinine kopyalayın.\n' >&2
    exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ gerekli"'
if ! id kap-bildirim >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/kap-bildirim --shell /usr/sbin/nologin kap-bildirim
fi
install -d -m 0700 -o kap-bildirim -g kap-bildirim /var/lib/kap-bildirim

if [ ! -f /etc/kap-bildirim.env ]; then
    install -m 0600 "$app_dir/.env.example" /etc/kap-bildirim.env
    sed -i 's|^DATA_DIR=.*|DATA_DIR=/var/lib/kap-bildirim|' /etc/kap-bildirim.env
fi
chmod 0600 /etc/kap-bildirim.env
for unit in kap-bildirim.service kap-bildirim-health.service kap-bildirim-health.timer kap-bildirim-backup.service kap-bildirim-backup.timer; do
    install -m 0644 "$app_dir/deploy/$unit" "/etc/systemd/system/$unit"
done
systemd-analyze verify "$app_dir/deploy/kap-bildirim.service" "$app_dir/deploy/kap-bildirim-health.service" "$app_dir/deploy/kap-bildirim-health.timer" "$app_dir/deploy/kap-bildirim-backup.service" "$app_dir/deploy/kap-bildirim-backup.timer"
systemctl daemon-reload
systemctl enable kap-bildirim.service kap-bildirim-health.timer kap-bildirim-backup.timer
systemctl restart kap-bildirim.service
systemctl start kap-bildirim-health.timer kap-bildirim-backup.timer
printf 'Kuruldu. Durum: systemctl status kap-bildirim --no-pager\n'
