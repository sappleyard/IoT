# Aircraft Noise

Measure aircraft noise around the house with three ESP32 microphones, and use the Pi's
ADS-B receiver to work out which plane made which noise.

```
             ESP-NOW (no router)
 Node A ─────────────┐
 (room 1, mic)       │
                     ▼
 Node B ────────► Bridge ESP32 ──USB serial──► Raspberry Pi ◄── RTL-SDR (ADS-B)
 (room 2, mic)    (relay; no mic now)          │
                                               ├─ adsb-logger:  aircraft positions → SQLite
                                               └─ bridge-link:  node reports → SQLite (raw audio: laptop recorder)
```

## Folders

| Folder | What's in it | Runs on |
|---|---|---|
| [pi/](pi/) | Python: ADS-B logger, bridge link, audio recorder, clip labeller; systemd services and setup scripts | Raspberry Pi (recorder also on a laptop) |
| [esp32/bridge/](esp32/bridge/) | Firmware for the bridge: the ESP32 plugged into the Pi | Bridge ESP32 |
| [esp32/node/](esp32/node/) | Firmware for the two remote nodes, A and B (same code, different node ID) | Node A and node B |
| [esp32/components/](esp32/components/) | Code both firmwares share: mic, ESP-NOW radio, packet format, USB serial link | (built into both) |

See [pi/README.md](pi/README.md) and [esp32/README.md](esp32/README.md) for setup.

## Plan and status

| Step | What | Status |
|---|---|---|
| 1 | Log ADS-B to a database on the Pi (+ FlightAware feeding) | Code done: [pi/aircraft_noise/adsb/](pi/aircraft_noise/adsb/) |
| 2 | One INMP441 mic working on an ESP32, levels in dB | Code done: [esp32/components/mic/](esp32/components/mic/) |
| 3 | ESP-NOW between nodes, bridge to the Pi over USB | Code done: [esp32/](esp32/), [pi/aircraft_noise/nodes/](pi/aircraft_noise/nodes/) |
| 4 | Collect labelled audio, with ADS-B as the labeller | Code done: [pi/aircraft_noise/audio/](pi/aircraft_noise/audio/), [pi/aircraft_noise/labelling/](pi/aircraft_noise/labelling/) |
| 5 | Train an aircraft/no-aircraft model and run it on the ESP32s | Not started (`p_aircraft` is in the packet, sent as null for now) |
| 6 | Look up flights with FlightAware AeroAPI | Not started |
| 7 | Match noise events to flights | Not started |

Start step 1 first: the step 4 labeller learns from the logged flight history, so the more
days that are logged the better.

## Privacy

Raw audio catches conversations. Keep it on the Pi, and delete `~/aircraft-noise-data/audio`
(and the clips in `dataset/`) once the model is trained. The 1 s levels and model scores are fine to keep.
