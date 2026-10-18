"""Settings shared by every Pi-side program. Change things here rather than in the scripts."""

import os
from pathlib import Path

# --- Where data lives (outside the repo, so deploys never touch it) ---------------------------
DATA_DIR = Path(os.environ.get("AIRCRAFT_NOISE_DATA", Path.home() / "aircraft-noise-data"))
DB_PATH = DATA_DIR / "noise.db"          # ADS-B positions + node reports (SQLite)
AUDIO_DIR = DATA_DIR / "audio"           # hourly WAV files from the mics
DATASET_DIR = DATA_DIR / "dataset"       # labelled 1 s clips for training

# --- Step 1: ADS-B logger ----------------------------------------------------------------------
AIRCRAFT_JSON = "/run/readsb/aircraft.json"   # written by readsb every second (a URL works too)
RECEIVER_JSON = "/run/readsb/receiver.json"   # has lat/lon once readsb-set-location has been run
POLL_INTERVAL_S = 1.0
MAX_RANGE_KM = 60          # don't log aircraft further away than this (needs the receiver location)
STALE_AFTER_S = 5          # aircraft.json older than this means readsb has stopped
RECEIVER_ALT_M = 0.0       # receiver height above sea level, used for slant range

# --- Step 3: link to the bridge ESP32 ----------------------------------------------------------
SERIAL_PORT = None         # None = find the ESP32 automatically
SERIAL_BAUD = 921600       # only matters for boards on a USB-UART chip; native USB ignores it
COMMAND_PORT = 47000       # UDP port on 127.0.0.1 where the link service takes commands
RECORD_BRIDGE_AUDIO = False  # step 4: True = save the bridge mic's raw audio as hourly WAVs

# Per-node mic calibration (step 2): phone sound-meter reading minus the node's level_db.
# Stored alongside each report, so calibrated level = level_db + cal_offset_db.
MIC_CAL_OFFSET_DB = {0: 0.0, 1: 0.0, 2: 0.0}

# --- Step 4: audio + labelling -----------------------------------------------------------------
SAMPLE_RATE = 16000
SPEED_OF_SOUND_MS = 343.0
AIRCRAFT_MAX_KM = 3.0      # "aircraft" clip: nearest plane's slant range under this
CLEAR_MIN_KM = 15.0        # "no aircraft" clip: nothing closer than this ...
CLEAR_WINDOW_S = 60        # ... for this many seconds either side
