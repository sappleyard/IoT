"""Count lost ESP-NOW packets per node from gaps in seq (the step 3 range test).

Leave the nodes where they'll live for a day, then:

    python3 -m aircraft_noise.nodes.gaps              # last 24 h
    python3 -m aircraft_noise.nodes.gaps --hours 2

A restart (uptime going backwards) resets seq, so it isn't counted as loss.
If a far room loses more than a few percent, turn on long-range mode on all three boards.
"""

import argparse
import time

from ..db import connect

NAMES = {0: "bridge", 1: "A", 2: "B"}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()

    since = time.time() - args.hours * 3600
    rows = connect().execute(
        "SELECT node, ts, seq, uptime_ms, rssi FROM node_reports WHERE ts >= ? ORDER BY node, ts",
        (since,))

    stats = {}
    for node, ts, seq, uptime, rssi in rows:
        s = stats.setdefault(node, {"got": 0, "lost": 0, "restarts": 0, "prev": None,
                                    "rssi": [], "last": ts})
        if s["prev"] is not None:
            prev_seq, prev_uptime = s["prev"]
            if uptime < prev_uptime:
                s["restarts"] += 1
            else:
                s["lost"] += (seq - prev_seq - 1) & 0xFFFF
        s["prev"] = (seq, uptime)
        s["got"] += 1
        s["last"] = ts
        if rssi is not None:
            s["rssi"].append(rssi)

    if not stats:
        print(f"No reports in the last {args.hours:g} h. Is the bridge-link service running?")
        return
    print(f"Last {args.hours:g} h")
    print(f"{'NODE':<8}{'RECEIVED':>10}{'LOST':>8}{'LOSS %':>8}{'RESTARTS':>10}"
          f"{'MEAN RSSI':>11}  LAST SEEN")
    for node in sorted(stats):
        s = stats[node]
        total = s["got"] + s["lost"]
        rssi = f"{sum(s['rssi']) / len(s['rssi']):.0f}" if s["rssi"] else "-"
        print(f"{NAMES.get(node, node):<8}{s['got']:>10}{s['lost']:>8}"
              f"{100 * s['lost'] / total:>8.1f}{s['restarts']:>10}{rssi:>11}  "
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s['last']))}")


if __name__ == "__main__":
    main()
