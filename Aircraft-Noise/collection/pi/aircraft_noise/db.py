"""The SQLite database both services write to (WAL mode, so reading it never blocks them)."""

import sqlite3

from .config import BANDS_HZ, DB_PATH

BAND_COLUMNS = [f"band_{hz}" for hz in BANDS_HZ]

SCHEMA = f"""
-- One row per aircraft position received (from readsb).
CREATE TABLE IF NOT EXISTS aircraft_positions (
    ts          REAL NOT NULL,   -- unix time the position was received (Pi clock)
    hex         TEXT NOT NULL,   -- ICAO address
    callsign    TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    alt_baro_ft INTEGER,         -- pressure altitude (not corrected for the day's air pressure)
    alt_geom_ft INTEGER,         -- GPS altitude above the WGS84 ellipsoid (not sea level), as sent
    speed_kt    REAL,
    track_deg   REAL,
    dist_km     REAL,            -- ground distance from the receiver
    category    TEXT,            -- ADS-B emitter category, e.g. A3 airliner, A7 helicopter
    on_ground   INTEGER          -- 1 = the aircraft reports being on the ground
);
CREATE INDEX IF NOT EXISTS aircraft_positions_ts ON aircraft_positions (ts);

-- One row per aircraft that came within PASS_RADIUS_KM (slant range) of home: the ground
-- truth that mic detections are checked against, and the training labels.
CREATE TABLE IF NOT EXISTS passes (
    hex              TEXT NOT NULL,
    callsign         TEXT,
    first_ts         REAL NOT NULL,   -- first and last position inside the radius
    last_ts          REAL NOT NULL,
    closest_ts       REAL NOT NULL,   -- when it was closest (sound arrives slant/343 s later)
    closest_slant_km REAL NOT NULL,   -- straight-line distance from the receiver at that moment
    closest_dist_km  REAL,            -- ground distance
    closest_alt_ft   INTEGER,         -- above sea level (GPS altitude corrected by geoid_m)
    alt_is_gps       INTEGER,         -- 1 = GPS altitude; 0 = pressure altitude (uncorrected)
    speed_kt         REAL,
    track_deg        REAL,
    category         TEXT             -- ADS-B emitter category (A7 = helicopter)
);
CREATE INDEX IF NOT EXISTS passes_closest_ts ON passes (closest_ts);

-- Periods when the ADS-B logger was running with fresh data. Outside them "no aircraft
-- seen" means nothing, so labelling must only trust these periods.
CREATE TABLE IF NOT EXISTS logger_coverage (
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL
);

-- One row per mic per second: numbers only, never audio.
CREATE TABLE IF NOT EXISTS levels (
    ts        REAL NOT NULL,     -- unix time the Pi received it
    mic       TEXT NOT NULL,     -- name from MICS in config.py (or the MAC if unknown)
    seq       INTEGER,           -- +1 a second per node: gaps = lost packets
    uptime_ms INTEGER,           -- going backwards = the node restarted
    flags     INTEGER,           -- 1 = mic fault (no signal from the mic)
    laeq      REAL,              -- A-weighted level over the second, dB(A), uncalibrated
    lamax     REAL,              -- loudest 125 ms, dB(A), uncalibrated
    {", ".join(f"{c} REAL" for c in BAND_COLUMNS)},   -- octave bands, dB, uncalibrated
    rssi      INTEGER,           -- radio signal strength at the bridge, dBm
    cal_db    REAL               -- the mic's calibration when stored: add it to every level
);
CREATE INDEX IF NOT EXISTS levels_mic_ts ON levels (mic, ts);

-- Small facts, e.g. the receiver location.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def set_meta(db, key, value):
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))
