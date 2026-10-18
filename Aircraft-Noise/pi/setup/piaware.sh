#!/usr/bin/env bash
# Feed FlightAware from the readsb that's already running (step 1). Being a feeder doubles
# the free AeroAPI allowance used in step 6. piaware only relays what readsb decodes; it
# doesn't touch the SDR stick.
#
#   sudo bash ~/IoT/Aircraft-Noise/pi/setup/piaware.sh
#
# piaware has to be installed first. See pi/README.md: FlightAware's apt repo didn't have
# Debian 13 (trixie) packages at the time of writing.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi
if ! command -v piaware-config >/dev/null; then
  echo "piaware isn't installed yet. See 'FlightAware feeder' in pi/README.md." >&2
  exit 1
fi

echo "==> Pointing piaware at readsb's Beast output (127.0.0.1:30005)"
piaware-config receiver-type relay
piaware-config receiver-host 127.0.0.1
piaware-config receiver-port 30005
systemctl restart piaware

echo
echo "Feeder ID: $(piaware-config -show feeder-id 2>/dev/null || echo 'not assigned yet, check again in a minute')"
echo "Claim it (while logged in to FlightAware): https://flightaware.com/adsb/piaware/claim"
