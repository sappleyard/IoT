"""The outlook's model: which way Heathrow lands, hour by hour, and how many passes that brings.

Passes over the house are bimodal. On westerly operations Heathrow's arrival stream comes in
from the east over the house, heading west (track about 270), and gives about 650 passes a day;
on easterly operations almost none of it does, and about 30 pass. So the forecast is mostly a
forecast of runway direction, done in three parts:

  1. labels: each past daytime hour is west, east or unknown, from how many of its passes were
     heading west. Night hours carry no label: there are hardly any arrivals to tell from.
  2. a two-state Markov chain (west, east) with hourly switching chances that depend on the
     tailwind a westerly landing would have, averaged over the next few hours: Heathrow plans
     a change on the wind it expects, and this fits the history far better than the wind of
     the hour itself or of the hours before. It's fitted by EM, so unlabelled night hours are
     filled in from the hours either side, and each M-step is a small logistic regression with
     a Gaussian prior, because three weeks of data hold only a handful of switches.
  3. counts: the mean passes for each local hour of the day in each direction.

The simulation then runs the chain forward many times, each run on one ensemble member's wind.
"""

import sqlite3
import time
from datetime import datetime

import numpy as np

WEST, EAST = 0, 1
HOUR_S = 3600
LABEL_HOURS = range(6, 22)      # local hours when westerly arrivals never stop (06:00-21:59)
WEST_TRACK = (225, 315)         # headings of the westerly arrival stream
WEST_MIN = 10                   # westbound passes in an hour that mean "west"
EAST_MAX = 2                    # at most this many mean "east"; between is left unknown
MIN_COVERAGE = 0.9              # fraction of an hour the logger must have run to use it
LABEL_ERROR = 0.002             # chance a label is wrong, so one odd hour can't break the fit

# Switching chance per hour: from west, sigmoid(a + b * tailwind); from east, sigmoid(a - b *
# tailwind). The priors encode Heathrow's rule (go easterly once the tailwind on 27 passes a
# few knots, with a preference for westerly) loosely enough for the data to move them.
PRIOR_MEAN = {WEST: np.array([-6.0, 1.0]), EAST: np.array([-4.0, 1.0])}
PRIOR_SD = np.array([2.0, 0.5])
EM_ITERATIONS = 50
LOOKAHEAD_H = 6                 # switching follows the mean tailwind over the next 6 hours
# Prior passes per daytime hour: Heathrow lands about 40 an hour, and on easterlies its arrival
# stream is nowhere near; worth COUNT_PRIOR_HOURS hours of data at each hour of the day.
COUNT_PRIOR = {WEST: 40.0, EAST: 2.0}
COUNT_PRIOR_HOURS = 2
SETTLED = 0.95                  # a day counts as all one direction above this mean probability


def sigmoid(z):
    return 1 / (1 + np.exp(-z))


def local_hour(ts):
    return np.array([time.localtime(int(t)).tm_hour for t in ts])


def local_date(ts):
    return np.array([time.strftime("%Y-%m-%d", time.localtime(int(t))) for t in ts])


