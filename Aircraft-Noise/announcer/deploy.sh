#!/usr/bin/env bash
# Copy the announcer from this Mac to the Pi (~/aircraft-announcer) and restart it.
#
#   Aircraft-Noise/announcer/deploy.sh
#
# The first time, also run:  ssh -t sebpi@sebpi sudo bash aircraft-announcer/setup/install.sh
# Only this folder goes to the Pi: the collection services and their data are untouched.
set -euo pipefail

host=sebpi@sebpi
# .venv (made on the Pi by install.sh) is excluded, so --delete leaves it alone.
rsync -az --delete --exclude __pycache__ --exclude .DS_Store --exclude .venv \
  "$(dirname "$0")/" "$host:aircraft-announcer/"
echo "Copied to $host:aircraft-announcer"

# The service runs as your user, so it can be restarted without sudo: stop it and systemd
# starts it again with the new code (and model).
ssh "$host" 'pkill -u "$USER" -f "python -m aircraft_announcer" && echo "Announcer restarting" || true'
