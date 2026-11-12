"""Every setting for the announcer, in one place."""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- Which model decides "an aircraft was heard" --------------------------------------------
# A file pair in announcer/models/: NAME.onnx and NAME.json, written by
#   model/train.py --export NAME
# To use a newly trained model: export it, change this line, run announcer/deploy.sh.
MODEL = "cnn-heli4-2026-11-05"
MODELS_DIR = HERE.parent / "models"

# --- Announcements ---------------------------------------------------------------------------
QUIET_FROM_HOUR = 20       # no announcements from 20:00...
QUIET_UNTIL_HOUR = 9       # ...until 09:00 (heard aircraft are still logged)
MIN_GAP_S = 30             # v2: at least this long between announcement starts
VOLUME = 25                # Sonos volume for announcements, 0-100
SPEAKER = None             # Sonos IP, or None to find one on the network (SSDP)
HTTP_PORT = 8765           # the Sonos fetches each spoken clip from this port on the Pi
VOICE = "en_GB-alan-medium"   # Piper voice, downloaded by setup/install.sh

# --- What is said: v1 or v2 --------------------------------------------------------------------
# v1  every heard aircraft, the moment it's ready: no gap, nothing put back after, so each
#     announcement cuts off whatever was playing, the previous announcement included:
#       "British Airways 2 7 1, from Frankfurt to London Heathrow, at 3,100 feet, an Airbus A three twenty."
# v2  only unusual ones (the top UNIQUE_TOP_PERCENT by uniqueness.py's score), at least
#     MIN_GAP_S apart, putting back what was playing afterwards, with a lead-in:
#       "First time heard: ...", "The first Boeing seven four seven in two weeks: ...",
#       "A rare visitor: ...", and every model in ALWAYS_UNIQUE: "G.O.A.T. ..."
# Switch on the Pi, no restart needed (checked before every announcement):
#   echo 1 > ~/aircraft-noise-data/ANNOUNCER_VERSION      (or 2; with no file, VERSION is used)
# Every heard aircraft is scored and logged either way, so the v2 history stays complete.
VERSION = 2
UNIQUE_TOP_PERCENT = 3.0
UNIQUE_MIN_HISTORY = 500   # nothing is unique until this many aircraft have been heard
# Always unique, with their own lead-in (matched within the model name, ignoring case)
ALWAYS_UNIQUE = {"a380": "G.O.A.T.", "747": "G.O.A.T."}
HOME_AIRPORT = "London Heathrow"   # the end of a route that doesn't make it unusual

# --- Matching a heard sound to an aircraft ----------------------------------------------------
SPEED_OF_SOUND = 343.0     # m/s: sound from an aircraft arrives slant / 343 s after it's sent
# The model fires on windows ending from about 5 s before to 15 s after an aircraft's sound
# arrives (as it was trained), so look for an aircraft whose sound arrived in this span.
ARRIVAL_BEFORE_S = 20
ARRIVAL_AFTER_S = 5
# Aircraft further away than this aren't the one heard. By day nothing beyond ~3 km stands out
# from household noise; at night aircraft are heard to ~13 km (model/audibility.py).
MATCH_MAX_KM_DAY = 3.0
MATCH_MAX_KM_NIGHT = 8.0
NIGHT_HOURS = (23, 7)      # 23:00-07:00, as in the audibility analysis and training
REANNOUNCE_AFTER_S = 1200  # one announcement per pass: the same aircraft again only after 20 min
TRACK_HISTORY_S = 90       # how long each aircraft's positions are kept for matching

# --- Inputs (shared with the collection services on the Pi) ---------------------------------
DATA_DIR = Path(os.environ.get("AIRCRAFT_NOISE_DATA", Path.home() / "aircraft-noise-data"))
NOISE_DB = DATA_DIR / "noise.db"            # read only: written by the collection services
ANNOUNCER_DB = DATA_DIR / "announcer.db"    # heard aircraft and the lookup cache
# While this file exists nothing is spoken (heard aircraft are still logged):
#   touch ~/aircraft-noise-data/ANNOUNCER_PAUSED   (rm it to resume; no restart needed)
PAUSE_FILE = DATA_DIR / "ANNOUNCER_PAUSED"
VERSION_FILE = DATA_DIR / "ANNOUNCER_VERSION"   # "1" or "2": overrides VERSION (see above)
CACHE_DIR = DATA_DIR / "announcer-cache"    # route and airport files, voice, spoken clips
AIRCRAFT_JSON = "/run/readsb/aircraft.json"
# The home location, from the collection code's location.json (deployed to ~/aircraft-noise).
LOCATION_FILE = Path(os.environ.get("AIRCRAFT_NOISE_LOCATION",
                                    Path.home() / "aircraft-noise" / "location.json"))
STALE_LEVELS_S = 10        # mic levels older than this: the mics or bridge are down

# --- Flight information ----------------------------------------------------------------------
# Routes, airports and airline names: VRS standing data (free, offline once downloaded; one
# file per airline / airport prefix, fetched when first needed and refreshed weekly).
VRS_URL = "https://raw.githubusercontent.com/vradarserver/standing-data/main"
VRS_REFRESH_S = 7 * 86400
# Aircraft model by ICAO address: adsbdb (free), cached.
ADSBDB_URL = "https://api.adsbdb.com/v0"
ADSBDB_REFRESH_S = 30 * 86400
HTTP_TIMEOUT_S = 5

SUMMARY_EVERY_S = 600      # log a health line this often
