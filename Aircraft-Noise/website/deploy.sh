#!/usr/bin/env bash
# Copy the website's code from this Mac to the Pi (~/aircraft-website). The next daily publish
# picks it up; to publish straight away: ssh sebpi@sebpi aircraft-website/publish.sh
set -euo pipefail

host=sebpi@sebpi
# pages/ (the Pi's clone of the site repo) and publish.log live only on the Pi: --delete must leave them.
rsync -az --delete --exclude __pycache__ --exclude .DS_Store --exclude pages --exclude preview --exclude publish.log \
  "$(dirname "$0")/" "$host:aircraft-website/"
echo "Copied to $host:aircraft-website"
