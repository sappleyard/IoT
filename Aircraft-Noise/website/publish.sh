#!/usr/bin/env bash
# Rebuild the public site on the Pi and push it to GitHub Pages. Run daily by cron (see README).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
pages="$here/pages"     # a clone of the site's repo
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/aircraft_pages -o IdentitiesOnly=yes"

git -C "$pages" pull --quiet --ff-only
# The generated data files in pages/data/ are left alone.
rsync -a --delete --exclude .git --exclude data/passes.json --exclude data/outlook.json \
  "$here/site/" "$pages/"
nice -n 19 python3 "$here/export.py" "$pages/data/passes.json"
# The 10-day outlook needs Open-Meteo: if that fails, keep yesterday's and publish the passes.
nice -n 19 python3 "$here/forecast/forecast.py" "$pages/data/outlook.json" || echo "Forecast failed"

git -C "$pages" add -A
if git -C "$pages" diff --cached --quiet; then
    echo "Nothing new"
else
    git -C "$pages" commit --quiet -m "Passes to $(date -d yesterday +%F)"
    git -C "$pages" push --quiet
    echo "Published"
fi
