#!/usr/bin/env python3
"""Write outlook.json: passes over the house for each of the next 10 days, with a likely range.

Runs on the Pi from publish.sh, after export.py. The model is in model.py; Heathrow's wind
comes from Open-Meteo (weather.py), asked for Heathrow's coordinates only.

Published per day: date, passes (median of the runs), low and high (10th and 90th
percentiles), wind and weather in a few words. Nothing else: no times, positions, distances
or altitudes, so the file says nothing about where the house is.

    forecast.py OUT.json [--db PATH] [--runs N]
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np

import model
import weather

DB = Path.home() / "aircraft-noise-data" / "noise.db"
DAYS = 10
RUNS = 4000
LOW_HIGH = (10, 90)             # percentiles for the published range
DAYTIME = range(7, 22)          # local hours the wind and weather summary describes
SEED = 1                        # same inputs, same numbers: reruns don't jitter the site


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out")
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--runs", type=int, default=RUNS)
    args = parser.parse_args()

    now = int(time.time()) // model.HOUR_S * model.HOUR_S     # start of the current hour
    hist = model.history(args.db, now)
    label = model.labels(hist)
    # Wind for each past hour looks a few hours ahead, so fetch a little past now.
    grid = np.arange(hist["ts"][0], now + model.LOOKAHEAD_H * model.HOUR_S, model.HOUR_S)
    hours, tail = weather.analysis(int(grid[0]), int(grid[-1]) + model.HOUR_S)
    tail = model.fill(np.interp(grid, hours, tail, left=np.nan, right=np.nan))
    tail = model.lookahead(tail)[:len(hist["ts"])]
    params, cov, gamma, _ = model.fit_chain(label, tail)
    means = model.count_means(hist, gamma)
    spread = model.day_spread(hist, gamma, means)
    # Where Heathrow is now: the chain's belief at the last full hour, labels and wind so far.
    _, _, _, start = model.forward_backward(params, label, tail)

    midnight = model.today_start(now)
    end = model.today_start(midnight + (DAYS + 0.5) * 86400)  # local midnight after day 10
    ens_hours, ens_tail = weather.ensemble(DAYS + 2)
    future = np.arange(now, end, model.HOUR_S, dtype=np.int64)
    tails = model.lookahead([np.interp(future, ens_hours, m, right=np.nan) for m in ens_tail])
    rng = np.random.default_rng(SEED)
    per_hour = model.simulate(params, cov, start, tails, means, spread, future, args.runs, rng)
    today = model.local_date([now])[0]
    so_far = int(hist["passes"][hist["ts"] >= midnight].sum())
    dates, totals = model.daily(future, per_hour, (today, so_far))

    det_hours, speed, direction, code = weather.deterministic(DAYS + 1)
    det_dates = model.local_date(det_hours)
    daytime = np.isin(model.local_hour(det_hours), DAYTIME)
    days = []
    for i, date in enumerate(dates[:DAYS]):
        pick = (det_dates == date) & daytime
        wind, words = weather.summary(speed[pick], direction[pick], code[pick])
        low, mid, high = np.percentile(totals[:, i], [LOW_HIGH[0], 50, LOW_HIGH[1]])
        days.append({"date": date, "passes": int(round(mid)), "low": int(round(low)),
                     "high": int(round(high)), "wind": wind, "weather": words})

    Path(args.out).write_text(json.dumps({"generated": today, "days": days}) + "\n")
    state = "west" if start[model.WEST] > 0.5 else "east"
    print(f"Outlook for {dates[0]}..{dates[DAYS - 1]}: now {state} "
          f"(P west {start[model.WEST]:.2f}); " + ", ".join(str(d["passes"]) for d in days))


if __name__ == "__main__":
    main()
