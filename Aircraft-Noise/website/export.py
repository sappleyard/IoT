#!/usr/bin/env python3
"""Write passes.json, the data behind the public site, from the databases on the Pi.

This is the only thing that decides what leaves the house, so it writes a fixed list of fields
and nothing else. Left out on purpose, because each could help locate the house from public
ADS-B history: distances, altitudes, speeds, headings, positions and exact times.
  - times are floored to the hour, and rows within an hour are sorted by callsign, not time
  - only complete days are written (nothing from today)
Each pass is marked heard (1) if the announcer heard that aircraft during it, 0 if not, and
null before the announcer started logging (then nobody was listening).
Highlights are the most unusual flights the mics heard on each complete day, scored by the
announcer's uniqueness model (uniqueness.py) against the heard flights before each one.

    export.py OUT.json
"""

import bisect
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path.home() / "aircraft-announcer"))
from aircraft_announcer import flights                               # noqa: E402
from aircraft_announcer.config import UNIQUE_MIN_HISTORY             # noqa: E402
from aircraft_announcer.uniqueness import Uniqueness                 # noqa: E402
from aircraft_announcer.config import ANNOUNCER_DB, NOISE_DB         # noqa: E402

TIME_STEP_S = 3600       # published times are to the hour
LOOKUP_PAUSE_S = 0.5     # between network requests: adsbdb is a free service
HIGHLIGHTS = 8           # at most this many rare flights (and every ALWAYS_UNIQUE one after)
HIGHLIGHT_PERCENTILE = 90    # beyond the announced few, flights at least this unusual
HEARD_BEFORE_S = 60     # a heard row counts for a pass from this long before it starts...
HEARD_AFTER_S = 120     # ...to this long after it ends (the sound arrives slant / 343 s late)
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


NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def label(phrase):
    """The site's short label for an announcer lead-in: 'First time heard:' -> 'First recorded',
    'A rare visitor:' -> 'Rare', 'The first Boeing 747 in two weeks:' -> 'First in 2 weeks'.
    Anything else (G.O.A.T.) is shown as it is."""
    phrase = phrase.rstrip(":")
    if phrase == "First time heard":
        return "First recorded"
    if phrase == "A rare visitor":
        return "Rare"
    m = re.fullmatch(r"The first .+ in (\w+)(?: (days|weeks))?", phrase)
    if m:
        n, unit = m.groups()
        n = str(NUMBERS.index(n)) if n in NUMBERS else n
        return f"First in {n} {unit}" if unit else f"First in {n}"
    return phrase


def heard_times():
    """{hex: sorted heard times} from the announcer's log, and when it started (None: no log)."""
    try:
        db = sqlite3.connect(f"file:{ANNOUNCER_DB}?mode=ro", uri=True, timeout=30)
        rows = db.execute("SELECT lower(hex), ts FROM heard ORDER BY ts").fetchall()
        db.close()
    except sqlite3.Error as e:
        print(f"No heard flights: {e}")
        return {}, None
    times = {}
    for hex_, ts in rows:
        times.setdefault(hex_, []).append(ts)
    return times, rows[0][1] if rows else None


def was_heard(times, hex_, first, last):
    t = times.get(hex_.lower(), ())
    i = bisect.bisect_left(t, first - HEARD_BEFORE_S)
    return i < len(t) and t[i] <= last + HEARD_AFTER_S


