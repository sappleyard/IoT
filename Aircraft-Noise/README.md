# Aircraft Noise

Measure how loud aircraft are at home, with two microphones and the Pi's ADS-B receiver
saying which plane was where, and announce each aircraft the mics hear on the Sonos.

**Privacy: no audio is recorded or stored.** Each mic node boils every second of sound down
to nine numbers (overall loudness and seven pitch bands) and throws the sound away. One-second
band levels can't reproduce speech.

```
 Mic A: living room ──┐  ESP-NOW long-range radio
 (N window, floor 2)  │
                      ▼
 Mic B: bedroom ───► Bridge ESP32 ──USB──► Raspberry Pi ◄── RTL-SDR (ADS-B)
 (S window, floor 3)  (relay only)          ├─ bridge-link: mic levels    ─┐
                                            ├─ adsb-logger: aircraft       ─┴► noise.db (SQLite)
                                            └─ announcer: model hears an aircraft ─► Sonos
```

## What's measured

Each node, every second (all levels in dB, uncalibrated; the Pi stores each mic's `cal_db` to add):

| Field | Meaning |
|---|---|
| `laeq` | A-weighted level over the second: how loud it sounds, like a phone sound meter, dB(A) |
| `lamax` | Loudest 125 ms within the second, dB(A) |
| `band_63` … `band_4000` | Unweighted level in each octave band (63, 125, 250, 500 Hz, 1, 2, 4 kHz): a coarse sound fingerprint, the kind airport noise monitors use to tell aircraft from traffic |

The ADS-B logger stores every aircraft position within 60 km, about once a second each, and
one `passes` row per aircraft that comes within 5 km (its closest moment and distance): the
ground truth that mic detections are checked against. Both mics record all the time, plane or
not: training needs plenty of "no aircraft" examples too.

The radio link is ESP-NOW in long-range mode, **encrypted** (AES-CCMP); the bridge only
accepts the two nodes listed in `protocol.h`.

## The database: `~/aircraft-noise-data/noise.db` on the Pi

| Table | One row per |
|---|---|
| `levels` | mic per second: `ts, mic ('A'/'B'), seq, uptime_ms, flags, laeq, lamax, band_63 … band_4000, rssi, cal_db` |
| `aircraft_positions` | aircraft position: `ts, hex, callsign, lat, lon, alt_baro_ft, alt_geom_ft, speed_kt, track_deg, dist_km` |
| `passes` | aircraft within 5 km: `hex, callsign, first_ts, last_ts, closest_ts, closest_slant_km, closest_dist_km, closest_alt_ft, speed_kt, track_deg` |
| `logger_coverage` | period the ADS-B logger was running with fresh data (outside them, "no aircraft" means nothing) |
| `meta` | fact, e.g. the receiver location |

```sql
-- Mic B's calibrated loudness over the last hour
SELECT datetime(ts, 'unixepoch', 'localtime'), laeq + cal_db FROM levels
WHERE mic = 'B' AND ts > strftime('%s', 'now') - 3600;
```

`flags = 1` means the mic sent no signal (dead or disconnected). Gaps in `seq` are lost
packets; `uptime_ms` going backwards means the node restarted.

## Files

Grouped by where each part runs. `collection/` and `announcer/` run on the hardware, each with
its own `deploy.sh` that copies just that folder to the Pi, so analysis and training code never
goes there. Only an exported model (`announcer/models/`) does.

```
collection/                 COLLECTING THE DATA (runs on the boards and the Pi)
  esp32/
    flash.sh                build + flash:  ./flash.sh bridge|node [pi|<port>]
    common/                 shared by both firmwares: packet format and board list (protocol.h),
                            radio; secrets.h holds the encryption keys (made by flash.sh, git-ignored)
    bridge/main/main.c      relays node packets to the Pi as JSON lines
    node/main/main.c, mic.c measures the mic, sends a packet a second
  pi/
    deploy.sh               copy this folder to the Pi and restart the services
    location.json           home location {"lat": .., "lon": ..}: git-ignored, never published
    setup/                  install.sh and the two systemd services
    aircraft_noise/
      config.py             every setting, including the mics (MAC, name, location, calibration)
      db.py                 the database schema
      adsb_logger.py        service: aircraft positions -> database
      bridge_link.py        service: mic levels -> database, health log every 10 min
announcer/                  ANNOUNCING HEARD AIRCRAFT (runs on the Pi)
  deploy.sh                 copy this folder to the Pi (~/aircraft-announcer) and restart it
  setup/                    install.sh (Python environment, voice) and the systemd service
  models/                   exported models: NAME.onnx + NAME.json (made by model/train.py --export)
  aircraft_announcer/
    config.py               every setting: model, quiet hours, gap, volume, matching distances
    features.py             model input from the levels; model/dataset.py uses the same code
    detector.py             runs the chosen model on each mic's latest 30 s
    flights.py              which aircraft was heard; route, airline, model lookups (cached)
    speech.py               the sentence, spoken by Piper (offline)
    sonos.py                plays it over whatever the Sonos is playing
    __main__.py             service: detect -> match -> look up -> announce, every second
analysis/                   EXPLORING THE DATA (Mac)
  summary.py                report figures + summary from a copy of the Pi's database
  audibility.py             how audible aircraft are by distance/direction/time; fits the label model
  data/, figures/           database copy and outputs (git-ignored: they hold the home location)
model/                      THE AIRCRAFT CLASSIFIER (Mac)
  dataset.py                labelled 30 s windows from the database copy
  train.py                  trains and compares the detectors (CNN, boosting, threshold);
                            --export NAME writes the CNN to announcer/models/
  data/                     windows, models, results (git-ignored)
docs/                       design summary, training summaries
  reference/                Heathrow's runway alternation programme, departure routes map
data-archive/               old databases (git-ignored)
```

## Hardware

Three ESP32 dev boards (ESP32-D0WD-V3, CH340 USB, 4 MB flash), all on mains power:

| Board | MAC | Role |
|---|---|---|
| Bridge | `28:05:A5:34:6B:38` | On the Pi's USB. No mic. |
| Node A | `28:05:A5:35:B6:50` | Living room, north-facing window, floor 2 |
| Node B | `28:05:A5:35:E0:EC` | Bedroom, south-facing window, floor 3 |

INMP441 wiring on each node: VDD → 3V3, GND → GND, **L/R → GND**, SCK → GPIO 26, WS → GPIO 25,
SD → GPIO 33. Keep the wires short. A faulty INMP441 can short the supply: that one crashed the
Pi's USB controller, so try a new or suspect mic on a phone charger first.

The ADS-B antenna stays indoors: it already sees every aircraft within 60 km in all directions,
updating close aircraft about once a second, and only nearby aircraft matter for noise.

## Setting up

ESP-IDF 5.5 is installed on the Mac (`~/esp/esp-idf`). Pi: `ssh sebpi@sebpi` over Tailscale.

0. Create `collection/pi/location.json` with the home location: `{"lat": 51.5, "lon": -0.1}`.
1. Flash the boards (each plugged into the Pi, or the Mac with `./flash.sh node`):
   `collection/esp32/flash.sh bridge pi`, then `collection/esp32/flash.sh node pi` for each node.
2. Copy the code to the Pi: `collection/pi/deploy.sh`
3. Once, on the Pi (needs your password): `ssh -t sebpi@sebpi sudo bash aircraft-noise/setup/install.sh`
4. Start collecting: `ssh sebpi@sebpi rm aircraft-noise-data/PAUSED`

Health: `ssh sebpi@sebpi journalctl -u bridge-link -u adsb-logger -f`. Every 10 minutes
bridge-link logs a line per mic (received, lost, restarts, mic-fault seconds, signal strength).

**Pausing** (e.g. to flash a board on the Pi; `flash.sh` does this for you):
`ssh sebpi@sebpi touch aircraft-noise-data/PAUSED`, and `rm` it to resume.

## Calibration

Do this with both nodes in their weatherproof cases, so the case is part of the calibration:

1. Put the two nodes side by side, the same distance (1-2 m) from a speaker, with a phone
   sound-meter app (set to dB(A)) next to them.
2. Play pink noise (any "pink noise" video or app) at three steady volumes, about 50, 65 and
   80 dB(A) on the phone, for a minute each, plus a minute of quiet. Note the phone's readings.
3. From the `levels` rows for those minutes: B minus A gives the mics' difference precisely
   (same sound, same moment); the phone reading minus each mic gives the absolute offset
   (only as good as the phone, about ±5 dB). Pink noise covers every octave band, so it also
   shows whether a case colours some bands.
4. Put the offsets in `MICS` in `collection/pi/aircraft_noise/config.py` and deploy. No reflash needed.

Current offsets: A −6, B −6. Side by side the mics agree within 0.1 dB(A) (22 Oct); the absolute −6 comes from a phone app, so it's approximate until the pink-noise calibration.

## Analysis

`python3 analysis/summary.py` copies the Pi's database (consistently) and writes figures and a
summary to `analysis/figures/`: levels over time, the daily profile, octave-band spectra of
aircraft vs quiet, and the sound propagation model (LAmax = L1 - 20 log10(r) - alpha r, fitted
to the passes the mics heard, background-corrected), with detection rate by distance.

`python3 analysis/audibility.py --local` measures how far each aircraft's sound stands out from
aircraft-free moments of the same period (an AUC: 0.5 = no different, 1 = always louder) against
distance, direction, type and day/night, out to 20 km, and fits a model that predicts it from
ADS-B alone (`analysis/data/audibility.json`). `model/dataset.py` labels windows with it instead
of fixed distances. Findings so far: by day aircraft beyond ~3 km can't be told from household
noise by level; at night they stand out to ~13 km; the approach path north of the house is much
louder than the one south of it, despite being further away.

## Announcer

Runs on the Pi beside the collection services and only reads their database, so collecting
carries on unchanged. Every second it runs the model on each mic's last 30 s (the same windows
it was trained on). When the model hears an aircraft it finds which one: the nearest aircraft
whose sound, sent slant / 343 m/s earlier, was arriving then (within 3 km by day, 8 km at night).
Then it says, on the Sonos, over whatever is playing:

