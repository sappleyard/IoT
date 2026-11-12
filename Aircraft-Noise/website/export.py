#!/usr/bin/env python3
"""Write passes.json, the data behind the public site, from the databases on the Pi.

This is the only thing that decides what leaves the house, so it writes a fixed list of fields
and nothing else. Left out on purpose, because each could help locate the house from public
ADS-B history: distances, altitudes, speeds, headings, positions and exact times.
  - times are floored to the hour, and rows within an hour are sorted by callsign, not time
  - only complete days are written (nothing from today)
Highlights are the most unusual flights the mics heard on the last complete day, scored by the
announcer's uniqueness model (uniqueness.py) against the heard flights before each one.

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
from aircraft_announcer.uniqueness import Uniqueness                 # noqa: E402
from aircraft_announcer.config import ANNOUNCER_DB, NOISE_DB         # noqa: E402

TIME_STEP_S = 3600       # published times are to the hour
LOOKUP_PAUSE_S = 0.5     # between network requests: adsbdb is a free service
HIGHLIGHTS = 8           # at most this many flights
HIGHLIGHT_PERCENTILE = 90    # beyond the announced few, flights at least this unusual
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


def highlights(day):
    """The last complete day's most unusual heard flights, most unusual first: each
    [hour, callsign, airline, origin, destination, aircraft, why]. Every heard flight is scored
    in time order against the ones before it, as the announcer scores it live."""
    db = sqlite3.connect(f"file:{ANNOUNCER_DB}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT ts, callsign, airline, origin, destination, aircraft_model, "
                      "category FROM heard ORDER BY ts").fetchall()
    db.close()
    start = datetime.combine(day, datetime.min.time()).timestamp()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
    history, picked, seen = Uniqueness(), [], set()
    for row in rows:
        if row["ts"] >= end:
            break
        score = history.score(dict(row), row["ts"])
        callsign = (row["callsign"] or "").strip()
        # a flight heard twice (both mics, or two windows) is one highlight
        if row["ts"] < start or not score["phrase"] or callsign in seen:
            continue
        if score["unique"] or score["percentile"] >= HIGHLIGHT_PERCENTILE:
            seen.add(callsign)
            when = datetime.fromtimestamp(row["ts"] - row["ts"] % TIME_STEP_S)
            picked.append((score["unique"], score["percentile"], [
                when.strftime("%H:%M"), callsign, row["airline"] or "", row["origin"] or "",
                row["destination"] or "", row["aircraft_model"] or "",
                score["phrase"].rstrip(":")]))
    picked.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return [p[2] for p in picked[:HIGHLIGHTS]]


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

    last = date.today() - timedelta(days=1)
    try:
        best = highlights(last)
    except sqlite3.Error as e:      # no announcer history: the site just leaves them out
        print(f"No highlights: {e}")
        best = []
    Path(out).write_text(json.dumps({"generated": date.today().isoformat(), "days": days,
                                     "passes": rows,
                                     "highlights": {"date": last.isoformat(), "flights": best}},
                                    ensure_ascii=False,
                                    separators=(",", ":")))
    print(f"Wrote {len(rows)} passes over {len(days)} days to {out}")


if __name__ == "__main__":
    main(sys.argv[1])