def highlights(before):
    """Each complete day's most unusual heard flights, most unusual first: [{date, flights}]
    with each flight [hour, callsign, airline, origin, destination, aircraft, why], for days
    before `before` (a date). Every heard flight is scored in time order against the ones
    before it, as the announcer scores it live."""
    db = sqlite3.connect(f"file:{ANNOUNCER_DB}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT ts, callsign, airline, origin, destination, aircraft_model, "
                      "category FROM heard ORDER BY ts").fetchall()
    db.close()
    end = datetime.combine(before, datetime.min.time()).timestamp()
    history, days = Uniqueness(), {}
    for row in rows:
        if row["ts"] >= end:
            break
        score = history.score(dict(row), row["ts"])
        callsign = (row["callsign"] or "").strip()
        when = datetime.fromtimestamp(row["ts"] - row["ts"] % TIME_STEP_S)
        picked, seen = days.setdefault(when.strftime("%Y-%m-%d"), ([], set()))
        # a flight heard twice (both mics, or two windows) is one highlight
        if not score["lead_in"] or callsign in seen:
            continue
        if score["unique"] or (history.flights > UNIQUE_MIN_HISTORY
                               and score["percentile"] >= HIGHLIGHT_PERCENTILE):
            seen.add(callsign)
            picked.append((score["always"], score["unique"], score["percentile"], [
                when.strftime("%H:%M"), callsign, row["airline"] or "", row["origin"] or "",
                row["destination"] or "", row["aircraft_model"] or "",
                label(score["lead_in"])]))
    out = []
    for day, (picked, _) in sorted(days.items()):
        picked.sort(key=lambda p: p[:3], reverse=True)
        always = [p[3] for p in picked if p[0]]
        rare = [p[3] for p in picked if not p[0]][:HIGHLIGHTS]
        # nothing is unusual while the history warms up (UNIQUE_MIN_HISTORY): start after
        if picked or out:
            out.append({"date": day, "flights": rare + always})
    return out


def main(out):
    noise = sqlite3.connect(f"file:{NOISE_DB}?mode=ro", uri=True, timeout=30)
    today = datetime.combine(date.today(), datetime.min.time()).timestamp()
    passes = noise.execute("SELECT hex, callsign, closest_ts, category, first_ts, last_ts "
                           "FROM passes "
                           "WHERE closest_ts < ? ORDER BY closest_ts", (today,)).fetchall()
    spans = noise.execute("SELECT start_ts, end_ts FROM logger_coverage").fetchall()
    noise.close()

    lookups = flights.Lookups(sqlite3.connect(ANNOUNCER_DB, timeout=30))
    route = lru_cache(None)(lookups.route)
    model = lru_cache(None)(lookups.aircraft_model)
    airline = lru_cache(None)(flights.airline_name)

    times, listening = heard_times()
    rows = []
    for i, (hex_, callsign, ts, category, first, last) in enumerate(passes):
        if i % 200 == 0:
            print(f"{i}/{len(passes)}", flush=True)
        when = datetime.fromtimestamp(ts - ts % TIME_STEP_S)
        callsign = (callsign or "").strip()
        origin, destination = route(callsign) if callsign else (None, None)
        code = callsign[:3] if callsign[:3].isalpha() and callsign[3:4].isdigit() else None
        rows.append([when.strftime("%Y-%m-%d"), when.strftime("%H:%M"), callsign,
                     (airline(code) if code else None) or "", origin or "", destination or "",
                     model(hex_) or "", KINDS.get(category, ""),
                     None if listening is None or first < listening
                     else int(was_heard(times, hex_, first, last))])
    rows.sort(key=lambda r: r[:8])     # by date, hour, then callsign: no order within an hour

    first = date.fromisoformat(rows[0][0])
    days = []
    day = first
    while day < date.today():
        days.append([day.isoformat(), round(covered_hours(spans, day), 1)])
        day += timedelta(days=1)

    try:
        best = highlights(date.today())
    except sqlite3.Error as e:      # no announcer history: the site just leaves them out
        print(f"No highlights: {e}")
        best = []
    Path(out).write_text(json.dumps({"generated": date.today().isoformat(), "days": days,
                                     "passes": rows,
                                     "highlights": best},
                                    ensure_ascii=False,
                                    separators=(",", ":")))
    print(f"Wrote {len(rows)} passes over {len(days)} days to {out}")


if __name__ == "__main__":
    main(sys.argv[1])