def history(db_path, end_ts):
    """Hourly history up to end_ts (exclusive, on an hour): passes, westbound, coverage.

    Returns a dict of arrays on whole UTC hours from the logger's first full hour.
    """
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    passes = np.array(db.execute("SELECT closest_ts, COALESCE(track_deg, -1) FROM passes "
                                 "WHERE closest_ts < ?", (end_ts,)).fetchall(), dtype=float)
    spans = db.execute("SELECT start_ts, end_ts FROM logger_coverage ORDER BY start_ts").fetchall()
    db.close()

    start = int(spans[0][0]) // HOUR_S * HOUR_S + HOUR_S
    ts = np.arange(start, end_ts, HOUR_S, dtype=np.int64)
    covered = np.zeros(len(ts))
    for a, b in spans:
        overlap = np.minimum(b, ts + HOUR_S) - np.maximum(a, ts)
        covered += np.clip(overlap, 0, None) / HOUR_S
    idx = ((passes[:, 0] - start) // HOUR_S).astype(int)
    ok = (idx >= 0) & (idx < len(ts))
    count = np.bincount(idx[ok], minlength=len(ts))
    westbound = (passes[:, 1] >= WEST_TRACK[0]) & (passes[:, 1] <= WEST_TRACK[1])
    west = np.bincount(idx[ok & westbound], minlength=len(ts))
    return {"ts": ts, "passes": count, "west": west, "covered": covered}


def labels(hist):
    """West, east or -1 (unknown) for each hour, from its westbound passes."""
    hour = local_hour(hist["ts"])
    out = np.full(len(hist["ts"]), -1)
    day = np.isin(hour, LABEL_HOURS) & (hist["covered"] >= MIN_COVERAGE)
    out[day & (hist["west"] >= WEST_MIN)] = WEST
    out[day & (hist["west"] <= EAST_MAX)] = EAST
    return out


def transitions(params, tail):
    """Per-hour switching chances (from west, from east), given each new hour's tailwind."""
    p_we = sigmoid(params[WEST][0] + params[WEST][1] * tail)
    p_ew = sigmoid(params[EAST][0] - params[EAST][1] * tail)
    return p_we, p_ew


def emissions(label):
    """How well each state explains each hour's label (1 for both when there's no label)."""
    e = np.ones((len(label), 2))
    for s in (WEST, EAST):
        e[label == s, s] = 1 - LABEL_ERROR
        e[label == s, 1 - s] = LABEL_ERROR
    return e


def forward_backward(params, label, tail, start=(0.5, 0.5)):
    """State probabilities per hour, and expected switches per step, given all the labels.

    Hour t+1's state comes from hour t's through the chance set by tailwind at t+1.
    Returns (gamma: hours x 2, xi: steps x 2 x 2, log-likelihood, filtered last-hour probs).
    """
    n = len(label)
    p_we, p_ew = transitions(params, tail)
    trans = np.empty((n, 2, 2))
    trans[:, WEST, WEST], trans[:, WEST, EAST] = 1 - p_we, p_we
    trans[:, EAST, EAST], trans[:, EAST, WEST] = 1 - p_ew, p_ew
    e = emissions(label)
    alpha = np.empty((n, 2))
    scale = np.empty(n)
    a = np.asarray(start, dtype=float) * e[0]
    for t in range(n):
        if t:
            a = (alpha[t - 1] @ trans[t]) * e[t]
        scale[t] = a.sum()
        alpha[t] = a / scale[t]
    beta = np.ones((n, 2))
    for t in range(n - 2, -1, -1):
        b = trans[t + 1] @ (e[t + 1] * beta[t + 1])
        beta[t] = b / scale[t + 1]
    gamma = alpha * beta
    gamma /= gamma.sum(axis=1, keepdims=True)
    xi = (alpha[:-1, :, None] * trans[1:] * (e[1:] * beta[1:])[:, None, :]
          / scale[1:, None, None])
    return gamma, xi, np.log(scale).sum(), alpha[-1]


def fit_logistic(x, switched, stayed, mean, sd):
    """MAP fit of sigmoid(a + b x) to expected switch/stay counts, with a Gaussian prior."""
    w = mean.copy()
    design = np.column_stack([np.ones_like(x), x])
    precision = 1 / sd ** 2
    for _ in range(25):
        p = sigmoid(design @ w)
        grad = design.T @ (switched - (switched + stayed) * p) - precision * (w - mean)
        hess = (design.T * ((switched + stayed) * p * (1 - p))) @ design + np.diag(precision)
        step = np.linalg.solve(hess, grad)
        w += step
        if np.abs(step).max() < 1e-6:
            break
    return w, np.linalg.inv(hess)


def fit_chain(label, tail):
    """EM fit of the switching model.

    Returns (params, cov, gamma, log-likelihood): cov is each fit's uncertainty, which is
    large with so few switches, so the simulation draws parameters from it rather than
    trusting the best fit.
    """
    params = {s: PRIOR_MEAN[s].copy() for s in (WEST, EAST)}
    x = tail[1:]
    for _ in range(EM_ITERATIONS):
        gamma, xi, loglik, _ = forward_backward(params, label, tail)
        fits = {WEST: fit_logistic(x, xi[:, WEST, EAST], xi[:, WEST, WEST],
                                   PRIOR_MEAN[WEST], PRIOR_SD),
                EAST: fit_logistic(-x, xi[:, EAST, WEST], xi[:, EAST, EAST],
                                   PRIOR_MEAN[EAST], PRIOR_SD)}
        new = {s: fits[s][0] for s in fits}
        cov = {s: fits[s][1] for s in fits}
        done = all(np.abs(new[s] - params[s]).max() < 1e-4 for s in (WEST, EAST))
        params = new
        if done:
            break
    return params, cov, gamma, loglik


def count_means(hist, gamma):
    """Mean passes per local hour of day in each direction: array 2 x 24.

    Hours are weighted by how likely each direction was, so night hours count towards the
    direction the chain says they were in. A couple of hours' worth of prior (COUNT_PRIOR)
    stands in until a direction has been seen at that hour, so an early forecast can't take
    westerly counts for easterly ones.
    """
    use = hist["covered"] >= MIN_COVERAGE
    hour = local_hour(hist["ts"])[use]
    weights = gamma[use]
    counts = hist["passes"][use]
    daytime = np.isin(np.arange(24), LABEL_HOURS)
    means = np.zeros((2, 24))
    for s in (WEST, EAST):
        prior = np.where(daytime, COUNT_PRIOR[s], 0.0)
        total = np.bincount(hour, weights=weights[:, s] * counts, minlength=24)
        weight = np.bincount(hour, weights=weights[:, s], minlength=24)
        means[s] = (total + COUNT_PRIOR_HOURS * prior) / (weight + COUNT_PRIOR_HOURS)
    return means


def day_spread(hist, gamma, means):
    """How much whole days vary beyond chance, per direction: sd of log(actual / expected).

    Westerly days are nearly as steady as pure chance allows; easterly days vary more (their
    few passes are other traffic, which comes and goes with the weather). Only full days spent
    in one direction are used, and the part chance (Poisson) explains is taken off.
    """
    dates = local_date(hist["ts"])
    hour = local_hour(hist["ts"])
    spread = np.zeros(2)
    for s in (WEST, EAST):
        logs, chance = [], []
        for d in sorted(set(dates))[1:]:
            k = dates == d
            full = k.sum() >= 23 and (hist["covered"][k] >= MIN_COVERAGE).all()
            if not full or gamma[k, s].mean() < SETTLED:
                continue
            expected = means[s, hour[k]].sum()
            logs.append(np.log(max(hist["passes"][k].sum(), 1) / expected))
            chance.append(1 / expected)
        if len(logs) >= 2:
            spread[s] = np.sqrt(max(np.var(logs, ddof=1) - np.mean(chance), 0))
    return spread


def simulate(params, cov, start_probs, tails, means, spread, hours, runs, rng):
    """Many runs of the chain: passes per hour and direction per hour, both runs x hours.

    tails is members x hours of tailwind; each run follows one member, chosen at random, and
    its own draw of the switching parameters, so the spread covers the weather, Heathrow's
    switching and how little the fit knows. Each run's days are also scaled by a random
    factor (spread) for the day-to-day variation in traffic.
    """
    member = rng.integers(0, len(tails), runs)
    draw = {s: rng.multivariate_normal(params[s], cov[s], runs) for s in (WEST, EAST)}
    state = (rng.random(runs) < start_probs[EAST]).astype(int)
    hour = local_hour(hours)
    _, day = np.unique(local_date(hours), return_inverse=True)
    factor = np.exp(rng.normal(size=(runs, day.max() + 1, 2)) * spread)
    out = np.empty((runs, len(hours)))
    path = np.empty((runs, len(hours)), np.int8)
    for t in range(len(hours)):
        tail = tails[member, t]
        tail = np.where(np.isnan(tail), 0.0, tail)
        p_we = sigmoid(draw[WEST][:, 0] + draw[WEST][:, 1] * tail)
        p_ew = sigmoid(draw[EAST][:, 0] - draw[EAST][:, 1] * tail)
        switch = rng.random(runs) < np.where(state == WEST, p_we, p_ew)
        state = np.where(switch, 1 - state, state)
        rate = means[state, hour[t]] * factor[np.arange(runs), day[t], state]
        out[:, t] = rng.poisson(rate)
        path[:, t] = state
    return out, path


def daily(hours, per_hour, observed_today=None):
    """Totals per local date: (dates, runs x days). observed_today adds passes already logged."""
    dates = local_date(hours)
    days = sorted(set(dates))
    totals = np.column_stack([per_hour[:, dates == d].sum(axis=1) for d in days])
    if observed_today:
        date, count = observed_today
        if date in days:
            totals[:, days.index(date)] += count
    return days, totals


def lookahead(tail, hours=LOOKAHEAD_H):
    """Mean of each hour's tailwind and the next hours' (fewer at the end), along the last axis."""
    tail = np.asarray(tail, dtype=float)
    ok = ~np.isnan(tail)
    pad = [(0, 0)] * (tail.ndim - 1) + [(1, 0)]
    total = np.pad(np.cumsum(np.where(ok, tail, 0), axis=-1), pad)
    count = np.pad(np.cumsum(ok, axis=-1), pad)
    n = tail.shape[-1]
    stop = np.minimum(np.arange(n) + hours, n)
    sums = total[..., stop] - total[..., :n]
    counts = count[..., stop] - count[..., :n]
    return np.where(counts > 0, sums / np.maximum(counts, 1), np.nan)


def fill(tail):
    """Carry the last known tailwind into any gaps (the API rarely leaves one)."""
    tail = np.array(tail, dtype=float)
    last = 0.0
    for i, v in enumerate(tail):
        if np.isnan(v):
            tail[i] = last
        else:
            last = v
    return tail


def today_start(now):
    """The Unix time of local midnight today."""
    return int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0,
                                                   microsecond=0).timestamp())
