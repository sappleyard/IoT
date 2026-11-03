#!/usr/bin/env bash
# Copy the Pi code from this Mac to the Pi (~/aircraft-noise) and restart the services.
#
#   Aircraft-Noise/pi/deploy.sh
#
# The first time, also run:  ssh -t sebpi@sebpi sudo bash aircraft-noise/setup/install.sh
# Data lives in ~/aircraft-noise-data on the Pi, so a deploy never touches it.
set -euo pipefail

host=sebpi@sebpi
rsync -az --delete --exclude __pycache__ --exclude .DS_Store \
  "$(dirname "$0")/" "$host:aircraft-noise/"
echo "Copied to $host:aircraft-noise"

# The services run as your user, so they can be restarted without sudo: stop them and
# systemd starts them again with the new code.
ssh "$host" 'pkill -u "$USER" -f "python3 -m aircraft_noise\." && echo "Services restarting" || true'
