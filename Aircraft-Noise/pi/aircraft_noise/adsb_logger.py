"""ADS-B logger: saves every aircraft position readsb receives to the database.

Polls readsb's aircraft.json once a second and stores each new position once, timestamped
with when it was received (poll time minus readsb's seen_pos). Runs as the adsb-logger service.
"""

import json
import logging
import math
import time

from .config import AIRCRAFT_JSON, MAX_RANGE_KM, PAUSE_FILE, RECEIVER_JSON, STALE_AFTER_S
from .db import connect, set_meta

log = logging.getLogger("adsb-logger")

INSERT = """INSERT INTO aircraft_positions
    (ts, hex, callsign, lat, lon, alt_baro_ft, alt_geom_ft, speed_kt, track_deg, dist_km)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def read_json(path):
    with open(path) as f:
        return json.load(f)


def ground_km(a, b):
    """Great-circle distance between two (lat, lon) points in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def new_positions(data, receiver, last_saved):
    """Rows for positions not saved yet. last_saved maps hex -> time of its last saved row."""
    rows = []
    for a in data.get("aircraft", []):
        seen_pos = a.get("seen_pos")
        if seen_pos is None or seen_pos > 3 or a.get("lat") is None:
            continue   # no position, or an old one already saved on an earlier poll
        hex_ = a["hex"].upper()
        ts = round(data["now"] - seen_pos, 1)
        if abs(ts - last_saved.get(hex_, 0)) < 0.25:
            continue   # same position as last poll
        dist = ground_km(receiver, (a["lat"], a["lon"]))
        if dist > MAX_RANGE_KM:
            continue
        last_saved[hex_] = ts
        alt_baro = 0 if a.get("alt_baro") == "ground" else a.get("alt_baro")
        rows.append((ts, hex_, a.get("flight", "").strip() or None, a["lat"], a["lon"],
                     alt_baro, a.get("alt_geom"), a.get("gs"), a.get("track"), round(dist, 3)))
    return rows


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db = connect()

    rx = read_json(RECEIVER_JSON)
    if "lat" not in rx:
        raise SystemExit("readsb has no receiver location: sudo readsb-set-location <lat> <lon>")
    receiver = (rx["lat"], rx["lon"])
    set_meta(db, "receiver_lat", receiver[0])
    set_meta(db, "receiver_lon", receiver[1])
    db.commit()

    last_saved = {}
    coverage = None   # logger_coverage row being extended while data is fresh
    saved, next_summary = 0, time.monotonic() + 600
    log.info("logging aircraft within %d km of %.4f, %.4f", MAX_RANGE_KM, *receiver)

    while True:
        started = time.monotonic()
        if PAUSE_FILE.exists():
            coverage = None
        else:
            try:
                data = read_json(AIRCRAFT_JSON)
                fresh = time.time() - data["now"] < STALE_AFTER_S
            except (OSError, ValueError, KeyError) as e:
                log.warning("can't read %s: %s", AIRCRAFT_JSON, e)
                fresh = False
            if fresh:
                rows = new_positions(data, receiver, last_saved)
                db.executemany(INSERT, rows)
                saved += len(rows)
                if coverage is None:
                    coverage = db.execute("INSERT INTO logger_coverage VALUES (?, ?)",
                                          (data["now"], data["now"])).lastrowid
                else:
                    db.execute("UPDATE logger_coverage SET end_ts = ? WHERE rowid = ?",
                               (data["now"], coverage))
                db.commit()
            else:
                coverage = None   # a gap: the next fresh poll starts a new coverage period

        if started >= next_summary:
            log.info("%s: %d positions saved in the last 10 min",
                     "paused" if PAUSE_FILE.exists() else "running", saved)
            saved, next_summary = 0, started + 600
            cutoff = time.time() - 600   # forget aircraft not heard for 10 minutes
            last_saved = {h: t for h, t in last_saved.items() if t > cutoff}

        time.sleep(max(0.0, 1.0 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
