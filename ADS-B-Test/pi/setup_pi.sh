#!/usr/bin/env bash
# One-time setup for the ADS-B receiver on the Raspberry Pi.
# Run on the Pi as root:  sudo bash setup_pi.sh
#
# 1. Blocks the DVB-T (TV tuner) kernel drivers so ADS-B software can use the RTL-SDR stick.
# 2. Installs rtl-sdr tools.
# 3. Installs readsb (the ADS-B decoder) and tar1090 (web map at http://<pi>/tar1090).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi

echo "==> Blocking DVB-T TV tuner drivers"
cat > /etc/modprobe.d/blacklist-rtl-sdr-dvb.conf <<'EOF'
# Stop the TV tuner drivers claiming the RTL-SDR stick, so readsb can use it.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832_sdr
blacklist rtl2832
blacklist rtl2830
blacklist dvb_usb_v2
EOF
# Unload them now so a reboot isn't needed.
for mod in rtl2832_sdr dvb_usb_rtl28xxu rtl2832 rtl2830 dvb_usb_v2; do
  modprobe -r "$mod" 2>/dev/null || true
done

echo "==> Installing rtl-sdr tools"
apt-get update
apt-get install -y rtl-sdr wget ca-certificates

echo "==> Installing readsb"
bash -c "$(wget -nv -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"

echo "==> Installing tar1090"
bash -c "$(wget -nv -O - https://github.com/wiedehopf/tar1090/raw/master/install.sh)"

echo
echo "==> Status"
systemctl is-active readsb && echo "readsb is running"
lsmod | grep -E 'dvb_usb_rtl28xxu|rtl2832' && echo "WARNING: TV tuner driver still loaded, reboot the Pi" || echo "TV tuner drivers are blocked"
echo
echo "Done. Map: http://$(hostname)/tar1090"
echo "Optional, for distances and range: sudo readsb-set-location <latitude> <longitude>"
