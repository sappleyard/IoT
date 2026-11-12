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
| `archived` | day's file in the archive: `day, tbl, rows, bytes, md5, uploaded_ts, pruned_ts` (see [Archive](#archive)) |
| `meta` | fact, e.g. the receiver location |

```sql
-- Mic B's calibrated loudness over the last hour
SELECT datetime(ts, 'unixepoch', 'localtime'), laeq + cal_db FROM levels
WHERE mic = 'B' AND ts > strftime('%s', 'now') - 3600;
```

`flags = 1` means the mic sent no signal (dead or disconnected). Gaps in `seq` are lost
packets; `uptime_ms` going backwards means the node restarted.

## Archive

The database grows about 67 MB a day (positions 55%, levels 28%, indexes the rest), and an SD
card is a single copy that can fail. So the Pi keeps only the last **30 days** of levels and
positions, and every finished day is stored in a private **Cloudflare R2** bucket,
`aircraft-noise-archive`, that belongs to this project only:

    levels/2026-11-07.csv.gz   aircraft_positions/2026-11-07.csv.gz
    passes/2026-11-07.csv.gz   logger_coverage/2026-11-07.csv.gz

One gzipped CSV per table per UTC day, written once and never changed: about 13 MB a day for two
mics (4.7 GB a year; R2's free tier is 10 GB, then $0.015/GB a month). `collection/pi/
aircraft_noise/archive.py` runs nightly at 01:15 by cron on the Pi:

1. **Upload** every finished day not in the `archived` table yet, then read each file's size and
   MD5 back from R2; only a match is recorded.
2. **Prune**: for days over 30 days old, check the levels and positions files on R2 again, then
   delete those rows from the Pi an hour at a time, so the services never wait long.
   `passes` and `logger_coverage` are under 1 MB and stay. SQLite reuses the freed space, so
   `noise.db` levels off at about 2 GB instead of growing.

If an upload fails nothing is deleted, and the next night carries on where it stopped. The
log's last line each night gives the archive's size and the Pi's free space:
`ssh sebpi@sebpi tail aircraft-noise-data/archive.log`. `--dry-run` says what it would do.

**Scaling:** the Pi's storage stays at 30 days however long it runs; each extra mic node adds
about 2.3 MB a day to the archive; and day files can be fetched by date range without loading the
rest. **On the Mac**, `summary.py`'s copy of the database (also used by `audibility.py` and
`model/dataset.py`) puts the pruned days back from R2, downloading each file once into
`analysis/data/archive/`, so analysis and training still see every day. **If the Pi is lost**,
`python3 -m aircraft_noise.archive --restore OUT.db` (run from `collection/pi/`) rebuilds a
database from the archive alone, at most one day behind.

**Privacy:** the archive holds aircraft positions and distances near the house, so the bucket is
private and its keys stay off git. `meta` (the home location) is never uploaded; `--restore` fills
it back in from `location.json`.

**Kept apart from everything else:** the bucket is this project's alone. Its tokens are
limited to that one bucket, so they can't reach anything else in the Cloudflare account, and
no other project's code or keys touch it. The keys live in an aircraft-noise-only file, and
nothing here uses Wrangler or Workers.

**Setting up** (once):

1. Cloudflare dashboard → R2 → Create bucket `aircraft-noise-archive` (location: automatic).
2. R2 → Manage API tokens → Create API token, twice, both "Apply to specific buckets only" →
   `aircraft-noise-archive`:
   `aircraft-noise sebpi` with **Object Read & Write** (for the Pi), and
   `aircraft-noise mac` with **Object Read** only (for the Mac's analysis copy).
3. Put each token's keys on its machine, readable by you only:

       ssh sebpi@sebpi 'mkdir -p ~/.config/aircraft-noise && umask 077 && cat > ~/.config/aircraft-noise/r2.json'
       mkdir -p ~/.config/aircraft-noise && (umask 077; cat > ~/.config/aircraft-noise/r2.json)

   then paste `{"account_id": "…", "bucket": "aircraft-noise-archive", "access_key_id": "…",
   "secret_access_key": "…"}` and press Ctrl-D.
4. Deploy (`collection/pi/deploy.sh`), upload what's there so far, then add the cron job:

       ssh sebpi@sebpi 'cd aircraft-noise && python3 -m aircraft_noise.archive'
       ssh sebpi@sebpi '(crontab -l 2>/dev/null; echo "15 1 * * * cd \$HOME/aircraft-noise && nice -n 19 python3 -m aircraft_noise.archive >> \$HOME/aircraft-noise-data/archive.log 2>&1") | crontab -'

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
      archive.py            nightly: finished days -> Cloudflare R2, deletes old ones from the Pi
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
website/                    THE PUBLIC SITE (built on the Pi, served by GitHub Pages)
  deploy.sh                 copy this folder to the Pi (~/aircraft-website)
  export.py                 passes -> site/data/passes.json: the only data that leaves the house
  publish.sh                daily on the Pi: export, forecast, then push to github.com/sappleyard/flight-passes
  forecast/                 the Outlook's 10-day forecast: forecast.py (writes outlook.json),
                            model.py (runway direction chain, counts), weather.py (Open-Meteo,
                            Heathrow only), backtest.py (scores it against past days)
  site/                     the static pages (Passes, Outlook), styled after axolokl.co.uk
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
If that aircraft is unusual (below), it says, on the Sonos, over whatever is playing:

> First time heard: Air Astana 6 6 3, from Almaty to London Heathrow, at 3,400 feet, an Airbus A three twenty one.

- **Route, airport and airline names** from the VRS standing data (free; one file per airline,
  downloaded when first needed and refreshed weekly). **Aircraft model** from adsbdb (free),
  cached for a month. Anything unknown is left out of the sentence.
- **Only unique aircraft (v2)**, scored against the history of heard aircraft
  (`uniqueness.py`). Each feature (airline, aircraft model, the end of the route that isn't
  Heathrow, and the ADS-B category when the model is unknown) gets a rarity in bits: how much
  more surprising its value is than that feature's values usually are, mixed with the share
  of heard flights whose value is as rare. Counts are recency-weighted (60-day half-life), so
  a model that stops coming becomes rare again. A flight's score is its rarest feature, plus a
  little for a rare airline + model pair; it's ranked among the last 14 days' heard flights,
  and only the **top 3%** are announced (about 5-10 a busy day so far; more as the
  history grows past its first weeks). The reason picks a lead-in: "First time heard:",
  "The first Boeing seven four seven in two weeks:" or "A rare visitor:". Nothing is
  unique until 500 aircraft have been heard. The airframe and night were tried and dropped:
  a new airframe is the norm, and night isn't rare (nor announced). Settings
  (`UNIQUE_TOP_PERCENT`, 100 = announce everything, as v1 did) are in `config.py`.
- **Each aircraft once per pass**, at least 30 s between announcements, none 20:00-09:00.
- **Every heard aircraft is logged** in `~/aircraft-noise-data/announcer.db` (table `heard`:
  who, when, how far, which mics, its ADS-B category, its uniqueness score, percentile and
  reason, what was said, or why not: `not unique`, `quiet hours`, `paused`...), quiet hours
  included: the history the score is taken from. Which were rare:
  `ssh sebpi@sebpi "sqlite3 aircraft-noise-data/announcer.db \"SELECT datetime(ts,'unixepoch'), percentile, reason, text FROM heard WHERE percentile >= 97\""`
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

## Website

https://sappleyard.github.io/flight-passes/ is a public table of every pass (one row per
aircraft within 5 km, with a summary per day), plus an Outlook page with a 10-day forecast.
It's a static site: `website/site/` holds the pages, and `website/export.py` and
`website/forecast/` write the data they show.

**What's published** is limited on purpose. Flight tracks are public, so anything that ties a
flight to a precise time or distance could help someone find the house. `export.py` writes only
the date, the **hour**, callsign, airline, route, aircraft and type. It never writes distance,
altitude, speed, heading, position or exact times. It publishes complete days only, and within
an hour sorts rows by callsign, not by time. Routes and aircraft come from the announcer's
cached lookups (`flights.py`).

**Publishing** runs on the Pi: `website/deploy.sh` copies the code; `publish.sh` rebuilds
`passes.json` into `~/aircraft-website/pages` (a clone of the site repo) and pushes it. It
needs a write key for that repo only, and a daily cron job:

    ssh sebpi@sebpi 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/aircraft_pages -C "sebpi flight-passes"'
    ssh sebpi@sebpi cat .ssh/aircraft_pages.pub | gh repo deploy-key add - --repo sappleyard/flight-passes --allow-write --title sebpi
    ssh sebpi@sebpi 'GIT_SSH_COMMAND="ssh -i ~/.ssh/aircraft_pages" git clone git@github.com:sappleyard/flight-passes.git aircraft-website/pages && git -C aircraft-website/pages config user.name sappleyard && git -C aircraft-website/pages config user.email sappleyard@users.noreply.github.com'
    ssh sebpi@sebpi '(crontab -l 2>/dev/null; echo "30 1 * * * \$HOME/aircraft-website/publish.sh >> \$HOME/aircraft-website/publish.log 2>&1") | crontab -'

**Outlook data:** the page fills itself from `data/outlook.json`, shaped like this:
`{"generated": "2026-11-06", "days": [{"date": "2026-11-06", "passes": 420, "low": 300,
"high": 560, "wind": "SW 18 km/h", "weather": "Showers"}, ...]}`. Any missing day or field
shows a dash. `site/data/outlook.json` is an empty placeholder; `publish.sh` runs
`forecast/forecast.py` after `export.py` to write the real one into `pages/data/` (rsync leaves
it alone), and keeps the previous day's if Open-Meteo can't be reached.

**The forecast** (`forecast/`) is mostly a forecast of Heathrow's runway direction, because
days are bimodal: westerly operations bring the arrival stream overhead, heading west (about
650 passes a day); easterly operations bring about 40. Each past daytime hour (06-22 local,
logger running) is labelled west (10+ westbound passes), east (2 or fewer) or unknown. A
two-state Markov chain switches hourly with a chance that is a logistic function of the
tailwind a westerly landing would have (wind along runway 27, averaged over the next 6 hours,
which fits far better than the hour's own wind: Heathrow plans changes on the forecast). It's
fitted by EM so night hours are filled in, with Gaussian priors from Heathrow's rule (go
easterly above a few knots of tailwind, prefer westerly), since there are only a handful of
switches. Counts are the mean passes per local hour in each direction, plus a day-to-day
spread. 4000 runs then go through the next 10 days, each on one of the 51 ECMWF ensemble
members and its own draw of the fitted parameters; `passes` is the median, `low`/`high` the
10th/90th percentiles. Today's passes so far are added to today's runs. Wind and weather are
Heathrow's daytime vector-mean wind and most significant WMO code, from Open-Meteo's
deterministic forecast. All weather requests use Heathrow's coordinates, never the house's.

`forecast/backtest.py` (Mac or Pi, `--db` for a database copy) remakes each past day's 01:00
forecast from hours before then and wind as forecast at the time (Open-Meteo's previous runs,
so leads 0-6 only, on 3 models instead of the ensemble) and compares it with persistence and
climatology. On 22 Oct-11 Nov: mean absolute error over leads 0-6 was 103 passes a day,
against 280 for persistence and 251 for climatology (47 same-day, 65 next-day); about 2 in 3
actual days fell inside low-high, not 8 in 10, so the ranges are somewhat too narrow.

## Next steps

See the design summary (`docs/`): v2 (announce only unique aircraft, scored from the `heard`
history) is done (see Announcer); next, the publishing API and dashboard.
