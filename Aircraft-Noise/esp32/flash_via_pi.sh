#!/usr/bin/env bash
# Flash a firmware built on this Mac onto an ESP32 plugged into the Pi's USB.
#
#   ./flash_via_pi.sh bridge          # build esp32/bridge first (idf.py build)
#   ./flash_via_pi.sh node_a          # build with ./node/flash.sh first, or idf.py -B build_a ... build
#
# Copies the build output to the Pi and runs esptool there (in ~/esptool-venv, created on first
# use). Pauses the Pi services while flashing (see PAUSE_FILE in config.py), since the
# bridge-link service holds the serial port.
set -euo pipefail
cd "$(dirname "$0")"

host=sebpi@sebpi
case "${1:-}" in
  bridge) build=bridge/build ;;
  node_a) build=node/build_a ;;
  node_b) build=node/build_b ;;
  *) echo "Usage: $0 bridge|node_a|node_b" >&2; exit 1 ;;
esac
[[ -f $build/flash_args ]] || { echo "No build in $build: build it first." >&2; exit 1; }

# Everything esptool needs: the files listed in flash_args, and flash_args itself.
files=(flash_args $(awk '/^0x/ {print $2}' "$build/flash_args"))
remote=esp-flash/$1
ssh "$host" "rm -rf $remote && mkdir -p $remote"
(cd "$build" && tar cf - "${files[@]}") | ssh "$host" "tar xf - -C $remote"

ssh -t "$host" "
  set -e
  [[ -x ~/esptool-venv/bin/esptool ]] || { python3 -m venv ~/esptool-venv && ~/esptool-venv/bin/pip install -q esptool; }
  port=\$(ls /dev/serial/by-id/*CP210* /dev/serial/by-id/*Espressif* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -1 || true)
  [[ -n \$port ]] || { echo 'No ESP32 found on the Pi (try another cable: some USB-C cables are charge-only).'; exit 1; }
  # Pause the Pi services so the link lets go of the serial port (left paused if it already was).
  pause=~/aircraft-noise-data/PAUSED
  was_paused=0; [[ -e \$pause ]] && was_paused=1
  mkdir -p ~/aircraft-noise-data && touch \$pause && sleep 3
  cd $remote
  ~/esptool-venv/bin/esptool --chip esp32 --port \$port --baud 460800 write-flash @flash_args || rc=\$?
  [[ \$was_paused == 0 ]] && rm -f \$pause
  exit \${rc:-0}
"
