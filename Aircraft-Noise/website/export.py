#!/usr/bin/env python3
"""Write passes.json, the data behind the public site, from the databases on the Pi.

This is the only thing that decides what leaves the house, so it writes a fixed list of fields
and nothing else. Left out on purpose, because each could help locate the house from public
ADS-B history: distances, altitudes, speeds, headings, positions and exact times.
  - times are floored to the hour, and rows within an hour are sorted by callsign, not time
  - only complete days are written (nothing from today)

    export.py OUT.json
"""

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path.home() / "aircraft-announcer"))
from aircraft_announcer import flights                               # noqa: E402
from aircraft_announcer.config import ANNOUNCER_DB, NOISE_DB         # noqa: E402

TIME_STEP_S = 3600       # published times are to the hour
LOOKUP_PAUSE_S = 0.5     # between network requests: adsbdb is a free service
KINDS = {"A1": "Light", "A2": "Small", "A3": "Large", "A4": "Large", "A5": "Heavy",
         "A7": "Helicopter"}

real_fetch = flights.fetch


def polite_fetch(url):
    time.sleep(LOOKUP_PAUSE_S)
    return real_fetch(url)


flights.fetch = polite_fetch


def covered_hours(spans, day):
    """Hours of day that the logger was running."""
    start = datetime.combine(day, datetime.min.time()).timestamp()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
    return sum(max(0, min(b, end) - max(a, start)) for a, b in spans) / 3600


def main(out):
    noise = sqlite3.connect(f"file:{NOISE_DB}?mode=ro", uri=True, timeout=30)
    today = datetime.combine(date.today(), datetime.min.time()).timestamp()
    passes = noise.execute("SELECT hex, callsign, closest_ts, category FROM passes "
                           "WHERE closest_ts < ? ORDER BY closest_ts", (today,)).fetchall()
    spans = noise.execute("SELECT start_ts, end_ts FROM logger_coverage").fetchall()
    noise.close()

    lookups = flights.Lookups(sqlite3.connect(ANNOUNCER_DB, timeout=30))
    route = lru_cache(None)(lookups.route)
    model = lru_cache(None)(lookups.aircraft_model)
    airline = lru_cache(None)(flights.airline_name)

    rows = []
    for i, (hex_, callsign, ts, category) in enumerate(passes):
        if i % 200 == 0:
            print(f"{i}/{len(passes)}", flush=True)
        when = datetime.fromtimestamp(ts - ts % TIME_STEP_S)
        callsign = (callsign or "").strip()
        origin, destination = route(callsign) if callsign else (None, None)
        code = callsign[:3] if callsign[:3].isalpha() and callsign[3:4].isdigit() else None
        rows.append([when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), callsign,
                     (airline(code) if code else None) or "", origin or "", destination or "",
                     model(hex_) or "", KINDS.get(category, "")])
    rows.sort()     # by date, hour, then callsign: no order within an hour

    first = date.fromisoformat(rows[0][0])
    days = []
    day = first
    while day < date.today():
        days.append([day.isoformat(), round(covered_hours(spans, day), 1)])
        day += timedelta(days=1)

    Path(out).write_text(json.dumps({"generated": date.today().isoformat(), "days": days,
                                     "passes": rows}, ensure_ascii=False,
                                    separators=(",", ":")))
    print(f"Wrote {len(rows)} passes over {len(days)} days to {out}")


if __name__ == "__main__":
    main(sys.argv[1])
