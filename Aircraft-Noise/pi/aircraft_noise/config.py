"""Every setting for the Pi programs, in one place."""

import json
import os
from pathlib import Path

# Data lives outside the code, so deploys never touch it.
DATA_DIR = Path(os.environ.get("AIRCRAFT_NOISE_DATA", Path.home() / "aircraft-noise-data"))
DB_PATH = DATA_DIR / "noise.db"

# While this file exists both services sit idle and the bridge's serial port is released,
# e.g. to flash a board:  touch ~/aircraft-noise-data/PAUSED  (rm it to resume).
PAUSE_FILE = DATA_DIR / "PAUSED"

# The home location (receiver and mics), from pi/location.json: {"lat": .., "lon": ..}.
# Kept out of git, so the code can be published without revealing where it runs.
LOCATION_FILE = Path(__file__).resolve().parent.parent / "location.json"


def home_location():
    """(lat, lon) from LOCATION_FILE."""
    try:
        loc = json.loads(LOCATION_FILE.read_text())
        return loc["lat"], loc["lon"]
    except (OSError, ValueError, KeyError) as e:
        raise SystemExit(f'Need {LOCATION_FILE} like {{"lat": 51.5, "lon": -0.1}} ({e})')


# --- ADS-B logger -------------------------------------------------------------------------
AIRCRAFT_JSON = "/run/readsb/aircraft.json"   # rewritten by readsb every second
MAX_RANGE_KM = 60          # don't log aircraft further away than this
STALE_AFTER_S = 5          # aircraft.json older than this means readsb has stopped
PASS_RADIUS_KM = 5         # an aircraft coming within this slant range makes a "pass" row
PASS_END_S = 60            # ...written once it's been outside that range this long

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
