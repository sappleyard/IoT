"""How audible is an aircraft? Measure it against distance, direction, type and time of day, and
fit a model that predicts it from ADS-B alone (model/dataset.py uses it to label windows).

    python3 analysis/audibility.py            # copy the latest database from the Pi, then analyse
    python3 analysis/audibility.py --local    # use the copy already in analysis/data/

Writes analysis/data/audibility.json (the fitted model), figures/5_audibility.png and
figures/audibility.txt. All git-ignored: the direction terms give away roughly where the house is.

Method
  * Every airborne track within MAX_KM (from aircraft_positions, which reaches 60 km, not just the
    5 km passes table) is reduced to its closest approach: time, slant distance, bearing from the
    house, emitter category.
  * Its sound reaches the mic slant / 343 m/s later. The rise there is the peak 5 s LAeq in
    [-10, +15] s minus the background LA90 (the level exceeded 90% of the time, ISO 1996) of the
    3 minutes before. A 5 s mean ignores door slams; LA90 ignores the previous aircraft, which a
    median doesn't when Heathrow arrivals come every 90 s.
  * A rise alone can't say "heard": at 3 am, street traffic gives 15+ dB rises in about 15% of
    aircraft-free minutes. So each rise is compared with the null, the same statistic at random
    aircraft-free times of the same period (day 07-23 or night). The track's score is the share of
    null rises it beats: 0.5 on average means indistinguishable from no aircraft, 1.0 always louder.
    Averaged over a group of tracks that's the ROC AUC of "aircraft vs nothing" by level.
  * A fractional logistic regression predicts the score from ADS-B and the clock alone, so
    dataset.py can say, for any aircraft at any moment, how audible it is expected to be.
Only "isolated" tracks (no other aircraft within 1.5x their distance for +-45 s) are used to fit.
The score uses only the overall level, so it's a lower bound on what a person (or the classifier,
which also sees the octave bands) can hear.
"""

import argparse
import json
import sqlite3
import time
import warnings

import matplotlib
import numpy as np

from summary import DB, FIGURES, SPEED_OF_SOUND, fetch

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

MODEL_FILE = DB.parent / "audibility.json"
MAX_KM = 20.0        # tracks further than this are treated as inaudible
GAP_S = 300          # a track splits after 5 min without positions
ISOLATION, ISO_S = 1.5, 45
NULL_KM = {"day": 8.0, "night": 15.0}   # null: nothing within this for +-NULL_S (day is busier)
NULL_S = 45
DAY_HOURS = (7, 23)  # local
CATEGORIES = {"A1": 1, "A2": 2, "A3": 3, "A4": 4, "A5": 5, "A7": 7}
HEAVY, HELI = 5, 7
FEATURES = ["log10 slant km", "day", "day x log10 slant", "north / slant", "east / slant",
            "heavy (A5)", "helicopter (A7)"]
BINS_KM = [0, 1.2, 1.5, 3, 4, 6, 8, 10, 13, 16, 20]


# --- ADS-B: positions and tracks (also used by model/dataset.py) ----------------------------

