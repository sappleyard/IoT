#!/usr/bin/env bash
# Build and flash node A or B. Each node gets its own build folder and settings.
#
#   ./flash.sh a                       # build, flash and open the serial monitor
#   ./flash.sh b /dev/cu.usbmodem101   # pick the port yourself
#   ./flash.sh a menuconfig            # change node A's settings
#
# Needs ESP-IDF 5.3+ in your shell (. ~/esp/esp-idf/export.sh). Target defaults to
# esp32 (ESP32-WROOM-32 dev boards); for an ESP32-S3 run:  IDF_TARGET=esp32s3 ./flash.sh a
set -euo pipefail
cd "$(dirname "$0")"

node=$(echo "${1:-}" | tr '[:upper:]' '[:lower:]')
if [[ "$node" != a && "$node" != b ]]; then
  echo "Usage: $0 a|b [port|menuconfig]" >&2
  exit 1
fi
export IDF_TARGET=${IDF_TARGET:-esp32}

args=(-B "build_$node" -D SDKCONFIG="build_$node/sdkconfig"
      -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.node_$node")

if [[ "${2:-}" == menuconfig ]]; then
  idf.py "${args[@]}" menuconfig
elif [[ -n "${2:-}" ]]; then
  idf.py "${args[@]}" -p "$2" flash monitor
else
  idf.py "${args[@]}" flash monitor
fi
