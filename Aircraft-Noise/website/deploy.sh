#!/usr/bin/env bash
# Copy the website's code from this Mac to the Pi (~/aircraft-website). The next daily publish
# picks it up; to publish straight away: ssh sebpi@sebpi aircraft-website/publish.sh
set -euo pipefail

host=sebpi@sebpi
# pages/ is the Pi's clone of the site repo, so --delete must leave it alone.
rsync -az --delete --exclude __pycache__ --exclude .DS_Store --exclude pages --exclude preview \
  "$(dirname "$0")/" "$host:aircraft-website/"
echo "Copied to $host:aircraft-website"
