# Pi side

Everything here runs on `sebpi` except `audio/record.py`, which also runs on a laptop.

```
pi/
├── deploy.sh                 copy the repo from the Mac to the Pi (~/IoT)
├── setup/
│   ├── install.sh            one-time setup: receiver location, pyserial, systemd services
│   ├── piaware.sh            point piaware at readsb (FlightAware feeder)
│   └── systemd/              adsb-logger.service, bridge-link.service
└── aircraft_noise/           the Python package
    ├── config.py             all settings: paths, ports, thresholds, mic calibration
    ├── db.py                 SQLite schema shared by everything
    ├── geo.py                distances
    ├── adsb/logger.py        step 1: aircraft.json → database        (service: adsb-logger)
    ├── nodes/link.py         step 3: bridge serial ↔ database/WAVs   (service: bridge-link)
    ├── nodes/send.py         send a command to the nodes ("listen 2 90")
    ├── nodes/gaps.py         step 3 range test: lost packets per node
    ├── nodes/serial_protocol.py   the line format on USB serial
    ├── audio/wav_writer.py   hourly WAV files named by start time
    ├── audio/record.py       step 4: record a node plugged into a laptop
    └── labelling/label_clips.py   step 4: 1 s clips labelled from the ADS-B log
```

Data lives in `~/aircraft-noise-data/` on the Pi (outside the repo, so deploys never touch it):
`noise.db` (SQLite), `audio/` (hourly WAVs), `dataset/` (labelled clips).

Only dependency beyond the standard library: pyserial (`python3-serial`, installed by install.sh).

## Setup (once)

From the Mac:

```
Aircraft-Noise/pi/deploy.sh
ssh -t sebpi@sebpi sudo bash IoT/Aircraft-Noise/pi/setup/install.sh <latitude> <longitude>
```

The latitude/longitude set readsb's receiver location, so every aircraft gets a distance
(and aircraft further than `MAX_RANGE_KM` aren't logged). install.sh starts both services:
`adsb-logger` starts logging straight away, and `bridge-link` waits until the bridge ESP32 is plugged in.

**Pausing collection** (no sudo needed): `ssh sebpi@sebpi touch aircraft-noise-data/PAUSED`.
Both services idle and the link lets go of the serial port, e.g. to plug in and flash a different
board. Resume with `ssh sebpi@sebpi rm aircraft-noise-data/PAUSED`. Nothing is logged while
paused, and the labeller knows it (no "no aircraft" labels for that time).

After changing code: run `deploy.sh` again, then `ssh -t sebpi@sebpi sudo systemctl restart adsb-logger bridge-link`.

## Step 1: ADS-B logging

```
journalctl -u adsb-logger -f                          # every 10 min: how many positions were saved
python3 -c "import sqlite3; print(sqlite3.connect('/home/sebpi/aircraft-noise-data/noise.db').execute('select count(*) from aircraft_positions').fetchone())"
```

Each row is one position: time, hex, callsign, lat, lon, altitudes, speed, track, ground distance.
The `logger_coverage` table records when the logger was running, so the labeller never
mistakes an outage for an empty sky.

### FlightAware feeder

Being a feeder doubles the free AeroAPI allowance used in step 6. piaware just relays what
readsb already decodes. At the time of writing FlightAware's apt repo had no Debian 13 (trixie) packages, so:

1. Install piaware: check https://www.flightaware.com/adsb/piaware/install for trixie
   support first. If there's none yet, the
   [FlightAware forum thread on trixie](https://discussions.flightaware.com/t/install-piaware-on-debian-13-trixie/99296)
   has community-built packages and build-from-source scripts. Read any script before running it with sudo.
2. `ssh -t sebpi@sebpi sudo bash IoT/Aircraft-Noise/pi/setup/piaware.sh` sets
   `receiver-type relay`, `receiver-host 127.0.0.1`, `receiver-port 30005` and prints the feeder ID to claim.

## Step 3: talking to the nodes

```
journalctl -u bridge-link -f                          # bridge messages, commands, errors
cd ~/IoT/Aircraft-Noise/pi
python3 -m aircraft_noise.nodes.send status           # bridge MAC + nodes it has heard
python3 -m aircraft_noise.nodes.send listen 2 90      # node B: listen for 90 s
python3 -m aircraft_noise.nodes.send stop 2           # node B: back to sleep
python3 -m aircraft_noise.nodes.gaps                  # packet loss per node, last 24 h
```

Every report goes into the `node_reports` table, stamped with the Pi's clock when it arrives
(well under a second late, which is plenty for matching against flights).

**Mic calibration:** compare a node's `level_db` with a phone sound-meter app in the same
spot, and put the difference (phone − node) in `MIC_CAL_OFFSET_DB` in config.py. Each report stores
the offset that was in force, so calibrated level = `level_db + cal_offset_db`.

## Step 4: collecting labelled audio

1. **Bridge audio** (only if the bridge has a mic; it doesn't at the moment): set
   `RECORD_BRIDGE_AUDIO = True` in config.py and turn on *Bridge has a microphone* in the bridge
   firmware. Hourly WAVs then appear in `~/aircraft-noise-data/audio/`.
2. **Remote spots:** put a node where it will live, plug it into a laptop and record:
   ```
   pip install pyserial
   cd Aircraft-Noise/pi
   python3 -m aircraft_noise.audio.record --out ~/recordings
   ```
   Start it within 30 s of plugging the node in (or press RST). Then copy the WAVs into the Pi's audio folder:
   `scp ~/recordings/*.wav sebpi@sebpi:aircraft-noise-data/audio/`
3. **Label:**
   ```
   python3 -m aircraft_noise.labelling.label_clips --dry-run   # counts only
   python3 -m aircraft_noise.labelling.label_clips             # writes the clips
   ```
   Listen to about 50 clips of each label (`dataset/aircraft/`, `dataset/no_aircraft/`) and adjust
   `--aircraft-km` / `--clear-km` (or their defaults in config.py) until nearly all are right.
   Aim for several hundred aircraft clips before the first training run.
4. **Confusers:** also record TV, music, hoover, kettle, washing machine, doors, talking, rain
   and road traffic, ideally when no plane is near, so the labeller files them as "no aircraft".
   The public ESC-50 dataset has an
   airplane class and plenty of household sounds to pad this out.
