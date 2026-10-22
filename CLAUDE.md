# IoT

Home IoT projects. Each project lives in its own subfolder.

## Hardware

- **sebpi**: Raspberry Pi (Debian 13 trixie, aarch64), reachable as `ssh sebpi@sebpi` over Tailscale
  (tailnet IP 100.75.211.110). Key-based SSH works from the Mac; `sudo` on the Pi needs a password,
  so root-level setup is done with scripts the user runs themselves (`ssh -t sebpi@sebpi sudo bash <script>`).
- RTL-SDR stick (RTL2832U, USB ID 0bda:2838) with an ADS-B antenna, plugged into the Pi.

## Projects

- [ADS-B-Test/](ADS-B-Test/): receive aircraft ADS-B signals on the Pi with readsb and read them from Python.
- [Aircraft-Noise/](Aircraft-Noise/): two ESP32 + INMP441 mic nodes send per-second levels (LAeq, LAmax,
  7 octave bands; never audio) over ESP-NOW long range to a bridge ESP32 on the Pi's USB; Pi services
  log those and ADS-B positions to SQLite (`~/aircraft-noise-data/noise.db`). Kept deliberately small:
  `esp32/` (ESP-IDF 5.5, one firmware for both nodes, mics identified by MAC) and `pi/` (four Python
  files). The README is the single source of docs. Old data is archived (git-ignored) in
  `Aircraft-Noise/data-archive/`. Privacy matters to the user: don't add audio recording.

## Workflow

- The repo is pushed to GitHub (private: github.com/sappleyard/IoT). A PostToolUse hook
  (`.claude/hooks/autocommit.sh`) commits and pushes each file Claude edits. Changes made
  any other way still need committing and pushing by hand.
- Python code uses the standard library only unless a dependency is clearly worth it.
