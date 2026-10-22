"""Basic time-series analysis of the collected data: figures and a text summary for the report.

    python3 analysis/summary.py            # copy the latest database from the Pi, then analyse
    python3 analysis/summary.py --local    # analyse the copy already in analysis/data/

Writes analysis/figures/*.png and summary.txt. Runs on the Mac (needs numpy and matplotlib).
Both folders are git-ignored: the database copy contains the home location. Everything works
on time-sorted arrays with np.searchsorted, so weeks of data (millions of rows) stay fast.

Sound propagation model (see the design summary): an aircraft is treated as a point source, so
its level falls 6 dB per doubling of distance (spherical spreading) plus air absorption:

    LAmax(r) = L1 - 20 log10(r / 1 km) - alpha (r - 1 km)

L1 (the level at 1 km) and alpha (dB/km) are fitted to the fixed-wing passes the mic heard
(helicopters are a different kind of source and are shown separately). The sound from a pass
reaches the mic r / 343 m/s after the aircraft's closest moment, which is where each pass's
level is read. The background's energy is subtracted first (as in ISO 1996), and the fit only
uses distances where the mic heard at least 90% of passes: further out, only the passes that
happened to be louder get heard, which would flatten the curve and hide the air absorption.
"""

import argparse
import sqlite3
import subprocess
import time
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = HERE / "data" / "noise.db"
FIGURES = HERE / "figures"
HOST = "sebpi@sebpi"
BANDS_HZ = [63, 125, 250, 500, 1000, 2000, 4000]
SPEED_OF_SOUND = 343.0   # m/s
HEARD_RISE_DB = 6        # a pass counts as "heard" if LAmax rises this far above the background
CLOSE_KM = 3             # passes this close are "aircraft" moments for the spectra
QUIET_KM, QUIET_S = 15, 60   # "quiet": no aircraft within 15 km slant for 60 s either side
HELICOPTER = "A7"        # ADS-B emitter category for rotorcraft


def fetch():
    """Copy the Pi's database consistently (VACUUM INTO, so a half-written file is never copied)."""
    DB.parent.mkdir(exist_ok=True)
    script = ("import sqlite3, os\n"
              "os.path.exists('/tmp/noise-copy.db') and os.remove('/tmp/noise-copy.db')\n"
              "sqlite3.connect(os.path.expanduser('~/aircraft-noise-data/noise.db'))"
              ".execute(\"VACUUM INTO '/tmp/noise-copy.db'\")\n")
    subprocess.run(["ssh", HOST, "python3", "-"], input=script.encode(), check=True)
    subprocess.run(["scp", "-q", f"{HOST}:/tmp/noise-copy.db", str(DB)], check=True)
    subprocess.run(["ssh", HOST, "rm", "-f", "/tmp/noise-copy.db"], check=True)


def energy_mean(levels):
    """Average of levels in dB, done on sound energy (the correct way to average decibels)."""
    levels = np.asarray(levels, dtype=float)
    return 10 * np.log10(np.mean(10 ** (levels / 10))) if len(levels) else np.nan


def grouped_energy_mean(levels, groups):
    """Energy mean of levels per group label: (labels, means), in one pass over the data."""
    labels, idx = np.unique(groups, return_inverse=True)
    energy = np.bincount(idx, weights=10 ** (levels / 10)) / np.bincount(idx)
    return labels, 10 * np.log10(energy)


