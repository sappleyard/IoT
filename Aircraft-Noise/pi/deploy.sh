#!/usr/bin/env bash
# Copy the repo from this Mac to the Pi (~/IoT). Run from anywhere:
#
#   Aircraft-Noise/pi/deploy.sh
#
# Data lives in ~/aircraft-noise-data on the Pi, outside ~/IoT, so a deploy never touches it.
# The first time, run setup/install.sh on the Pi afterwards (it prints the command below).
set -euo pipefail

host=sebpi@sebpi
repo=$(cd "$(dirname "$0")/../.." && pwd)

rsync -az --delete \
  --exclude .git --exclude __pycache__ --exclude .DS_Store \
  --exclude 'build*/' --exclude sdkconfig --exclude sdkconfig.old \
  "$repo/" "$host:IoT/"
echo "Copied $repo to $host:IoT"
echo
echo "First time:   ssh -t $host sudo bash IoT/Aircraft-Noise/pi/setup/install.sh <lat> <lon>"
echo "After that:   ssh -t $host sudo systemctl restart adsb-logger bridge-link"
