"""Wind and weather at Heathrow from Open-Meteo (free, no key), as hourly arrays on UTC hours.

Every request is made for Heathrow's coordinates, never the house's: the house's location must
not reach any outside service. Heathrow's wind is also what decides its runway direction.

All times are Unix seconds on whole UTC hours (timeformat=unixtime), so clock changes never
come into it; days are turned into local dates by the caller.
"""

import json
import math
import time
import urllib.parse
import urllib.request

import numpy as np

HEATHROW = (51.4700, -0.4543)   # EGLL
RUNWAY_DEG = 270                # runways 09/27, close enough to 270 true for a wind component
TIMEOUT_S = 60
RETRIES = 3

FORECAST = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
PREVIOUS = "https://previous-runs-api.open-meteo.com/v1/forecast"
ENSEMBLE_MODEL = "ecmwf_ifs025"     # 51 members, hourly out past 10 days
ANALYSIS_MODEL = "ecmwf_ifs025"     # the same model for the past, so fit and forecast agree

WMO_WORDS = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog",
             48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle", 56: "Freezing drizzle",
             57: "Freezing drizzle", 61: "Light rain", 63: "Rain", 65: "Heavy rain",
             66: "Freezing rain", 67: "Freezing rain", 71: "Light snow", 73: "Snow",
             75: "Heavy snow", 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers",
             85: "Snow showers", 86: "Snow showers", 95: "Thunderstorms", 96: "Thunderstorms",
             99: "Thunderstorms"}
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def get(url, **params):
    """GET a JSON response, with a few retries: the job runs unattended once a day."""
    params = {"latitude": HEATHROW[0], "longitude": HEATHROW[1], "timeformat": "unixtime",
              "wind_speed_unit": "kn", **params}
    full = url + "?" + urllib.parse.urlencode(params)
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(full, timeout=TIMEOUT_S) as response:
                return json.load(response)
        except OSError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(10 * (attempt + 1))


def tailwind(speed_kt, direction_deg):
    """Tailwind (kt) for an aircraft landing westerly on runway 27: negative is a headwind.

    Heathrow lands into the wind, so a growing tailwind on 27 is what makes it go easterly.
    """
    speed = np.asarray(speed_kt, dtype=float)
    direction = np.asarray(direction_deg, dtype=float)
    return -speed * np.cos(np.radians(direction - RUNWAY_DEG))


def _array(values):
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def analysis(start_ts, end_ts):
    """Tailwind for past hours [start, end): each hour from the model run closest to it.

    Open-Meteo's previous-runs archive with no lead ("day 0") is a continuous record of the
    latest run's first hours, so it is as near to observed wind as a model gets, and it is the
    same model as the ensemble used for the future.
    """
    data = get(PREVIOUS, models=ANALYSIS_MODEL, hourly="wind_speed_10m,wind_direction_10m",
               start_date=time.strftime("%Y-%m-%d", time.gmtime(start_ts)),
               end_date=time.strftime("%Y-%m-%d", time.gmtime(end_ts)))["hourly"]
    ts = np.array(data["time"], dtype=np.int64)
    keep = (ts >= start_ts) & (ts < end_ts)
    tail = tailwind(_array(data["wind_speed_10m"]), _array(data["wind_direction_10m"]))
    return ts[keep], tail[keep]


def ensemble(days):
    """Hourly tailwind for each ensemble member from today 00 UTC: (hours, members x hours)."""
    data = get(ENSEMBLE, models=ENSEMBLE_MODEL, forecast_days=days,
               hourly="wind_speed_10m,wind_direction_10m")["hourly"]
    ts = np.array(data["time"], dtype=np.int64)
    members = sorted(k[len("wind_speed_10m_"):] for k in data
                     if k.startswith("wind_speed_10m_member"))
    names = [""] + ["_" + m for m in members]      # the control run plus each member
    tails = np.array([tailwind(_array(data["wind_speed_10m" + n]),
                               _array(data["wind_direction_10m" + n])) for n in names])
    return ts, tails


def deterministic(days):
    """The best-guess hourly forecast for the daily summary: (hours, speed km/h, dir, code)."""
    data = get(FORECAST, forecast_days=days, wind_speed_unit="kmh",
               hourly="wind_speed_10m,wind_direction_10m,weather_code")["hourly"]
    return (np.array(data["time"], dtype=np.int64), _array(data["wind_speed_10m"]),
            _array(data["wind_direction_10m"]), _array(data["weather_code"]))


def summary(speed_kmh, direction_deg, codes):
    """A day's wind and weather in a few words, e.g. ("SW 18 km/h", "Showers").

    Wind is the vector mean over the hours given (the caller passes daytime), so a day of
    swinging light winds reads as light. Weather is the most significant WMO code of those
    hours, as Open-Meteo's own daily code is.
    """
    ok = ~(np.isnan(speed_kmh) | np.isnan(direction_deg))
    wind = None
    if ok.any():
        # Direction is where the wind comes from: average the vectors, then point back.
        u = np.mean(speed_kmh[ok] * np.sin(np.radians(direction_deg[ok])))
        v = np.mean(speed_kmh[ok] * np.cos(np.radians(direction_deg[ok])))
        mean_dir = math.degrees(math.atan2(u, v)) % 360
        wind = f"{COMPASS[int((mean_dir + 22.5) // 45) % 8]} {round(np.mean(speed_kmh[ok]))} km/h"
    codes = codes[~np.isnan(codes)]
    weather = WMO_WORDS.get(int(codes.max())) if len(codes) else None
    return wind, weather