def local_hours(ts):
    """Local hour of day for each timestamp (clock changes included), fast for millions of rows."""
    hour_starts, idx = np.unique((ts // 3600) * 3600, return_inverse=True)
    return np.array([time.localtime(h).tm_hour for h in hour_starts])[idx]


def window(ts, start, end):
    """Index range [i, j) of the sorted array ts that falls within [start, end]."""
    return np.searchsorted(ts, start, "left"), np.searchsorted(ts, end, "right")


def load(db):
    bands = ", ".join(f"band_{hz} + cal_db" for hz in BANDS_HZ)
    mics = {}
    for (mic,) in db.execute("SELECT DISTINCT mic FROM levels ORDER BY mic"):
        rows = np.array(db.execute(
            f"SELECT ts, laeq + cal_db, lamax + cal_db, {bands}, flags, seq, uptime_ms "
            "FROM levels WHERE mic = ? AND cal_db IS NOT NULL ORDER BY ts", (mic,)).fetchall(),
            dtype=float)
        if len(rows):
            mics[mic] = {"ts": rows[:, 0], "laeq": rows[:, 1], "lamax": rows[:, 2],
                         "bands": rows[:, 3:10], "flags": rows[:, 10], "seq": rows[:, 11],
                         "uptime": rows[:, 12]}
    passes = db.execute("SELECT closest_ts, closest_slant_km, category FROM passes "
                        "ORDER BY closest_ts").fetchall()
    coverage = np.array(db.execute("SELECT start_ts, end_ts FROM logger_coverage "
                                   "ORDER BY start_ts").fetchall(), dtype=float).reshape(-1, 2)
    # Times when any airborne aircraft was within QUIET_KM slant range (for the quiet baseline).
    rx_alt = float((db.execute("SELECT value FROM meta WHERE key = 'receiver_alt_m'").fetchone()
                    or [0])[0])
    near = np.array(db.execute(
        "SELECT ts, dist_km, coalesce(alt_geom_ft, alt_baro_ft, 0) FROM aircraft_positions "
        "WHERE dist_km < ? AND NOT coalesce(on_ground, 0) ORDER BY ts", (QUIET_KM,)).fetchall(),
        dtype=float).reshape(-1, 3)
    slant = np.hypot(near[:, 1], (near[:, 2] * 0.3048 - rx_alt) / 1000)
    return mics, passes, coverage, near[slant < QUIET_KM, 0]


def arrival_time(closest_ts, slant_km):
    return closest_ts + slant_km * 1000 / SPEED_OF_SOUND


def pass_levels(mic, passes):
    """For each pass: (slant km, LAmax at the mic, background LAeq, is helicopter)."""
    out = []
    for closest_ts, slant, category in passes:
        arrival = arrival_time(closest_ts, slant)
        i, j = window(mic["ts"], arrival - 10, arrival + 10)
        k, m = window(mic["ts"], arrival - 120, arrival - 30)
        if j - i >= 10 and m - k >= 30:
            out.append((slant, mic["lamax"][i:j].max(), np.median(mic["laeq"][k:m]),
                        category == HELICOPTER))
    return np.array(out, dtype=float).reshape(-1, 4)


def complete_range(slant, heard, step=0.5, min_rate=0.9):
    """The distance up to which the mic hears at least min_rate of passes (0.5 km steps)."""
    edge = step
    while edge <= slant.max() + step:
        sel = (slant >= edge - step) & (slant < edge)
        if sel.sum() >= 3 and heard[sel].mean() < min_rate:
            return edge - step
        edge += step
    return edge


def fit_propagation(slant, lamax, background):
    """Fit LAmax = L1 - 20 log10(r) - alpha (r - 1): L1, alpha, rms, passes used, range used.

    Uses the heard passes within the range where at least 90% of passes are heard.
    """
    heard = lamax - background >= HEARD_RISE_DB
    max_km = complete_range(slant, heard)
    heard &= slant < max_km
    slant = slant[heard]
    # Remove the background's share of the energy, leaving the aircraft's own level.
    aircraft = 10 * np.log10(10 ** (lamax[heard] / 10) - 10 ** (background[heard] / 10))
    y = aircraft + 20 * np.log10(slant)                    # undo spherical spreading
    a = np.column_stack([np.ones_like(slant), -(slant - 1)])
    (l1, alpha), *_ = np.linalg.lstsq(a, y, rcond=None)
    rms = np.sqrt(np.mean((y - a @ [l1, alpha]) ** 2))
    return l1, alpha, rms, heard.sum(), max_km


def model(r, l1, alpha):
    return l1 - 20 * np.log10(r) - alpha * (r - 1)


def quiet_mask(ts, near_times, coverage):
    """Seconds with no aircraft within QUIET_KM for QUIET_S either side, while ADS-B was logging."""
    # Next aircraft time at or after ts - QUIET_S: quiet if it's beyond ts + QUIET_S.
    i = np.searchsorted(near_times, ts - QUIET_S)
    no_aircraft = (i == len(near_times)) | (near_times[np.minimum(i, len(near_times) - 1)] > ts + QUIET_S)
    # The coverage period starting at or before ts - QUIET_S must last until ts + QUIET_S.
    c = np.searchsorted(coverage[:, 0], ts - QUIET_S, "right") - 1
    covered = (c >= 0) & (coverage[np.maximum(c, 0), 1] >= ts + QUIET_S)
    return no_aircraft & covered


def near_arrival_mask(ts, arrivals, seconds):
    """Seconds within `seconds` of any of the sorted arrival times."""
    if not len(arrivals):
        return np.zeros(len(ts), bool)
    i = np.searchsorted(arrivals, ts)
    before = np.abs(ts - arrivals[np.maximum(i - 1, 0)])
    after = np.abs(arrivals[np.minimum(i, len(arrivals) - 1)] - ts)
    return np.minimum(before, after) <= seconds


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="use the existing copy, don't fetch")
    args = parser.parse_args()
    if not args.local:
        fetch()
    FIGURES.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    mics, passes, coverage, near_times = load(db)
    report = []
    say = report.append

    t0 = min(m["ts"][0] for m in mics.values()) if mics else time.time()
    t1 = max(m["ts"][-1] for m in mics.values()) if mics else time.time()
    days = max((t1 - t0) / 86400, 1e-9)
    stamp = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))  # noqa: E731
    helis = sum(1 for p in passes if p[2] == HELICOPTER)
    say(f"Period: {stamp(t0)} to {stamp(t1)} ({days:.1f} days)")
    say(f"ADS-B logger coverage: {(coverage[:, 1] - coverage[:, 0]).sum() / 3600:.1f} h; "
        f"{len(passes)} passes within 5 km ({len(passes) / days:.0f} per day, {helis} helicopter)")

    # Data quality per mic: seconds received, lost packets, restarts, mic faults.
    for name, m in mics.items():
        restarts = np.diff(m["uptime"]) < 0
        lost = int(np.where(restarts, 0, (np.diff(m["seq"]) - 1) % 65536).sum())
        say(f"Mic {name}: {len(m['ts'])} s received, {lost} lost "
            f"({100 * lost / max(len(m['ts']) + lost, 1):.2f}%), {int(restarts.sum())} restarts, "
            f"{int(m['flags'].sum())} s mic fault, overall {energy_mean(m['laeq']):.1f} dB(A)")

    arrivals = np.array([arrival_time(t, s) for t, s, _ in passes if s < CLOSE_KM])

    # 1. Timeline: one-minute levels, with close passes marked.
    fig, ax = plt.subplots(figsize=(12, 4))
    for name, m in mics.items():
        minutes, lev = grouped_energy_mean(m["laeq"], (m["ts"] // 60) * 60)
        ax.plot(minutes, lev, lw=0.7, label=f"Mic {name}")
    for a in arrivals:
        ax.axvline(a, color="grey", lw=0.3, alpha=0.4)
    ax.set_ylabel("LAeq, 1 min (dB(A))")
    ax.set_title(f"Sound level over time (grey lines: aircraft passing within {CLOSE_KM} km)")
    ticks = ax.get_xticks()
    ax.set_xticks(ticks, [time.strftime("%d %H:%M", time.localtime(x)) for x in ticks],
                  rotation=30, fontsize=8)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES / "1_timeline.png", dpi=150)

    # 2. Daily profile: average level per hour of day, and passes per hour.
    fig, ax = plt.subplots(figsize=(9, 4))
    for name, m in mics.items():
        hours, lev = grouped_energy_mean(m["laeq"], local_hours(m["ts"]))
        ax.plot(hours, lev, marker="o", label=f"Mic {name}")
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("LAeq (dB(A))")
    ax2 = ax.twinx()
    pass_hours = local_hours(np.array([p[0] for p in passes], dtype=float)) if passes else []
    ax2.bar(range(24), np.bincount(pass_hours, minlength=24)[:24] / days, alpha=0.2, color="grey")
    ax2.set_ylabel("Aircraft passes per hour (bars)")
    ax.set_title("Daily profile")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(FIGURES / "2_daily_profile.png", dpi=150)

    # 3. Propagation: LAmax against slant distance, with the fitted model; and detection rate.
    fig, ax = plt.subplots(figsize=(7, 5))
    for name, m in mics.items():
        pl = pass_levels(m, passes)
        heli = pl[:, 3] == 1
        slant, lamax, background = pl[~heli, :3].T
        heard = lamax - background >= HEARD_RISE_DB
        if heard.sum() < 5:
            say(f"Mic {name}: too few heard fixed-wing passes for the propagation fit")
            continue
        l1, alpha, rms, n, max_km = fit_propagation(slant, lamax, background)
        say(f"Mic {name}: propagation fit over {n} fixed-wing passes within {max_km:.1f} km: "
            f"LAmax = {l1:.1f} dB(A) at 1 km, air absorption {alpha:.1f} dB/km, scatter {rms:.1f} dB")
        for lo, hi in [(0, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 5)]:
            sel = (slant >= lo) & (slant < hi)
            if sel.any():
                say(f"   passes {lo}-{hi} km: {100 * heard[sel].mean():.0f}% rose "
                    f">= {HEARD_RISE_DB} dB above background ({sel.sum()} passes)")
        dots = ax.scatter(slant, lamax, s=8, alpha=0.5, label=f"Mic {name} passes")
        colour = dots.get_facecolor()[0]
        if heli.any():
            ax.scatter(pl[heli, 0], pl[heli, 1], s=24, marker="^", color=colour,
                       label=f"Mic {name} helicopters (not fitted)")
        r = np.linspace(max(slant.min(), 0.3), max_km, 100)
        ax.plot(r, model(r, l1, alpha), color=colour, alpha=1,
                label=f"Mic {name} model: {l1:.0f} dB(A) at 1 km, {alpha:.1f} dB/km")
        ax.axhline(np.median(background), color=colour, ls=":", lw=1)
    ax.set_xlabel("Slant distance at closest approach (km)")
    ax.set_ylabel("LAmax at the mic (dB(A))")
    ax.set_title("Sound propagation: measured vs model (dotted: typical background)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIGURES / "3_propagation.png", dpi=150)

    # 4. Octave-band spectra: close passes vs genuinely quiet times.
    fig, ax = plt.subplots(figsize=(7, 4))
    for name, m in mics.items():
        during = near_arrival_mask(m["ts"], arrivals, 10)
        quiet = quiet_mask(m["ts"], near_times, coverage)
        say(f"Mic {name}: {during.sum()} s near close passes, {quiet.sum()} s quiet "
            f"(no aircraft within {QUIET_KM} km for {QUIET_S} s either side, ADS-B logging)")
        for sel, style, label in [(during, "-", "aircraft"), (quiet, "--", "quiet")]:
            if sel.sum() > 10:
                ax.plot(BANDS_HZ, [energy_mean(m["bands"][sel, b]) for b in range(7)], style,
                        marker="o", label=f"Mic {name}, {label}")
    ax.set_xscale("log")
    ax.set_xticks(BANDS_HZ, [str(b) for b in BANDS_HZ])
    ax.set_xlabel("Octave band centre (Hz)")
    ax.set_ylabel("Band level (dB)")
    ax.set_title("Octave-band spectra: aircraft passing vs quiet")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIGURES / "4_band_spectra.png", dpi=150)

    (FIGURES / "summary.txt").write_text("\n".join(report) + "\n")
    print("\n".join(report))
    print(f"\nFigures in {FIGURES}")


if __name__ == "__main__":
    main()
