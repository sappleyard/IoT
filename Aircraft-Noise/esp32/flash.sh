#!/usr/bin/env bash
# Build and flash the bridge or a node.
#
#   ./flash.sh bridge pi                    # board plugged into the Pi
#   ./flash.sh node pi
#   ./flash.sh node                         # board plugged into this Mac (port found automatically)
#   ./flash.sh node /dev/cu.usbserial-110   # ...or name the port
#
# Both nodes get the same firmware: the Pi tells them apart by MAC (pi/aircraft_noise/config.py).
# Flashing via the Pi pauses its services while it runs, since they hold the serial port.
set -euo pipefail
cd "$(dirname "$0")"

board=${1:-}
where=${2:-}
if [[ $board != bridge && $board != node ]]; then
  echo "Usage: $0 bridge|node [pi|<port>]" >&2
  exit 1
fi
command -v idf.py >/dev/null || . ~/esp/esp-idf/export.sh >/dev/null
idf.py -C "$board" build

if [[ $where != pi ]]; then
  idf.py -C "$board" ${where:+-p "$where"} flash
  exit
fi

# Copy the build to the Pi and flash it there with esptool.
host=sebpi@sebpi
files=(flash_args $(awk '/^0x/ {print $2}' "$board/build/flash_args"))
ssh "$host" 'rm -rf esp-flash && mkdir esp-flash'
(cd "$board/build" && tar cf - "${files[@]}") | ssh "$host" 'tar xf - -C esp-flash'
ssh "$host" '
  set -e
  [[ -x ~/esptool-venv/bin/esptool ]] || { python3 -m venv ~/esptool-venv && ~/esptool-venv/bin/pip install -q esptool; }
  port=$(ls /dev/ttyUSB* 2>/dev/null | head -1)
  [[ -n $port ]] || { echo "No ESP32 found on the Pi (some USB-C cables are charge-only)." >&2; exit 1; }
  pause=~/aircraft-noise-data/PAUSED
  was_paused=0; [[ -e $pause ]] && was_paused=1
  mkdir -p ~/aircraft-noise-data && touch $pause && sleep 3   # let the services release the port
  cd esp-flash
  ~/esptool-venv/bin/esptool --chip esp32 --port "$port" --baud 460800 write-flash @flash_args || rc=$?
  [[ $was_paused == 1 ]] || rm -f $pause
  exit ${rc:-0}
'
