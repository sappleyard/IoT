# Aircraft Noise

Measure how loud aircraft are at home, with two microphones and the Pi's ADS-B receiver
saying which plane was where.

**Privacy: no audio is recorded or stored.** Each mic node boils every second of sound down
to nine numbers (overall loudness and seven pitch bands) and throws the sound away. One-second
band levels can't reproduce speech.

```
 Mic A: living room ──┐  ESP-NOW long-range radio
 (N window, floor 2)  │
                      ▼
 Mic B: bedroom ───► Bridge ESP32 ──USB──► Raspberry Pi ◄── RTL-SDR (ADS-B)
 (S window, floor 3)  (relay only)          ├─ bridge-link: mic levels    ─┐
                                            └─ adsb-logger: aircraft       ─┴► noise.db (SQLite)
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

```
analysis/summary.py         report figures + summary from a copy of the Pi's database
esp32/
  flash.sh                  build + flash:  ./flash.sh bridge|node [pi|<port>]
  common/                   shared by both firmwares: packet format and board list (protocol.h),
                            radio; secrets.h holds the encryption keys (made by flash.sh, git-ignored)
  bridge/main/main.c        relays node packets to the Pi as JSON lines
  node/main/main.c, mic.c   measures the mic, sends a packet a second
pi/
  deploy.sh                 copy the Pi code to the Pi and restart the services
  location.json             home location {"lat": .., "lon": ..}: git-ignored, never published
  setup/                    install.sh and the two systemd services
  aircraft_noise/
    config.py               every setting, including the mics (MAC, name, location, calibration)
    db.py                   the database schema
    adsb_logger.py          service: aircraft positions -> database
    bridge_link.py          service: mic levels -> database, health log every 10 min
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

0. Create `pi/location.json` with the home location: `{"lat": 51.5, "lon": -0.1}`.
1. Flash the boards (each plugged into the Pi, or the Mac with `./flash.sh node`):
   `esp32/flash.sh bridge pi`, then `esp32/flash.sh node pi` for each node.
2. Copy the code to the Pi: `pi/deploy.sh`
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
4. Put the offsets in `MICS` in `pi/aircraft_noise/config.py` and deploy. No reflash needed.

Current offsets (A −6, B −7) are rough estimates from an uncased test.

## Analysis

`python3 analysis/summary.py` copies the Pi's database (consistently) and writes figures and a
summary to `analysis/figures/`: levels over time, the daily profile, octave-band spectra of
aircraft vs quiet, and the sound propagation model (LAmax = L1 - 20 log10(r) - alpha r, fitted
to the passes the mics heard, background-corrected), with detection rate by distance.

## Next steps

See the design summary (`docs/`): the mic event detector (v1 rule), flight info from the free
offline VRS route database, the Sonos announcer, the model on octave bands (v2: special
aircraft only), then the publishing API and dashboard.
