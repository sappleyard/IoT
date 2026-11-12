"""Turn the collected database into labelled training windows for the aircraft classifier.

    python3 model/dataset.py            # copy the latest database from the Pi, then build
    python3 model/dataset.py --local    # use the copy already in model/data/noise.db

Writes model/data/windows.npz (git-ignored). Runs on the Mac (numpy only).

Each example is one mic's last WINDOW_S seconds: 9 channels (LAeq, LAmax, 7 octave bands)
x WINDOW_S steps, with that mic's background (the median of the BG_MINUTES before the window)
subtracted, so every value is "dB above the usual level". The model sees only these numbers:
no ADS-B, no time of day.

Labels come from ADS-B, never from the window's own sound (a "loud = aircraft" label would teach
the model that street traffic is aircraft too). Instead of fixed distances, each aircraft gets an
expected audibility from audibility.py: a model, fitted to this house's measurements,
of how far its sound stands out from aircraft-free moments given its distance, direction, type
and the time of day (0.5 = no different from no aircraft, 1 = always louder). For each mic:
  1 (aircraft)    the window ends while the sound of a track with audibility >= AUDIBLE is
                  arriving: from POS_BEFORE s before to POS_AFTER s after closest + slant / 343 m/s
  0 (no aircraft) every aircraft within MAX_KM for NEG_S s either side has audibility
                  <= INAUDIBLE, none is within MIN_NEG_KM, and the ADS-B logger was running
                  (outside its coverage "no aircraft" means nothing)
  everything in between (faintly audible aircraft, logger gaps) is ambiguous and left out.
Windows with a gap over FILL_GAP_S seconds, a mic fault or no background are left out too.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def find_sibling(folder, marker):
    """A sibling project folder (announcer/), wherever it sits above this one."""
    for parent in HERE.parents:
        if (parent / folder / marker).exists():
            return parent / folder
    raise SystemExit(f"Can't find {folder}/{marker} in any folder above {HERE}")


sys.path.insert(0, str(find_sibling("announcer", "aircraft_announcer")))
from database import DB, fetch  # noqa: E402
from audibility import (MAX_KM, HELI, load_model, load_positions, make_tracks,  # noqa: E402
                        predict)
# The window and background code the live announcer uses too, so training matches it exactly.
from aircraft_announcer.features import (BG_MINUTES, CHANNELS, WINDOW_S,  # noqa: E402,F401
                                         minute_background, to_grid, windows)

OUT = HERE / "data" / "windows.npz"
SPEED_OF_SOUND = 343.0    # m/s

AUDIBLE = 0.8     # tracks at least this audible make "aircraft" examples
INAUDIBLE = 0.6    # "no aircraft": every aircraft at most this audible...
MIN_NEG_KM = 3.0   # ...and none closer than 3 km (the audibility score uses the overall level only,
                   # so close in it can miss aircraft a person, or the octave bands, would notice)
POS_BEFORE = 5     # label windows ending from 5 s before the sound's arrival...
POS_AFTER = 15     # ...to 15 s after it
POS_STRIDE = 2     # keep every 2nd positive second (neighbours are nearly identical)
NEG_S = 45         # "no aircraft" must hold for 45 s either side
NEG_STRIDE = 10    # keep every 10th quiet second


def load(db):
    """Levels per mic, aircraft positions and tracks (with audibility per mic), logger coverage."""
    cols = ", ".join(f"{c} + cal_db" for c in CHANNELS)
    mics = {}
    for (mic,) in db.execute("SELECT DISTINCT mic FROM levels ORDER BY mic"):
        rows = np.array(db.execute(
            f"SELECT ts, {cols}, flags FROM levels WHERE mic = ? AND cal_db IS NOT NULL "
            "ORDER BY ts", (mic,)).fetchall(), dtype=float).reshape(-1, len(CHANNELS) + 2)
        if len(rows):
            mics[mic] = rows
    coverage = np.array(db.execute("SELECT start_ts, end_ts FROM logger_coverage "
                                   "ORDER BY start_ts").fetchall(), dtype=float).reshape(-1, 2)
    model = load_model()
    pos = load_positions(db, MAX_KM)
    tracks = make_tracks(pos)
    for mic in mics:
        m = model["mics"][mic]
        pos[f"aud_{mic}"] = predict(m, pos["slant"], pos["north"], pos["east"], pos["cat"], pos["ts"])
        tracks[f"aud_{mic}"] = predict(m, tracks["slant"], tracks["north"], tracks["east"],
                                       tracks["cat"], tracks["ts"])
    return mics, tracks, coverage, pos


def quiet(times, near_times, coverage):
    """True where no near_times fall within NEG_S s either side, while ADS-B was logging."""
    i = np.searchsorted(near_times, times - NEG_S)
    no_aircraft = (i == len(near_times)) | (near_times[np.minimum(i, len(near_times) - 1)]
                                            > times + NEG_S)
    if not len(coverage):
        return np.zeros(len(times), bool)
    c = np.searchsorted(coverage[:, 0], times - NEG_S, "right") - 1
    covered = (c >= 0) & (coverage[np.maximum(c, 0), 1] >= times + NEG_S)
    return no_aircraft & covered


def local_days_hours(ts):
    """Local date (YYYY-MM-DD) and hour of each timestamp, clock changes included."""
    hours, idx = np.unique((ts // 3600) * 3600, return_inverse=True)
    lt = [time.localtime(h) for h in hours]
    days = np.array([time.strftime("%Y-%m-%d", t) for t in lt])[idx]
    return days, np.array([t.tm_hour for t in lt])[idx]


def build(mics, tracks, coverage, pos):
    out = {k: [] for k in ("x", "y", "mic", "t", "slant", "heli", "pass_id", "audibility")}
    offsets = np.arange(-POS_BEFORE, POS_AFTER + 1, POS_STRIDE)
    for mic_i, (name, rows) in enumerate(sorted(mics.items())):
        heard = np.flatnonzero(tracks[f"aud_{name}"] >= AUDIBLE)
        passes = np.column_stack([tracks["ts"][heard], tracks["slant"][heard],
                                  tracks["cat"][heard] == HELI, tracks[f"aud_{name}"][heard]])
        arrivals = passes[:, 0] + passes[:, 1] * 1000 / SPEED_OF_SOUND
        # Times something possibly audible was around: any aircraft above INAUDIBLE or within
        # MIN_NEG_KM. The rest of the time (while ADS-B was logging) nothing audible was.
        near_times = pos["ts"][(pos[f"aud_{name}"] > INAUDIBLE) | (pos["slant"] < MIN_NEG_KM)]
        t0, grid = to_grid(rows)
        bg = minute_background(grid)
        n = len(grid)

        # Window ends: positives around each arrival, negatives at quiet seconds.
        pos_end = (np.round(arrivals - t0).astype(int)[:, None] + offsets).ravel()
        pos_pass = np.repeat(np.arange(len(passes)), len(offsets))
        seconds = np.arange(WINDOW_S - 1, n, NEG_STRIDE)
        neg_end = seconds[quiet(t0 + seconds.astype(float), near_times, coverage)]
        ends = np.concatenate([pos_end, neg_end])
        labels = np.concatenate([np.ones(len(pos_end)), np.zeros(len(neg_end))])
        pass_id = np.concatenate([pos_pass, np.full(len(neg_end), -1)])
        ok = (ends >= WINDOW_S - 1) & (ends < n)
        ends, labels, pass_id = ends[ok], labels[ok], pass_id[ok]

        # Window [end - WINDOW_S + 1, end], minus the background before it starts.
        x = windows(grid, bg, ends)   # (N, 9, W)
        good = ~np.isnan(x).any(axis=(1, 2))
        print(f"Mic {name}: {int(labels[good].sum())} aircraft windows, "
              f"{int((labels[good] == 0).sum())} quiet windows "
              f"({int((~good).sum())} dropped for gaps, faults or no background)")

        sel = pass_id[good]
        out["x"].append(x[good].astype(np.float32))
        out["y"].append(labels[good].astype(np.int8))
        out["mic"].append(np.full(good.sum(), mic_i, np.int8))
        out["t"].append(t0 + ends[good].astype(float))
        out["slant"].append(np.where(sel >= 0, passes[np.maximum(sel, 0), 1], np.nan))
        out["heli"].append(np.where(sel >= 0, passes[np.maximum(sel, 0), 2], 0).astype(np.int8))
        out["pass_id"].append(np.where(sel >= 0, heard[np.maximum(sel, 0)], -1).astype(np.int32))
        out["audibility"].append(np.where(sel >= 0, passes[np.maximum(sel, 0), 3], np.nan))
    data = {k: np.concatenate(v) for k, v in out.items()}
    data["day"], data["hour"] = local_days_hours(data["t"])
    data["mic_names"] = np.array(sorted(mics))
    data["channels"] = np.array(CHANNELS)
    data["neg_stride"] = np.array(NEG_STRIDE)
    return data


def describe(d):
    """Per-day counts, and the day/night balance (a classifier mustn't just learn the clock)."""
    print(f"\n{'day':<12}{'aircraft':>10}{'quiet':>10}")
    for day in np.unique(d["day"]):
        s = d["day"] == day
        print(f"{day:<12}{int(d['y'][s].sum()):>10}{int((d['y'][s] == 0).sum()):>10}")
    night = (d["hour"] < 7) | (d["hour"] >= 23)
    for label, name in [(1, "aircraft"), (0, "quiet")]:
        s = d["y"] == label
        print(f"{name} windows at night (23:00-07:00): {100 * night[s].mean():.0f}%")
    print("If quiet windows are mostly at night and aircraft ones by day, also train with "
          "--hours 7-23 to check the model isn't just telling day from night.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="use the existing copy, don't fetch")
    parser.add_argument("--db", type=Path, default=DB, help=f"database (default: {DB})")
    args = parser.parse_args()
    assert INAUDIBLE < AUDIBLE, "quiet must exclude every aircraft that counts as a positive"
    if not args.local:
        fetch()
    data = build(*load(sqlite3.connect(args.db)))
    OUT.parent.mkdir(exist_ok=True)
    np.savez_compressed(OUT, **data)
    describe(data)
    print(f"\n{len(data['y'])} windows of {len(CHANNELS)} x {WINDOW_S} s -> {OUT}")


if __name__ == "__main__":
    main()
