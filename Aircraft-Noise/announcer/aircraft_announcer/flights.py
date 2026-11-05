"""Which aircraft was just heard, and what to say about it.

Tracker keeps the last TRACK_HISTORY_S seconds of every aircraft from readsb's aircraft.json.
When the mics hear an aircraft, match() picks the one whose sound was arriving then: sound sent
from an aircraft at slant distance r reaches the house r / 343 m/s later, the same alignment the
training labels use. The nearest such aircraft wins (nearest = loudest).

Lookups, all cached in announcer.db or CACHE_DIR, and each optional (a failed lookup just
leaves that part out of the announcement):
  route, airport and airline names  VRS standing data, by callsign (one file per airline)
  route, if VRS doesn't know it     adsbdb, by callsign
  aircraft model                    adsbdb, by ICAO address
"""

import csv
import io
import json
import logging
import math
import re
import time
import urllib.error
import urllib.request
from collections import deque

from .config import (ADSBDB_REFRESH_S, ADSBDB_URL, AIRCRAFT_JSON, ARRIVAL_AFTER_S,
                     ARRIVAL_BEFORE_S, CACHE_DIR, HTTP_TIMEOUT_S, LOCATION_FILE, MATCH_MAX_KM_DAY,
                     MATCH_MAX_KM_NIGHT, NIGHT_HOURS,
                     SPEED_OF_SOUND, TRACK_HISTORY_S, VRS_REFRESH_S, VRS_URL)

log = logging.getLogger("announcer")

GROUND_MAX_ALT_FT = 100    # as the collection's ADS-B logger: low and slow = on the ground
GROUND_MAX_SPEED_KT = 30


def home_location():
    """(lat, lon, alt_m, geoid_m) from the collection's location.json."""
    try:
        loc = json.loads(LOCATION_FILE.read_text())
        return loc["lat"], loc["lon"], loc.get("alt_m", 0.0), loc.get("geoid_m", 0.0)
    except (OSError, ValueError, KeyError) as e:
        raise SystemExit(f"Need {LOCATION_FILE} (see the README): {e}")


