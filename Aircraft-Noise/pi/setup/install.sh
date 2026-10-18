#!/usr/bin/env bash
# One-time setup on the Pi for Aircraft-Noise (safe to re-run, e.g. after a deploy).
# Run on the Pi as root, from the copy deploy.sh put there:
#
#   sudo bash ~/IoT/Aircraft-Noise/pi/setup/install.sh [<latitude> <longitude>]
#
# 1. Sets the readsb receiver location if you pass one (so every aircraft gets a distance).
# 2. Installs pyserial and the sqlite3 CLI, and lets your user open the ESP32's serial port.
# 3. Installs and (re)starts the adsb-logger and bridge-link systemd services.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi
user=${SUDO_USER:?run this with sudo from your normal account, not as root}
pi_dir=$(cd "$(dirname "$0")/.." && pwd)

if [[ $# -eq 2 ]]; then
  echo "==> Setting receiver location to $1, $2"
  readsb-set-location "$1" "$2"
elif [[ $# -ne 0 ]]; then
  echo "Usage: sudo bash $0 [<latitude> <longitude>]" >&2
  exit 1
fi

echo "==> Installing Python packages"
apt-get install -y python3-serial sqlite3

echo "==> Letting $user use serial ports"
usermod -aG dialout "$user"

echo "==> Installing systemd services"
for unit in adsb-logger bridge-link; do
  sed -e "s|@USER@|$user|g" -e "s|@PI_DIR@|$pi_dir|g" \
    "$pi_dir/setup/systemd/$unit.service" > "/etc/systemd/system/$unit.service"
done
systemctl daemon-reload
systemctl enable adsb-logger bridge-link
systemctl restart adsb-logger bridge-link

echo
echo "==> Status"
systemctl --no-pager --lines=3 status adsb-logger bridge-link || true
echo
echo "Logs:  journalctl -u adsb-logger -f   |   journalctl -u bridge-link -f"
