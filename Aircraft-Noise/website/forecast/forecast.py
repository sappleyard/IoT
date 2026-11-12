#!/usr/bin/env python3
"""Write outlook.json: passes over the house for each of the next 10 days, how sure, and why.

Runs on the Pi from publish.sh, after export.py. The model is in model.py; Heathrow's wind
comes from Open-Meteo (weather.py), asked for Heathrow's coordinates only.

Each run's day is one of three kinds: west (Heathrow lands westerly nearly all day, arrivals
overhead), east (nearly all easterly) or switch (it turns round during the day). The kinds give
very different counts, so a median across all runs can land between them on a day that won't
happen. Instead the outlook names the most likely kind:
  passes      the median count of the runs of that kind
  confidence  the share of runs of that kind, as a percentage
  reason      one of REASONS, from the kind, the confidence and how much the weather
              ensemble's members disagree about the wind
Also: low and high (10th and 90th percentiles of all runs, for anyone who wants the spread),
wind and weather in a few words. Nothing else: no times, positions, distances or altitudes,
so the file says nothing about where the house is.

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
SETTLED_SHARE = 0.8             # a run's day is west (east) if at least this share of its
                                # daytime hours (model.LABEL_HOURS) are west (east)
UNSURE = 60                     # below this confidence (%) the reason says why it's unsure
SPREAD_KT = 4.0                 # ensemble members disagreeing by more than this (sd of the
                                # daytime tailwind, knots) means the weather itself is unsure
REASONS = {"west": "Westerly wind, arrivals overhead",
           "east": "Easterly wind, arrivals elsewhere",
           "switch": "Wind turning during the day",
           "edge": "Wind near the turning point, could go either way",
           "far": "Far ahead, forecast uncertain"}
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
    per_hour, path, member = model.simulate(params, cov, start, tails, means, spread, future,
                                            args.runs, rng, states=True)
    today = model.local_date([now])[0]
    so_far = int(hist["passes"][hist["ts"] >= midnight].sum())
    dates, totals = model.daily(future, per_hour, (today, so_far))

    det_hours, speed, direction, code = weather.deterministic(DAYS + 1)
    det_dates = model.local_date(det_hours)
    daytime = np.isin(model.local_hour(det_hours), DAYTIME)
    # Today's daytime hours already gone count with the direction the chain thinks they had.
    past = (hist["ts"] >= midnight) & np.isin(model.local_hour(hist["ts"]), model.LABEL_HOURS)
    past_west = int((gamma[past, model.WEST] > 0.5).sum())
    future_dates = model.local_date(future)
    labelled = np.isin(model.local_hour(future), model.LABEL_HOURS)
    days = []
    for i, date in enumerate(dates[:DAYS]):
        pick = (det_dates == date) & daytime
        wind, words = weather.summary(speed[pick], direction[pick], code[pick])
        low, high = np.percentile(totals[:, i], LOW_HIGH)
        hours = (future_dates == date) & labelled
        west = (path[:, hours] == model.WEST).sum(axis=1)
        n = hours.sum()
        if date == today:
            west, n = west + past_west, n + past.sum()
        share = west / max(n, 1)
        kind = np.where(share >= SETTLED_SHARE, "west",
                        np.where(share <= 1 - SETTLED_SHARE, "east", "switch"))
        kinds, counts = np.unique(kind, return_counts=True)
        top = kinds[counts.argmax()]
        confidence = int(round(100 * counts.max() / len(kind)))
        # how much the ensemble disagrees about the day's wind
        disagree = np.nanstd(np.nanmean(tails[:, hours], axis=1)) if n else 0.0
        reason = (("far" if disagree > SPREAD_KT else "edge") if confidence < UNSURE
                  else str(top))
        days.append({"date": date, "passes": int(round(np.median(totals[kind == top, i]))),
                     "confidence": confidence, "reason": REASONS[reason], "kind": str(top),
                     "low": int(round(low)), "high": int(round(high)),
                     "wind": wind, "weather": words})
        print(f"  {date}: {top} {confidence}% (ensemble sd {disagree:.1f} kt) -> {reason}")

    Path(args.out).write_text(json.dumps({"generated": today, "days": days}) + "\n")
    state = "west" if start[model.WEST] > 0.5 else "east"
    print(f"Outlook for {dates[0]}..{dates[DAYS - 1]}: now {state} "
          f"(P west {start[model.WEST]:.2f}); " + ", ".join(str(d["passes"]) for d in days))


if __name__ == "__main__":
    main()
