#!/usr/bin/env bash
# Set up the Pi for Aircraft-Noise (safe to re-run). Run on the Pi after deploy.sh:
#
#   sudo bash ~/aircraft-noise/setup/install.sh
#
# 1. Sets readsb's receiver location from location.json (the same file the logger reads).
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

loc=$(python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(d["lat"], d["lon"])' \
  "$pi_dir/location.json") || { echo "Need $pi_dir/location.json (see README)" >&2; exit 1; }
readsb-set-location $loc

apt-get install -y python3-serial sqlite3
usermod -aG dialout "$user"

# Raspberry Pi OS keeps logs in memory only (40-rpi-volatile-storage.conf); override that,
# capped at 100 MB so SD-card wear stays small.
mkdir -p /var/log/journal /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=100M\n' \
  > /etc/systemd/journald.conf.d/50-aircraft-noise.conf
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