def ground_km(a, b):
    """Great-circle distance between two (lat, lon) points in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


# --- Live aircraft -----------------------------------------------------------------------------

class Tracker:
    """Recent positions of every airborne aircraft: hex -> deque of (ts, slant_km, alt_ft)."""

    def __init__(self):
        lat, lon, self.alt_m, self.geoid_m = home_location()
        self.home = (lat, lon)
        self.tracks = {}
        self.info = {}       # hex -> {"callsign", "category"}, latest seen
        self.last_now = 0.0

    def poll(self):
        """Read aircraft.json once and add any new positions."""
        try:
            with open(AIRCRAFT_JSON) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = data.get("now", time.time())
        if now == self.last_now:
            return
        self.last_now = now
        for a in data.get("aircraft", []):
            if "lat" not in a or "seen_pos" not in a or a.get("seen_pos", 99) > 5:
                continue
            alt = a.get("alt_geom")
            alt = round(alt - self.geoid_m / 0.3048) if alt is not None else a.get("alt_baro")
            if a.get("alt_baro") == "ground" or not isinstance(alt, (int, float)):
                continue
            if alt < GROUND_MAX_ALT_FT and a.get("gs", 999) < GROUND_MAX_SPEED_KT:
                continue
            dist = ground_km(self.home, (a["lat"], a["lon"]))
            slant = math.hypot(dist, (alt * 0.3048 - self.alt_m) / 1000)
            hex_ = a["hex"]
            self.tracks.setdefault(hex_, deque()).append((now - a["seen_pos"], slant, alt))
            self.info[hex_] = {"callsign": (a.get("flight") or "").strip() or None,
                               "category": a.get("category")}
        for hex_ in list(self.tracks):
            track = self.tracks[hex_]
            while track and track[0][0] < now - TRACK_HISTORY_S:
                track.popleft()
            if not track:
                del self.tracks[hex_], self.info[hex_]

    def match(self, t):
        """The aircraft heard at time t: the nearest one whose sound arrived within the span
        the model fires on, within the day or night audible distance. None if none fits. Returns hex, callsign, slant, altitude."""
        hour = time.localtime(t).tm_hour
        night = hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]
        max_km = MATCH_MAX_KM_NIGHT if night else MATCH_MAX_KM_DAY
        best = None
        for hex_, track in self.tracks.items():
            for ts, slant, alt in track:
                arrival = ts + slant * 1000 / SPEED_OF_SOUND
                if (t - ARRIVAL_BEFORE_S <= arrival <= t + ARRIVAL_AFTER_S
                        and slant <= max_km and (best is None or slant < best["slant_km"])):
                    best = {"hex": hex_, "slant_km": slant, "alt_ft": alt, **self.info[hex_]}
        return best


# --- Lookups ---------------------------------------------------------------------------------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "aircraft-announcer (home project)"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
        return r.read()


def vrs_csv(path):
    """Rows of a VRS standing-data CSV, downloaded once and refreshed weekly. [] if missing."""
    local = CACHE_DIR / "vrs" / path
    if not local.exists() or time.time() - local.stat().st_mtime > VRS_REFRESH_S:
        local.parent.mkdir(parents=True, exist_ok=True)
        try:
            local.write_bytes(fetch(f"{VRS_URL}/{path}"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                local.write_bytes(b"")    # VRS has no such file: remember that for a week
            else:
                log.warning("VRS %s: %s", path, e)
            if not local.exists():
                return []
        except OSError as e:
            log.warning("VRS %s: %s", path, e)
            if not local.exists():
                return []
    return list(csv.DictReader(io.StringIO(local.read_text(encoding="utf-8-sig"))))


def spoken_airport(icao):
    """How people name the airport: the name when it starts with the city, which tells a city's
    airports apart ('London Heathrow', 'London Gatwick'), otherwise the city ('Las Vegas', not
    'Harry Reid International'; 'Kraków', not 'John Paul II International Airport Kraków-Balice')."""
    for row in vrs_csv(f"airports/schema-01/{icao[0]}/{icao[:2]}.csv"):
        if icao in (row["ICAO"], row["Code"]):
            name = re.sub(r"\s+(International\s+)?(Airport|Airfield|Aerodrome|Heliport)$", "",
                          row["Name"].strip())
            city = row["Location"].strip()
            return name if not city or name.lower().startswith(city.lower()) else city
    return None


def airline_name(code):
    for row in vrs_csv("airlines/schema-01/airlines.csv"):
        if row["ICAO"] == code or row["Code"] == code:
            return row["Name"].strip()
    return None


def model_name(a):
    """Maker and model from an adsbdb aircraft record: 'Airbus A320', 'Boeing 777', or None.

    The model is rarely the type field's first word, so look for the part that has a number:
      A320-251N -> A320           777 336ER -> 777             737MAX 8 -> 737MAX
      A.109 E Power -> A109       AW.139 -> AW139              EMB-190 LR -> E190
      Twin Squirrel AS.355 N -> AS355                          MBB-BK 117 D-2 (H145) -> H145
    With no such part, the ICAO type code ('DH8D') is used instead.
    """
    # "Airbus SAS" -> "Airbus", "The Boeing Company" -> "Boeing", "AIRBUS" -> "Airbus"
    words = [w for w in (a.get("manufacturer") or "").split() if w.lower() != "the"]
    maker = re.split(r"-", words[0])[0] if words else ""
    maker = maker.capitalize() if maker.isupper() and len(maker) > 4 else maker
    if maker.lower() == "de" and len(words) > 1:      # "De Havilland Canada" -> "De Havilland"
        maker = f"De {words[1]}"
    if re.match(r"DHC-?8", a.get("type") or ""):      # "DHC-8-402" is said "Dash 8"
        return f"{maker} Dash 8".strip()
    kind = re.sub(r"\b([A-Za-z]{1,4})[.-](?=\d)", r"\1", a.get("type") or "")    # A.109 -> A109
    found = (re.search(r"\b[A-Za-z]{1,4}\d{2,4}[A-Za-z]*\b", kind)
             or re.search(r"\b\d{2,4}[A-Za-z]*\b", kind))
    model = found.group(0) if found else (a.get("icao_type") or "").strip()
    if not model:
        return None
    model = re.sub(r"^EMB(?=\d)", "E", model)
    # Don't repeat a maker already in the name.
    return model if not maker or model.lower().startswith(maker.lower()) else f"{maker} {model}"


class Lookups:
    """Flight details for an announcement. Network results are cached in announcer.db."""

    def __init__(self, db):
        self.db = db
        db.execute("CREATE TABLE IF NOT EXISTS lookup_cache (key TEXT PRIMARY KEY, "
                   "fetched REAL NOT NULL, value TEXT)")

    def adsbdb(self, path):
        """adsbdb's response for path (e.g. 'aircraft/400a0e'), cached; None if unknown."""
        row = self.db.execute("SELECT fetched, value FROM lookup_cache WHERE key = ?",
                              (path,)).fetchone()
        if row and time.time() - row[0] < ADSBDB_REFRESH_S:
            return json.loads(row[1]) if row[1] else None
        try:
            value = json.loads(fetch(f"{ADSBDB_URL}/{path}"))["response"]
            value = value if isinstance(value, dict) else None
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.warning("adsbdb %s: %s", path, e)
                return json.loads(row[1]) if row and row[1] else None
            value = None    # adsbdb doesn't know it: remember that too
        except (OSError, ValueError) as e:
            log.warning("adsbdb %s: %s", path, e)
            return json.loads(row[1]) if row and row[1] else None
        self.db.execute("INSERT OR REPLACE INTO lookup_cache VALUES (?, ?, ?)",
                        (path, time.time(), json.dumps(value) if value else None))
        self.db.commit()
        return value

    def route(self, callsign):
        """(origin, destination) as spoken airport names, or (None, None)."""
        m = re.fullmatch(r"([A-Z]{3})(\d\w*)", callsign or "")
        if m:
            for row in vrs_csv(f"routes/schema-01/{callsign[0]}/{m.group(1)}-all.csv"):
                if row["Callsign"] == callsign:
                    stops = row["AirportCodes"].split("-")
                    return spoken_airport(stops[0]), spoken_airport(stops[-1])
        r = self.adsbdb(f"callsign/{callsign}") if callsign else None
        if r and "flightroute" in r:
            ends = [r["flightroute"].get(k) or {} for k in ("origin", "destination")]
            names = [spoken_airport(e["icao_code"]) if e.get("icao_code") else None for e in ends]
            return tuple(n or re.sub(r"\s+(International\s+)?Airport$", "", e.get("name", "")) or None
                         for n, e in zip(names, ends))
        return None, None

    def aircraft_model(self, hex_):
        """'Airbus A320', 'Boeing 777', ... or None."""
        r = self.adsbdb(f"aircraft/{hex_}")
        return model_name((r or {}).get("aircraft") or {})

    def details(self, heard):
        """Everything to announce about a matched aircraft (a dict from Tracker.match)."""
        callsign = heard.get("callsign")
        origin, destination = self.route(callsign)
        m = re.fullmatch(r"([A-Z]{3})(\d\w*)", callsign or "")
        return {**heard,
                "airline": airline_name(m.group(1)) if m else None,
                "flight_number": m.group(2) if m else None,
                "origin": origin, "destination": destination,
                "model": self.aircraft_model(heard["hex"])}
