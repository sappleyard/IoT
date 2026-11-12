#!/usr/bin/env python3
"""Backtest the outlook day by day over the logged history, against two simple baselines.

For each past day D it makes the forecast that would have been made at 01:00 that morning:
the chain is fitted on hours before then only, starts from the direction known then, and runs
on wind as it was forecast at the time (Open-Meteo's previous runs: a target day L days ahead
uses the run from L+1 days before it, so nothing comes from after the forecast was made).
Earlier runs are kept for 7 days, so leads 0-6 are tested; the published days 7-9 are not.

There is no archive of past ensembles, so the runs here are spread over a few models' earlier
forecasts (weather.BACKTEST_MODELS) instead of the 51 ECMWF members used live. Those few agree
more than the live ensemble does, so the backtest is run twice: on the models' runs alone, and
on copies of each with random wind errors added, growing with lead (WIND_ERROR_KT, roughly the
size ECMWF's earlier runs missed by over this period), to stand in for an ensemble's spread.

Baselines: persistence (every day ahead = the last full day) and climatology (the mean of all
full days, which has seen the answers, so it flatters that baseline).

    backtest.py [--db PATH] [--runs N]
"""

import argparse
import time

import numpy as np

import model
import weather
from forecast import DB, LOW_HIGH

LEADS = weather.MAX_PREVIOUS_DAY        # days 0..6 ahead
FIRST_ISSUE_DAYS = 1                    # need one full day of history before the first forecast
ISSUE_HOUR = 1                          # local hour the daily forecast is made (cron is 01:30)
RUNS = 2000
WIND_ERROR_KT = (0.5, 0.5)              # sd of added wind error: 0.5 kt + 0.5 kt per day ahead
WIND_ERROR_HOURS = 12                   # how long an error lasts (AR(1) time constant)
WARM_FROM = "2026-11-01"                # first forecast made after an easterly day was logged
COPIES = 10                             # perturbed copies of each model's run


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--runs", type=int, default=RUNS)
    args = parser.parse_args()

    now = int(time.time()) // model.HOUR_S * model.HOUR_S
    hist = model.history(args.db, now)
    label = model.labels(hist)
    # Wind for each past hour looks a few hours ahead, so fetch a little past now.
    grid = np.arange(hist["ts"][0], now + model.LOOKAHEAD_H * model.HOUR_S, model.HOUR_S)
    hours, tail = weather.analysis(int(grid[0]), int(grid[-1]) + model.HOUR_S)
    tail = model.fill(np.interp(grid, hours, tail, left=np.nan, right=np.nan))
    tail = model.lookahead(tail)[:len(hist["ts"])]
    prev_hours, prev = weather.previous_runs(int(hist["ts"][0]), now)

    # Actual passes per full local day (today is not full yet; the first day is partial).
    dates = model.local_date(hist["ts"])
    full_days = sorted(set(dates))[1:-1]
    actual = {d: int(hist["passes"][dates == d].sum()) for d in full_days}
    climatology = np.mean(list(actual.values()))

    for perturb in (False, True):
        print("\nRuns on the models' earlier forecasts"
              + (" plus random wind errors:" if perturb else " alone:"))
        rows = backtest(hist, label, tail, prev_hours, prev, dates, full_days, actual,
                        args.runs, perturb)
        report(rows, climatology, show_days=perturb)


def wind_members(prev_hours, prev, future, future_dates, targets, perturb, rng):
    """Tailwind members for the future hours: each model's run from lead+1 days before."""
    lead_of_hour = np.zeros(len(future), dtype=int)
    for lead, day in enumerate(targets):
        lead_of_hour[future_dates == day] = lead
    members = []
    for runs in prev.values():
        series = np.array([np.interp(future[i], prev_hours, runs[lead_of_hour[i]])
                           for i in range(len(future))])
        if not perturb:
            members.append(series)
            continue
        rho = np.exp(-1 / WIND_ERROR_HOURS)
        sd = WIND_ERROR_KT[0] + WIND_ERROR_KT[1] * lead_of_hour
        for _ in range(COPIES):
            noise = np.empty(len(future))
            noise[0] = rng.normal()
            for t in range(1, len(future)):
                noise[t] = rho * noise[t - 1] + np.sqrt(1 - rho ** 2) * rng.normal()
            members.append(series + sd * noise)
    return model.lookahead(members)


def backtest(hist, label, tail, prev_hours, prev, dates, full_days, actual, runs, perturb):
    """One forecast per past day, as made at ISSUE_HOUR that morning, scored on full days."""
    rng = np.random.default_rng(1)
    rows = []           # (lead, actual, median, low, high, persistence, issued, day)
    for i, issue_date in enumerate(full_days[FIRST_ISSUE_DAYS:], FIRST_ISSUE_DAYS):
        midnight = int(hist["ts"][dates == issue_date][0])
        issue = midnight + ISSUE_HOUR * model.HOUR_S
        past = hist["ts"] < issue
        sub = {k: v[past] for k, v in hist.items()}
        params, cov, gamma, _ = model.fit_chain(label[past], tail[past])
        means = model.count_means(sub, gamma)
        spread = model.day_spread(sub, gamma, means)
        _, _, _, start = model.forward_backward(params, label[past], tail[past])

        targets = full_days[i:i + LEADS]
        future = hist["ts"][(hist["ts"] >= issue) & np.isin(dates, targets)]
        members = wind_members(prev_hours, prev, future, model.local_date(future), targets,
                               perturb, rng)
        per_hour = model.simulate(params, cov, start, members, means, spread, future, runs, rng)
        so_far = int(hist["passes"][(hist["ts"] >= midnight) & (hist["ts"] < issue)].sum())
        days, totals = model.daily(future, per_hour, (issue_date, so_far))
        persistence = actual[full_days[i - 1]]
        for lead, day in enumerate(days):
            low, mid, high = np.percentile(totals[:, lead], [LOW_HIGH[0], 50, LOW_HIGH[1]])
            rows.append((lead, actual[day], mid, low, high, persistence, issue_date, day))
    return rows


def report(rows, climatology, show_days):
    print(f"Climatology (mean full day): {climatology:.0f} passes")
    table(rows, climatology)
    later = [r for r in rows if r[6] >= WARM_FROM]
    print(f"Forecasts made from {WARM_FROM}, once both directions had been seen:")
    table(later, climatology)
    if show_days:
        days(rows)


def table(rows, climatology):
    lead, actual, mid, low, high, persistence = np.array([r[:6] for r in rows]).T
    print("lead  n   MAE model  persistence  climatology   inside low-high")
    for L in sorted(set(lead.astype(int))) + [None]:
        pick = np.ones(len(lead), bool) if L is None else lead == L
        inside = np.mean((actual[pick] >= low[pick]) & (actual[pick] <= high[pick]))
        print(f"{'all' if L is None else L:>4} {pick.sum():3d} "
              f"{np.mean(abs(mid[pick] - actual[pick])):8.0f} "
              f"{np.mean(abs(persistence[pick] - actual[pick])):12.0f} "
              f"{np.mean(abs(climatology - actual[pick])):12.0f} "
              f"{inside:14.0%}")


def days(rows):
    print("\nSame-day and next-day forecasts (lead 0 is the day the forecast was made):")
    for r in rows:
        if r[0] in (0, 1):
            print(f"  made {r[6]} for {r[7]} (lead {r[0]}): {r[2]:4.0f} [{r[3]:4.0f}-{r[4]:4.0f}]"
                  f"  actual {r[1]:4d}  persistence {r[5]:4d}")


if __name__ == "__main__":
    main()
