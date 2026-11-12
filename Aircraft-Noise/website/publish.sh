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
# Name each script and stylesheet by its contents in the pages, so a browser never runs a cached
# old script against new data (GitHub Pages lets browsers keep files for 10 minutes).
for f in "$pages"/*.js "$pages"/*.css; do
    name="$(basename "$f")"
    v="$(md5sum "$f" | cut -c1-8)"
    sed -i "s|\"$name\"|\"$name?v=$v\"|g" "$pages"/*.html
done
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
