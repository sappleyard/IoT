#!/usr/bin/env bash
# Set up the announcer on the Pi (safe to re-run). Run on the Pi after deploy.sh:
#
#   sudo bash ~/aircraft-announcer/setup/install.sh
#
# 1. Makes the announcer's own Python environment (.venv) with numpy, onnxruntime and Piper.
# 2. Downloads the Piper voice into ~/aircraft-noise-data/announcer-cache/voices.
# 3. Installs and (re)starts the aircraft-announcer service.
# Needs the collection set up first (it reads its database and ~/aircraft-noise/location.json).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo: sudo bash $0" >&2
  exit 1
fi
user=${SUDO_USER:?run this with sudo from your normal account, not as root}
dir=$(cd "$(dirname "$0")/.." && pwd)
home=$(getent passwd "$user" | cut -d: -f6)

apt-get install -y python3-venv
as_user() { sudo -u "$user" -H "$@"; }

as_user python3 -m venv "$dir/.venv"
as_user "$dir/.venv/bin/pip" install --quiet --upgrade pip
as_user "$dir/.venv/bin/pip" install --quiet -r "$dir/requirements.txt"

voice=$(cd "$dir" && as_user .venv/bin/python -c 'from aircraft_announcer.config import VOICE; print(VOICE)')
voices="$home/aircraft-noise-data/announcer-cache/voices"
as_user mkdir -p "$voices"
if [[ ! -f $voices/$voice.onnx ]]; then
  (cd "$voices" && as_user "$dir/.venv/bin/python" -m piper.download_voices "$voice")
fi

sed -e "s|@USER@|$user|g" -e "s|@DIR@|$dir|g" \
  "$dir/setup/aircraft-announcer.service" > /etc/systemd/system/aircraft-announcer.service
systemctl daemon-reload
systemctl enable aircraft-announcer
systemctl restart aircraft-announcer
systemctl --no-pager --lines=5 status aircraft-announcer || true
echo
echo "Logs: journalctl -u aircraft-announcer -f"
echo "Test the speaker: cd $dir && .venv/bin/python -m aircraft_announcer --say 'Hello'"
