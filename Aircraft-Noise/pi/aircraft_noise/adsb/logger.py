"""ADS-B logger (step 1): save every aircraft position readsb receives to SQLite.

Polls readsb's aircraft.json once a second. Each new position is stored once, timestamped with
when it was actually received (the poll time minus readsb's seen_pos). Runs on the Pi as the
adsb-logger systemd service; to try it by hand, from the pi/ folder:

    python3 -m aircraft_noise.adsb.logger
"""

import argparse
import json
import logging
import sys
import time
import urllib.request
from pathlib import Path

from ..config import (AIRCRAFT_JSON, MAX_RANGE_KM, POLL_INTERVAL_S, RECEIVER_JSON,
                      STALE_AFTER_S)
from ..db import connect, set_meta
from ..geo import ground_km

# Reuse the aircraft.json parser from the ADS-B-Test project next door in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "ADS-B-Test"))
from adsb_monitor import Aircraft  # noqa: E402

log = logging.getLogger("adsb-logger")

INSERT = """INSERT INTO aircraft_positions
    (ts, hex, callsign, lat, lon, alt_baro_ft, alt_geom_ft, speed_kt, track_deg, dist_km)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

# A position older than this was already saved on an earlier poll (or is too old to be useful).
MAX_POSITION_AGE_S = 3 * POLL_INTERVAL_S


def read_json(source):
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=5) as resp:
            return json.load(resp)
    with open(source) as f:
        return json.load(f)


def read_receiver_location():
    try:
        rx = read_json(RECEIVER_JSON)
    except (OSError, ValueError):
        return None
    return (rx["lat"], rx["lon"]) if "lat" in rx and "lon" in rx else None


def new_positions(data, receiver, last_saved):
    """Rows for positions not saved yet. last_saved maps hex -> timestamp of its last saved row."""
    now = data["now"]
    rows = []
    for raw in data.get("aircraft", []):
        seen_pos = raw.get("seen_pos")
        if seen_pos is None or seen_pos > MAX_POSITION_AGE_S or raw.get("lat") is None:
            continue
        ac = Aircraft.from_json(raw)
        ts = round(now - seen_pos, 1)
        if abs(ts - last_saved.get(ac.hex, 0)) < 0.25:  # same position as last poll
            continue
        dist = ground_km(receiver, (ac.lat, ac.lon)) if receiver else None
        if dist is not None and dist > MAX_RANGE_KM:
            continue
        last_saved[ac.hex] = ts
        alt_baro = 0 if ac.altitude == "ground" else ac.altitude
        rows.append((ts, ac.hex, ac.flight or None, ac.lat, ac.lon, alt_baro,
                     raw.get("alt_geom"), ac.speed, ac.track,
                     None if dist is None else round(dist, 3)))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=AIRCRAFT_JSON, help="aircraft.json path or URL")
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL_S)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = connect()
    receiver = None
    next_receiver_check = 0.0
    last_saved = {}
    coverage_id = None   # row in logger_coverage being extended while data is fresh
    saved_since_report = 0
    next_report = time.monotonic() + 600

    log.info("logging %s to the database", args.source)
    while True:
        started = time.monotonic()

        if receiver is None and started >= next_receiver_check:
            receiver = read_receiver_location()
            if receiver:
                set_meta(db, "receiver_lat", receiver[0])
                set_meta(db, "receiver_lon", receiver[1])
                db.commit()
                log.info("receiver at %.4f, %.4f", *receiver)
            else:
                log.warning("receiver location not set: logging all ranges, no distances "
                            "(fix with: sudo readsb-set-location <lat> <lon>)")
                next_receiver_check = started + 600

        try:
            data = read_json(args.source)
            fresh = time.time() - data["now"] < STALE_AFTER_S
        except (OSError, ValueError, KeyError) as e:
            log.warning("can't read %s: %s", args.source, e)
            fresh = False

        if fresh:
            rows = new_positions(data, receiver, last_saved)
            db.executemany(INSERT, rows)
            saved_since_report += len(rows)
            if coverage_id is None:
                coverage_id = db.execute("INSERT INTO logger_coverage VALUES (?, ?)",
                                         (data["now"], data["now"])).lastrowid
            else:
                db.execute("UPDATE logger_coverage SET end_ts = ? WHERE rowid = ?",
                           (data["now"], coverage_id))
            db.commit()
        else:
            coverage_id = None   # a gap: the next fresh poll starts a new coverage period

        if started >= next_report:
            log.info("saved %d positions in the last 10 min", saved_since_report)
            saved_since_report = 0
            next_report = started + 600
            # Forget aircraft not heard for 10 minutes so the dict stays small.
            cutoff = time.time() - 600
            last_saved = {h: t for h, t in last_saved.items() if t > cutoff}

        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
