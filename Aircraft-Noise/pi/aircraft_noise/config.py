"""Every setting for the Pi programs, in one place."""

import os
from pathlib import Path

# Data lives outside the code, so deploys never touch it.
DATA_DIR = Path(os.environ.get("AIRCRAFT_NOISE_DATA", Path.home() / "aircraft-noise-data"))
DB_PATH = DATA_DIR / "noise.db"

# While this file exists both services sit idle and the bridge's serial port is released,
# e.g. to flash a board:  touch ~/aircraft-noise-data/PAUSED  (rm it to resume).
PAUSE_FILE = DATA_DIR / "PAUSED"

# --- ADS-B logger -------------------------------------------------------------------------
AIRCRAFT_JSON = "/run/readsb/aircraft.json"   # rewritten by readsb every second
RECEIVER_JSON = "/run/readsb/receiver.json"   # holds the receiver location
MAX_RANGE_KM = 60          # don't log aircraft further away than this
STALE_AFTER_S = 5          # aircraft.json older than this means readsb has stopped

# --- Bridge link --------------------------------------------------------------------------
SERIAL_BAUD = 115200
SUMMARY_EVERY_S = 600      # log a health line per mic this often
BRIDGE_SILENT_S = 180      # warn if the bridge's once-a-minute heartbeat stops this long

BANDS_HZ = [63, 125, 250, 500, 1000, 2000, 4000]   # octave-band centres, as the nodes send them

# The mics, keyed by the MAC address of the node board they're on. cal_db is added to the
# node's levels to correct that mic; it belongs to the mic, so move it if a mic moves.
# Both are first estimates (2026-10-21, against a phone sound-meter app, which is only good
# to about +-5 dB): recalibrate once the nodes are in their cases (see README).
MICS = {
    "28:05:A5:35:E0:EC": {"name": "A", "cal_db": -7.0,
                          "location": "Living room, north-facing window, floor 2"},
    "28:05:A5:35:B6:50": {"name": "B", "cal_db": -6.0,
                          "location": "Bedroom, south-facing window, floor 3"},
}