> British Airways 2 7 1, from Frankfurt-am-Main to London Heathrow, at 3,100 feet, an Airbus A three twenty.

- **Route, airport and airline names** from the VRS standing data (free; one file per airline,
  downloaded when first needed and refreshed weekly). **Aircraft model** from adsbdb (free),
  cached for a month. Anything unknown is left out of the sentence.
- **Each aircraft once per pass**, at least 30 s between announcements, none 20:00-09:00.
- **Every heard aircraft is logged** in `~/aircraft-noise-data/announcer.db` (table `heard`:
  who, when, how far, which mics, what was said, or why not), quiet hours included: the
  history v2's "unique aircraft only" filter will use.
- Voice: Piper (`en_GB-alan-medium`), offline on the Pi, about 1 s per sentence.

**Set up** (after the collection): `announcer/deploy.sh`, then once on the Pi (needs your
password) `ssh -t sebpi@sebpi sudo bash aircraft-announcer/setup/install.sh`.
Logs: `ssh sebpi@sebpi journalctl -u aircraft-announcer -f` (a health line every 10 min).
Test the speaker: `ssh sebpi@sebpi 'cd aircraft-announcer && .venv/bin/python -m aircraft_announcer --say "Hello"'`.
Settings (volume, quiet hours, gap, distances) are in `announcer/aircraft_announcer/config.py`:
change, then `announcer/deploy.sh`.

**Pausing announcements** (heard aircraft are still logged, with status `paused`; no restart):
`ssh sebpi@sebpi touch aircraft-noise-data/ANNOUNCER_PAUSED`, and `rm` it to resume.

**Using a newly trained model:**
1. `python3 model/dataset.py` (latest data), then
   `python3 model/train.py --heli-weight 4 --export cnn-2026-12-01` (any name). This writes
   `announcer/models/cnn-2026-12-01.onnx` and `.json` (threshold, feature settings, test scores).
2. Set `MODEL = "cnn-2026-12-01"` in `announcer/aircraft_announcer/config.py`.
3. `announcer/deploy.sh`. The log's first line names the model and its scores.

Any architecture works (ONNX), as long as it takes the windows from `features.py`. If those
windows are ever changed, the old models are refused at start-up rather than run on the wrong
input: retrain and export after changing `features.py`.

## Next steps

See the design summary (`docs/`): v2 (announce only unique aircraft, scored from the `heard`
history), then the publishing API and dashboard.
