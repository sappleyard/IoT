# IoT

Home IoT projects. Each project lives in its own subfolder.

## Hardware

- **sebpi**: Raspberry Pi (Debian 13 trixie, aarch64), reachable as `ssh sebpi@sebpi` over Tailscale
  (tailnet IP 100.75.211.110). Key-based SSH works from the Mac; `sudo` on the Pi needs a password,
  so root-level setup is done with scripts the user runs themselves (`ssh -t sebpi@sebpi sudo bash <script>`).
- RTL-SDR stick (RTL2832U, USB ID 0bda:2838) with an ADS-B antenna, plugged into the Pi.

## Projects

- [ADS-B-Test/](ADS-B-Test/): receive aircraft ADS-B signals on the Pi with readsb and read them from Python.

## Workflow

- The repo is pushed to GitHub (private: github.com/sappleyard/IoT). A PostToolUse hook
  (`.claude/hooks/autocommit.sh`) commits and pushes each file Claude edits. Changes made
  any other way still need committing and pushing by hand.
- Python code uses the standard library only unless a dependency is clearly worth it.
