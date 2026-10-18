#!/usr/bin/env python3
"""Live table of aircraft received by the Pi's ADS-B receiver.

Reads the aircraft.json feed that readsb/tar1090 publish over HTTP, so it runs
on the Pi itself or on any machine on the tailnet.

    python3 adsb_monitor.py                 # live view, refreshes every 2 s
    python3 adsb_monitor.py --once          # print one snapshot and exit
    python3 adsb_monitor.py --host localhost --interval 5
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_HOST = "sebpi"


@dataclass
class Aircraft:
    hex: str
    flight: str
    altitude: object  # feet, or "ground"
    speed: float | None  # knots
    track: float | None  # degrees
    lat: float | None
    lon: float | None
    squawk: str
    rssi: float | None  # dBFS
    seen: float  # seconds since last message

    @classmethod
    def from_json(cls, a):
        return cls(
            hex=a["hex"].upper(),
            flight=a.get("flight", "").strip(),
            altitude=a.get("alt_baro"),
            speed=a.get("gs"),
            track=a.get("track"),
            lat=a.get("lat"),
            lon=a.get("lon"),
            squawk=a.get("squawk", ""),
            rssi=a.get("rssi"),
            seen=a.get("seen", 0.0),
        )


def fetch_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def fetch_receiver_location(host):
    """Return (lat, lon) of the receiver if readsb has one set, else None."""
    try:
        rx = fetch_json(f"http://{host}/tar1090/data/receiver.json")
    except (urllib.error.URLError, ValueError, OSError):
        return None
    if "lat" in rx and "lon" in rx:
        return rx["lat"], rx["lon"]
    return None


def fetch_aircraft(host):
    data = fetch_json(f"http://{host}/tar1090/data/aircraft.json")
    planes = [Aircraft.from_json(a) for a in data.get("aircraft", [])]
    return data.get("messages", 0), planes


def distance_nm(a, b):
    """Great-circle distance between two (lat, lon) points in nautical miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3440.065 * math.asin(math.sqrt(h))


def fmt(value, spec="", missing="-"):
    return missing if value is None else format(value, spec)


def render(planes, messages, receiver):
    lines = [f"{'HEX':<7}{'FLIGHT':<9}{'ALT ft':>8}{'SPD kt':>8}{'HDG':>5}"
             f"{'DIST nm':>9}{'SQWK':>6}{'RSSI':>7}{'SEEN':>6}"]
    rows = []
    for p in planes:
        dist = None
        if receiver and p.lat is not None and p.lon is not None:
            dist = distance_nm(receiver, (p.lat, p.lon))
        rows.append((dist, p))
    # Nearest first when we know distances, otherwise strongest signal first.
    rows.sort(key=lambda r: (r[0] is None, r[0] or 0, -(r[1].rssi or -99)))
    for dist, p in rows:
        alt = p.altitude if p.altitude == "ground" else fmt(p.altitude, "d")
        lines.append(
            f"{p.hex:<7}{p.flight or '-':<9}{alt:>8}{fmt(p.speed, '.0f'):>8}"
            f"{fmt(p.track, '.0f'):>5}{fmt(dist, '.1f'):>9}{p.squawk or '-':>6}"
            f"{fmt(p.rssi, '.1f'):>7}{p.seen:>6.0f}"
        )
    with_pos = sum(1 for p in planes if p.lat is not None)
    lines.append("")
    lines.append(f"{len(planes)} aircraft ({with_pos} with position), "
                 f"{messages} messages since readsb started")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"host running readsb/tar1090 (default: {DEFAULT_HOST})")
    parser.add_argument("--interval", type=float, default=2.0, help="refresh seconds")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    args = parser.parse_args()

    receiver = fetch_receiver_location(args.host)
    while True:
        try:
            messages, planes = fetch_aircraft(args.host)
            out = render(planes, messages, receiver)
        except (urllib.error.URLError, OSError) as e:
            out = f"Can't reach readsb on {args.host}: {e}\nIs setup_pi.sh installed and the Pi online?"
            if args.once:
                print(out, file=sys.stderr)
                return 1
        if args.once:
            print(out)
            return 0
        os.system("clear")
        print(f"ADS-B via {args.host} - {time.strftime('%H:%M:%S')} (Ctrl+C to quit)\n")
        print(out)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