def local_hour(ts):
    """Local hour of each timestamp (clock changes included), computed once per distinct hour."""
    hours, idx = np.unique((np.asarray(ts) // 3600) * 3600, return_inverse=True)
    return np.array([time.localtime(h).tm_hour for h in hours], dtype=int)[idx]


def is_day(ts):
    h = local_hour(ts)
    return (h >= DAY_HOURS[0]) & (h < DAY_HOURS[1])


def load_positions(db, max_km=MAX_KM):
    """Airborne positions within max_km slant, sorted by time: dict of arrays."""
    meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
    lat0, lon0 = float(meta["receiver_lat"]), float(meta["receiver_lon"])
    rx_alt, geoid_ft = float(meta.get("receiver_alt_m", 0)), float(meta.get("geoid_m", 0)) / 0.3048
    rows = db.execute(
        "SELECT ts, hex, lat, lon, dist_km, coalesce(alt_geom_ft - ?, alt_baro_ft), category "
        "FROM aircraft_positions WHERE dist_km < ? AND NOT coalesce(on_ground, 0) "
        "AND coalesce(alt_geom_ft, alt_baro_ft) IS NOT NULL "
        "AND NOT (coalesce(alt_geom_ft - ?, alt_baro_ft) < 100 AND coalesce(speed_kt, 0) < 30) "
        "ORDER BY ts", (geoid_ft, max_km, geoid_ft)).fetchall()
    ts, hexes, lat, lon, dist, alt_ft, cat = zip(*rows) if rows else ([],) * 7
    north = (np.array(lat, float) - lat0) * 111.32
    east = (np.array(lon, float) - lon0) * 111.32 * np.cos(np.radians(lat0))
    slant = np.hypot(np.array(dist, float), (np.array(alt_ft, float) * 0.3048 - rx_alt) / 1000)
    keep = slant < max_km
    _, hex_id = np.unique(np.array(hexes, dtype=str), return_inverse=True)
    pos = {"ts": np.array(ts, float), "hex": hex_id, "slant": slant, "north": north, "east": east,
           "cat": np.array([CATEGORIES.get(c, 0) for c in cat], int)}
    return {k: v[keep] for k, v in pos.items()}


def make_tracks(pos):
    """Closest approach of every track (one aircraft, no 5-minute gap), with an isolation flag."""
    o = np.lexsort((pos["ts"], pos["hex"]))
    ts, hx = pos["ts"][o], pos["hex"][o]
    new = np.r_[True, (hx[1:] != hx[:-1]) | (np.diff(ts) > GAP_S)]
    track = np.cumsum(new) - 1
    by_dist = np.lexsort((pos["slant"][o], track))
    first = np.r_[True, track[by_dist][1:] != track[by_dist][:-1]]
    best = o[by_dist[first]]
    tr = {k: v[best] for k, v in pos.items()}
    order = np.argsort(tr["ts"])
    tr = {k: v[order] for k, v in tr.items()}
    # Isolated: nobody else within ISOLATION x its slant during +-ISO_S around its closest moment.
    i = np.searchsorted(pos["ts"], tr["ts"] - ISO_S)
    j = np.searchsorted(pos["ts"], tr["ts"] + ISO_S)
    tr["isolated"] = np.array([not np.any((pos["hex"][a:b] != h) & (pos["slant"][a:b] < ISOLATION * s))
                               for a, b, h, s in zip(i, j, tr["hex"], tr["slant"])])
    return tr


def features(slant, north, east, cat, ts):
    log_r = np.log10(np.maximum(slant, 0.3))
    day = is_day(ts).astype(float)
    r = np.maximum(slant, 0.3)
    return np.column_stack([log_r, day, day * log_r, north / r / r, east / r / r,
                            cat == HEAVY, cat == HELI]).astype(float)


def predict(model, slant, north, east, cat, ts):
    """Expected audibility score (0.5 = indistinguishable from no aircraft, 1 = always louder)."""
    z = features(slant, north, east, cat, ts) @ np.array(model["coef"]) + model["intercept"]
    return 1 / (1 + np.exp(-z))


def load_model():
    if not MODEL_FILE.exists():
        raise SystemExit(f"No {MODEL_FILE}: run analysis/audibility.py first")
    return json.loads(MODEL_FILE.read_text())


# --- Sound: rises and the null ----------------------------------------------------------------

def mic_levels(db, mic):
    """(ts, LAeq with faults as NaN, 5 s centred energy-mean LAeq)."""
    ts, laeq, flags = np.array(db.execute(
        "SELECT ts, laeq + cal_db, flags FROM levels WHERE mic = ? AND cal_db IS NOT NULL "
        "ORDER BY ts", (mic,)).fetchall(), dtype=float).T
    laeq[flags > 0] = np.nan
    energy = np.nan_to_num(10 ** (laeq / 10))
    count = np.convolve(~np.isnan(laeq), np.ones(5), "same")
    smooth = 10 * np.log10(np.convolve(energy, np.ones(5), "same") / np.maximum(count, 1) + 1e-12)
    smooth[count < 4] = np.nan
    return ts, laeq, smooth


def rises(levels, t_arr):
    """Peak 5 s LAeq in [t-10, t+15] minus LA90 of [t-200, t-20]. NaN without enough data."""
    ts, laeq, smooth = levels
    out = np.full(len(t_arr), np.nan)
    i, j = np.searchsorted(ts, t_arr - 10), np.searchsorted(ts, t_arr + 15)
    k, m = np.searchsorted(ts, t_arr - 200), np.searchsorted(ts, t_arr - 20)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for n in np.flatnonzero((j - i >= 20) & (m - k >= 90)):
            out[n] = np.nanmax(smooth[i[n]:j[n]]) - np.nanpercentile(laeq[k[n]:m[n]], 10)
    return out


def null_rises(db, levels, pos, rng):
    """Rises at random aircraft-free, logger-covered seconds: {'day': array, 'night': array}."""
    coverage = np.array(db.execute("SELECT start_ts, end_ts FROM logger_coverage "
                                   "ORDER BY start_ts").fetchall(), float).reshape(-1, 2)
    t = np.sort(rng.uniform(levels[0][0] + 300, levels[0][-1] - 60, 80000))
    c = np.searchsorted(coverage[:, 0], t - NULL_S, "right") - 1
    covered = (c >= 0) & (coverage[np.maximum(c, 0), 1] >= t + NULL_S)
    day = is_day(t)
    out = {}
    for period, sel in [("day", day), ("night", ~day)]:
        near = pos["ts"][pos["slant"] < NULL_KM[period]]
        i = np.searchsorted(near, t - NULL_S)
        free = (i == len(near)) | (near[np.minimum(i, len(near) - 1)] > t + NULL_S)
        r = rises(levels, t[sel & free & covered])
        out[period] = np.sort(r[~np.isnan(r)])
    return out


def scores(r, ts, null):
    """Each rise's share of same-period null rises it beats (ties count half)."""
    s = np.full(len(r), np.nan)
    for period, sel in [("day", is_day(ts)), ("night", ~is_day(ts))]:
        n = null[period]
        sel = sel & ~np.isnan(r)
        lo, hi = np.searchsorted(n, r[sel], "left"), np.searchsorted(n, r[sel], "right")
        s[sel] = (lo + hi) / 2 / len(n)
    return s


def fit(x, s):
    """Fractional logistic regression: each track counts as score s of a 1 and 1 - s of a 0."""
    from sklearn.linear_model import LogisticRegression
    xx = np.vstack([x, x])
    yy = np.r_[np.ones(len(x)), np.zeros(len(x))]
    lr = LogisticRegression(C=1e4, max_iter=2000).fit(xx, yy, sample_weight=np.r_[s, 1 - s])
    return {"coef": lr.coef_[0].tolist(), "intercept": float(lr.intercept_[0])}


# --- Main -------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="use the existing copy, don't fetch")
    args = parser.parse_args()
    if not args.local:
        fetch()
    FIGURES.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    report = []

    def say(line=""):
        print(line)
        report.append(line)

    pos = load_positions(db)
    tr = make_tracks(pos)
    arrival = tr["ts"] + tr["slant"] * 1000 / SPEED_OF_SOUND
    x = features(tr["slant"], tr["north"], tr["east"], tr["cat"], tr["ts"])
    day = is_day(arrival)
    fixed_wing = tr["cat"] != HELI
    say(f"{len(tr['ts'])} tracks within {MAX_KM:.0f} km, {tr['isolated'].sum()} isolated "
        f"(no other aircraft within {ISOLATION}x their distance for +-{ISO_S} s)")

    rng = np.random.default_rng(0)
    model = {"features": FEATURES, "day_hours": DAY_HOURS, "max_km": MAX_KM, "mics": {}}
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), sharey=True)
    for ax, (mic,) in zip(axes, db.execute("SELECT DISTINCT mic FROM levels ORDER BY mic").fetchall()):
        levels = mic_levels(db, mic)
        null = null_rises(db, levels, pos, rng)
        s = scores(rises(levels, arrival), arrival, null)
        use = tr["isolated"] & ~np.isnan(s)
        m = fit(x[use], s[use])
        model["mics"][mic] = m
        pred = predict(m, tr["slant"], tr["north"], tr["east"], tr["cat"], tr["ts"])
        say(f"\nMic {mic}: null of {len(null['day'])} daytime / {len(null['night'])} night-time "
            f"aircraft-free moments; null rise 50/95th percentile: day "
            f"{np.percentile(null['day'], 50):.1f}/{np.percentile(null['day'], 95):.1f} dB, night "
            f"{np.percentile(null['night'], 50):.1f}/{np.percentile(null['night'], 95):.1f} dB")
        say("  Audibility score of isolated fixed-wing tracks by slant distance "
            "(measured / model, [tracks]):")
        for period, sel, colour in [("night", ~day, "midnightblue"), ("day", day, "darkorange")]:
            row, pts = [], []
            for lo, hi in zip(BINS_KM[:-1], BINS_KM[1:]):
                b = use & sel & fixed_wing & (tr["slant"] >= lo) & (tr["slant"] < hi)
                if b.sum() >= 10:
                    row.append(f"{lo:g}-{hi:g} km {s[b].mean():.2f}/{pred[b].mean():.2f} [{b.sum()}]")
                    pts.append((np.median(tr["slant"][b]), s[b].mean(), pred[b].mean(), b.sum()))
            say(f"  {period:5}  " + ", ".join(row))
            r_, a_, p_, n_ = np.array(pts).T
            ax.scatter(r_, a_, s=10 + 3 * np.sqrt(n_), color=colour, zorder=3,
                       label=f"{period}: measured (dot size = tracks)")
            ax.scatter(r_, p_, s=40, marker="x", color=colour, zorder=3,
                       label=f"{period}: model for the same tracks")
            # The direction-neutral curve: an airliner with no north/east effect.
            r = np.geomspace(1, MAX_KM, 100)
            t_rep = np.full(len(r), arrival[np.flatnonzero(sel)[0]])
            ax.plot(r, predict(m, r, 0 * r, 0 * r, np.full(len(r), 3), t_rep), color=colour,
                    lw=1, alpha=0.6, label=f"{period}: model, airliner, direction ignored")
        names = ", ".join(f"{f} {c:+.2f}" for f, c in zip(FEATURES, m["coef"]))
        say(f"  model: intercept {m['intercept']:+.2f}, {names}")
        ax.axhline(0.5, color="grey", lw=0.8, ls=":")
        ax.set_xscale("log")
        ax.set_ylim(0.3, 1.0)
        ax.set_xlabel("slant distance at closest approach (km)")
        ax.set_title(f"Mic {mic}")
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("audibility score (0.5 = like no aircraft)")
    axes[0].legend(fontsize=7)
    axes[0].annotate("the 0-1.2 km dots are the approach path south of the house,\nthe 1.2-1.5 km dots the path north of it", (1.05, 0.31), fontsize=7)
    fig.suptitle("How audible aircraft are, by distance, direction and time of day")
    fig.tight_layout()
    fig.savefig(FIGURES / "5_audibility.png", dpi=150)
    MODEL_FILE.write_text(json.dumps(model, indent=1))
    (FIGURES / "audibility.txt").write_text("\n".join(report) + "\n")
    print(f"\nModel -> {MODEL_FILE}\nFigure -> {FIGURES / '5_audibility.png'}")


if __name__ == "__main__":
    main()
