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
MIN_GAP_S = 30             # at least this long between announcement starts, so none overlap
VOLUME = 25                # Sonos volume for announcements, 0-100
SPEAKER = None             # Sonos IP, or None to find one on the network (SSDP)
HTTP_PORT = 8765           # the Sonos fetches each spoken clip from this port on the Pi
VOICE = "en_GB-alan-medium"   # Piper voice, downloaded by setup/install.sh

# --- Only unique aircraft (v2: see uniqueness.py) ----------------------------------------------
# Each heard flight is scored for how rare its airline, model, route and ADS-B category are in
# the heard history, then ranked among the recent heard flights: only the top few per cent are
# announced (3% is about 10-20 on a busy day). 100 announces every heard aircraft, as v1 did.
UNIQUE_TOP_PERCENT = 3.0
UNIQUE_WINDOW_DAYS = 14    # "recent heard flights", the ones the percentile is taken among
UNIQUE_HALF_LIFE_DAYS = 60    # a flight heard this long ago counts half as much as one today
UNIQUE_MIN_HISTORY = 500   # nothing is called unique until this many flights have been heard
UNIQUE_PAIR_WEIGHT = 0.5   # share of a rare airline + model pair's extra rarity that counts
# Each feature's rarity is part "share of flights with a value this rare" and part surprisal
# beyond what's usual for that feature (which a value never heard before tops): this much of
# the latter. Both are fair between features with few values (models) and many (places).
UNIQUE_SURPRISAL_SHARE = 0.7
UNIQUE_RESCORE_S = 600     # how often the recent flights are re-ranked against the history
# Models always announced, however common, with their own lead-in (matched within the model
# name, ignoring case)
ALWAYS_UNIQUE = {"a380": "G.O.A.T."}
HOME_AIRPORT = "London Heathrow"   # the end of a route that doesn't make it unusual

# --- Matching a heard sound to an aircraft ----------------------------------------------------
SPEED_OF_SOUND = 343.0     # m/s: sound from an aircraft arrives slant / 343 s after it's sent
# The model fires on windows ending from about 5 s before to 15 s after an aircraft's sound
# arrives (as it was trained), so look for an aircraft whose sound arrived in this span.
ARRIVAL_BEFORE_S = 20
ARRIVAL_AFTER_S = 5
# Aircraft further away than this aren't the one heard. By day nothing beyond ~3 km stands out
# from household noise; at night aircraft are heard to ~13 km (analysis/audibility.py).
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
# Aircraft model by ICAO address, and routes VRS doesn't have: adsbdb (free), cached.
ADSBDB_URL = "https://api.adsbdb.com/v0"
ADSBDB_REFRESH_S = 30 * 86400
HTTP_TIMEOUT_S = 5

SUMMARY_EVERY_S = 600      # log a health line this often
