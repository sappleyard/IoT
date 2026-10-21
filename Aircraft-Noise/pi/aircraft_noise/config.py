"""Settings shared by every Pi-side program. Change things here rather than in the scripts."""

import os
from pathlib import Path

# --- Where data lives (outside the repo, so deploys never touch it) ---------------------------
DATA_DIR = Path(os.environ.get("AIRCRAFT_NOISE_DATA", Path.home() / "aircraft-noise-data"))
DB_PATH = DATA_DIR / "noise.db"          # ADS-B positions + node reports (SQLite)
AUDIO_DIR = DATA_DIR / "audio"           # hourly WAV files from the mics
DATASET_DIR = DATA_DIR / "dataset"       # labelled 1 s clips for training
# While this file exists, both services idle and the link lets go of the serial port
# (e.g. to flash a board). No sudo needed:  touch ~/aircraft-noise-data/PAUSED  /  rm ...
PAUSE_FILE = DATA_DIR / "PAUSED"

# --- Step 1: ADS-B logger ----------------------------------------------------------------------
AIRCRAFT_JSON = "/run/readsb/aircraft.json"   # written by readsb every second (a URL works too)
RECEIVER_JSON = "/run/readsb/receiver.json"   # has lat/lon once readsb-set-location has been run
POLL_INTERVAL_S = 1.0
MAX_RANGE_KM = 60          # don't log aircraft further away than this (needs the receiver location)
STALE_AFTER_S = 5          # aircraft.json older than this means readsb has stopped
RECEIVER_ALT_M = 0.0       # receiver height above sea level, used for slant range

# --- Step 3: link to the bridge ESP32 ----------------------------------------------------------
SERIAL_PORT = None         # None = find the ESP32 automatically
SERIAL_BAUD = 230400       # must match the firmware; ignored by native USB (ESP32-S3)
COMMAND_PORT = 47000       # UDP port on 127.0.0.1 where the link service takes commands
RECORD_BRIDGE_AUDIO = False  # step 4: save the bridge mic's raw audio as hourly WAVs (bridge has no mic now)

# Per-node mic calibration (step 2): a phone sound meter's dB(A) reading minus the node's
# level_dba, measured side by side. Stored alongside each report, so calibrated level =
# level_dba + cal_offset_db (and, roughly, the same for level_db).
# The offset belongs to the mic, so it moves if a mic moves to another node.
#   node 2: 2026-10-21, ~60 dB(A) on the phone vs ~66 from this mic (measured while it was on
#           node 1, then moved to node 2) -> -6. Phone apps are only good to about +-5 dB.
#   node 1: its first mic was faulty (all zeros, and it crashed the Pi's USB controller);
#           calibrate the replacement before trusting its levels.
MIC_CAL_OFFSET_DB = {0: 0.0, 1: 0.0, 2: -6.0}

# --- Step 4: audio + labelling -----------------------------------------------------------------
SAMPLE_RATE = 16000
SPEED_OF_SOUND_MS = 343.0
AIRCRAFT_MAX_KM = 3.0      # "aircraft" clip: nearest plane's slant range under this
CLEAR_MIN_KM = 15.0        # "no aircraft" clip: nothing closer than this ...
CLEAR_WINDOW_S = 60        # ... for this many seconds either side
