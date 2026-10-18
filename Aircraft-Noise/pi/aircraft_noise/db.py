"""The SQLite database every Pi program shares.

One file, in WAL mode so the loggers keep writing while other scripts read.
"""

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
-- Step 1: one row per aircraft position received.
CREATE TABLE IF NOT EXISTS aircraft_positions (
    ts          REAL NOT NULL,   -- unix time the position was received (Pi clock)
    hex         TEXT NOT NULL,   -- ICAO address
    callsign    TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    alt_baro_ft INTEGER,         -- pressure altitude, 0 on the ground
    alt_geom_ft INTEGER,         -- GPS altitude, when the aircraft sends it
    speed_kt    REAL,
    track_deg   REAL,
    dist_km     REAL             -- ground distance from the receiver, NULL if location unknown
);
CREATE INDEX IF NOT EXISTS aircraft_positions_ts ON aircraft_positions (ts);

-- Periods when the ADS-B logger was running with fresh data. "No aircraft" labels
-- are only trusted inside these, so a logger outage can't pass for a quiet sky.
CREATE TABLE IF NOT EXISTS logger_coverage (
    start_ts REAL NOT NULL,
    end_ts   REAL NOT NULL
);

-- Step 3: one row per NodeReport from the ESP32s.
CREATE TABLE IF NOT EXISTS node_reports (
    ts            REAL NOT NULL,     -- unix time the Pi received it
    node          INTEGER NOT NULL,  -- 0 = bridge, 1 = A, 2 = B
    seq           INTEGER,
    uptime_ms     INTEGER,
    level_db      REAL,              -- 1 s level, uncalibrated dB SPL estimate
    peak_db       REAL,
    p_aircraft    REAL,              -- on-device model output (step 5)
    battery_mv    INTEGER,
    rssi          INTEGER,           -- ESP-NOW signal strength at the bridge
    cal_offset_db REAL               -- MIC_CAL_OFFSET_DB for this node when stored
);
CREATE INDEX IF NOT EXISTS node_reports_node_ts ON node_reports (node, ts);

-- Small key/value facts, e.g. the receiver location.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path=DB_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def set_meta(db, key, value):
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


def get_meta(db, key):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def receiver_location(db):
    """(lat, lon) saved by the ADS-B logger, or None."""
    lat, lon = get_meta(db, "receiver_lat"), get_meta(db, "receiver_lon")
    return (float(lat), float(lon)) if lat and lon else None
