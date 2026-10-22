#!/usr/bin/env bash
# Set up the Pi for Aircraft-Noise (safe to re-run). Run on the Pi after deploy.sh:
#
#   sudo bash ~/aircraft-noise/setup/install.sh [<latitude> <longitude>]
#
# 1. Sets readsb's receiver location, if given (the ADS-B logger needs one).
# 2. Installs pyserial and lets your user open the bridge's serial port.
# 3. Keeps system logs across reboots, so problems can be traced afterwards.
# 4. Installs and (re)starts the adsb-logger and bridge-link services.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi
user=${SUDO_USER:?run this with sudo from your normal account, not as root}
pi_dir=$(cd "$(dirname "$0")/.." && pwd)

if [[ $# -eq 2 ]]; then
  readsb-set-location "$1" "$2"
elif [[ $# -ne 0 ]]; then
  echo "Usage: sudo bash $0 [<latitude> <longitude>]" >&2
  exit 1
fi

apt-get install -y python3-serial sqlite3
usermod -aG dialout "$user"

mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald

for unit in adsb-logger bridge-link; do
  sed -e "s|@USER@|$user|g" -e "s|@PI_DIR@|$pi_dir|g" \
    "$pi_dir/setup/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable adsb-logger bridge-link
systemctl restart adsb-logger bridge-link
systemctl --no-pager --lines=3 status adsb-logger bridge-link || true
echo
echo "Logs: journalctl -u adsb-logger -u bridge-link -f"
