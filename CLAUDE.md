# IoT

Home IoT projects. Each project lives in its own subfolder.

## Hardware

- **sebpi**: Raspberry Pi (Debian 13 trixie, aarch64), reachable as `ssh sebpi@sebpi` over Tailscale
  (tailnet IP 100.75.211.110). Key-based SSH works from the Mac; `sudo` on the Pi needs a password,
  so root-level setup is done with scripts the user runs themselves (`ssh -t sebpi@sebpi sudo bash <script>`).
- RTL-SDR stick (RTL2832U, USB ID 0bda:2838) with an ADS-B antenna, plugged into the Pi.
- Sonos Era 100 ("Kitchen") on the LAN: plays audio clips over whatever is on (local API, port 1443).

## Projects

- [Aircraft-Noise/](Aircraft-Noise/): two ESP32 + INMP441 mic nodes send per-second levels (LAeq, LAmax,
  7 octave bands; never audio) over ESP-NOW long range to a bridge ESP32 on the Pi's USB; Pi services
  log those and ADS-B positions to SQLite (`~/aircraft-noise-data/noise.db`). Folders by where they run:
  `collection/` (deployed: `esp32/`, ESP-IDF 5.5, one firmware for both nodes, mics identified by MAC;
  `pi/`, Python services plus `archive.py`: nightly, finished UTC days go to the private R2 bucket
  `aircraft-noise-archive` as gzipped CSVs and the Pi keeps 30 days; `analysis/summary.py`'s
  fetch merges pruned days back; keep this bucket and its keys separate from any other
  project's Cloudflare resources), `announcer/` (deployed separately: runs an exported model on the live
  levels, announces unusual heard aircraft on the Sonos, `uniqueness.py` scores them), `website/` (public site at
  sappleyard.github.io/flight-passes, built daily on the Pi by `website/publish.sh`: `export.py`
  publishes only date, hour, callsign, airline, route, aircraft, type, heard; never distances,
  altitudes or exact times; `forecast/` writes the 10-day forecast, querying Open-Meteo at
  Heathrow only, never the house),
  then Mac-only `analysis/` and `model/` (classifier
  training: never deployed; `train.py --export` writes models into `announcer/models/`), `docs/`
  and the git-ignored `data-archive/`. `announcer/aircraft_announcer/features.py` is the one copy
  of the model-input code, used by training and the announcer alike.
  The project README is the single source of docs. Privacy matters to the user: don't add audio
  recording, and never commit the home location (`location.json`, databases, `analysis/data/`).

## Workflow

- The repo is pushed to GitHub (public: github.com/sappleyard/IoT). A PostToolUse hook
  (`.claude/hooks/autocommit.sh`) commits and pushes each file Claude edits. Changes made
  any other way still need committing and pushing by hand.
- Python code uses the standard library only unless a dependency is clearly worth it.
- No AI attribution in git: no `Co-Authored-By: Claude` (or similar) lines in commit messages or
  PR descriptions. History was rewritten on 2026-10-25 to remove them.
