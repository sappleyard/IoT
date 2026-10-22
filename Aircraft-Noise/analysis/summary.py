"""Basic time-series analysis of the collected data: figures and a text summary for the report.

    python3 analysis/summary.py            # copy the latest database from the Pi, then analyse
    python3 analysis/summary.py --local    # analyse the copy already in analysis/data/

Writes analysis/figures/*.png and summary.txt. Runs on the Mac (needs numpy and matplotlib).
Both folders are git-ignored: the database copy contains the home location.

Sound propagation model (see the design summary): an aircraft is treated as a point source, so
its level falls 6 dB per doubling of distance (spherical spreading) plus air absorption:

    LAmax(r) = L1 - 20 log10(r / 1 km) - alpha (r - 1 km)

L1 (the level at 1 km) and alpha (dB/km) are fitted to the passes. The sound from a pass
reaches the mic r / 343 m/s after the aircraft's closest moment, which is where each pass's
level is read. Only passes that rose clearly above the background are fitted, with the
background's energy subtracted first (as in ISO 1996): otherwise distant passes, whose "peak"
is really just background noise, flatten the curve and hide the air absorption.
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
    passes = db.execute("SELECT closest_ts, closest_slant_km, callsign FROM passes "
                        "ORDER BY closest_ts").fetchall()
    coverage = db.execute("SELECT start_ts, end_ts FROM logger_coverage").fetchall()
    return mics, passes, coverage


def pass_levels(mic, passes):
    """For each pass: (slant km, LAmax at the mic, background LAeq), using the sound delay."""
    out = []
    for closest_ts, slant, _ in passes:
        arrival = closest_ts + slant * 1000 / SPEED_OF_SOUND
        peak = (mic["ts"] >= arrival - 10) & (mic["ts"] <= arrival + 10)
        before = (mic["ts"] >= arrival - 120) & (mic["ts"] <= arrival - 30)
        if peak.sum() >= 10 and before.sum() >= 30:
            out.append((slant, mic["lamax"][peak].max(), np.median(mic["laeq"][before])))
    return np.array(out).reshape(-1, 3)


def fit_propagation(slant, lamax, background):
    """Fit LAmax = L1 - 20 log10(r) - alpha (r - 1) to the heard passes: L1, alpha, rms, n."""
    heard = lamax - background >= HEARD_RISE_DB
    slant = slant[heard]
    # Remove the background's share of the energy, leaving the aircraft's own level.
    aircraft = 10 * np.log10(10 ** (lamax[heard] / 10) - 10 ** (background[heard] / 10))
    y = aircraft + 20 * np.log10(slant)                    # undo spherical spreading
    a = np.column_stack([np.ones_like(slant), -(slant - 1)])
    (l1, alpha), *_ = np.linalg.lstsq(a, y, rcond=None)
    rms = np.sqrt(np.mean((y - a @ [l1, alpha]) ** 2))
    return l1, alpha, rms, heard.sum()


def model(r, l1, alpha):
    return l1 - 20 * np.log10(r) - alpha * (r - 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local", action="store_true", help="use the existing copy, don't fetch")
    args = parser.parse_args()
    if not args.local:
        fetch()
    FIGURES.mkdir(exist_ok=True)
    db = sqlite3.connect(DB)
    mics, passes, coverage = load(db)
    report = []
    say = report.append

    t0 = min(m["ts"][0] for m in mics.values()) if mics else time.time()
    t1 = max(m["ts"][-1] for m in mics.values()) if mics else time.time()
    days = max((t1 - t0) / 86400, 1e-9)
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    say(f"Period: {fmt(t0)} to {fmt(t1)} ({days:.1f} days)")
    say(f"ADS-B logger coverage: {sum(e - s for s, e in coverage) / 3600:.1f} h; "
        f"{len(passes)} passes within 5 km ({len(passes) / days:.0f} per day)")

    # Data quality per mic: seconds received, lost packets, restarts, mic faults.
    for name, m in mics.items():
        restarts = np.diff(m["uptime"]) < 0
        gaps = np.where(restarts, 0, (np.diff(m["seq"]) - 1) % 65536)
        lost = int(gaps.sum())
        say(f"Mic {name}: {len(m['ts'])} s received, {lost} lost "
            f"({100 * lost / max(len(m['ts']) + lost, 1):.2f}%), {int(restarts.sum())} restarts, "
            f"{int(m['flags'].sum())} s mic fault, overall {energy_mean(m['laeq']):.1f} dB(A)")

    # 1. Timeline: one-minute levels, with close passes marked.
    fig, ax = plt.subplots(figsize=(12, 4))
    for name, m in mics.items():
        minute = (m["ts"] // 60) * 60
        keys, idx = np.unique(minute, return_inverse=True)
        lev = [energy_mean(m["laeq"][idx == i]) for i in range(len(keys))]
        ax.plot(keys, lev, lw=0.7, label=f"Mic {name}")
    for closest_ts, slant, _ in passes:
        if slant < 3:
            ax.axvline(closest_ts + slant * 1000 / SPEED_OF_SOUND, color="grey", lw=0.3, alpha=0.4)
    ax.set_ylabel("LAeq, 1 min (dB(A))")
    ax.set_title("Sound level over time (grey lines: aircraft passing within 3 km)")
    ax.set_xticks(ax.get_xticks())
    ax.set_xticklabels([time.strftime("%d %H:%M", time.localtime(x)) for x in ax.get_xticks()],
                       rotation=30, fontsize=8)
    ax.legend()
    fig.tight_layout()
    fig.savefig(FIGURES / "1_timeline.png", dpi=150)

    # 2. Daily profile: average level per hour of day, and passes per hour.
    fig, ax = plt.subplots(figsize=(9, 4))
    hours = np.arange(24)
    for name, m in mics.items():
        hod = np.array([time.localtime(t).tm_hour for t in m["ts"]])
        ax.plot(hours, [energy_mean(m["laeq"][hod == h]) for h in hours], marker="o", label=f"Mic {name}")
    ax.set_xlabel("Hour of day")
    ax.set_ylabel("LAeq (dB(A))")
    ax2 = ax.twinx()
    phod = [time.localtime(p[0]).tm_hour for p in passes]
    ax2.bar(hours, [phod.count(h) / days for h in hours], alpha=0.2, color="grey")
    ax2.set_ylabel("Aircraft passes per hour (bars)")
    ax.set_title("Daily profile")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(FIGURES / "2_daily_profile.png", dpi=150)

    # 3. Propagation: LAmax against slant distance, with the fitted model; and detection rate.
    fig, ax = plt.subplots(figsize=(7, 5))
    for name, m in mics.items():
        pl = pass_levels(m, passes)
        slant, lamax, background = pl.T
        heard = lamax - background >= HEARD_RISE_DB
        if heard.sum() < 5:
            say(f"Mic {name}: too few heard passes for the propagation fit")
            continue
        l1, alpha, rms, n = fit_propagation(slant, lamax, background)
        say(f"Mic {name}: propagation fit over {n} heard passes: LAmax = {l1:.1f} dB(A) at 1 km, "
            f"air absorption {alpha:.1f} dB/km, scatter {rms:.1f} dB")
        for lo, hi in [(0, 1.5), (1.5, 2.5), (2.5, 3.5), (3.5, 5)]:
            sel = (slant >= lo) & (slant < hi)
            if sel.any():
                say(f"   passes {lo}-{hi} km: {100 * heard[sel].mean():.0f}% rose "
                    f">= {HEARD_RISE_DB} dB above background ({sel.sum()} passes)")
        dots = ax.scatter(slant, lamax, s=8, alpha=0.5, label=f"Mic {name} passes")
        r = np.linspace(max(slant.min(), 0.3), slant.max(), 100)
        ax.plot(r, model(r, l1, alpha), color=dots.get_facecolor()[0], alpha=1,
                label=f"Mic {name} model: {l1:.0f} dB(A) at 1 km, {alpha:.1f} dB/km")
        ax.axhline(np.median(background), color=dots.get_facecolor()[0], ls=":", lw=1)
    ax.set_xlabel("Slant distance at closest approach (km)")
    ax.set_ylabel("LAmax at the mic (dB(A))")
    ax.set_title("Sound propagation: measured vs model (dotted: typical background)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIGURES / "3_propagation.png", dpi=150)

    # 4. Octave-band spectra: close passes vs quiet times (no pass within 2 minutes).
    fig, ax = plt.subplots(figsize=(7, 4))
    arrivals = np.array([p[0] + p[1] * 1000 / SPEED_OF_SOUND for p in passes if p[1] < 3])
    for name, m in mics.items():
        if not len(arrivals):
            break
        nearest = np.min(np.abs(m["ts"][:, None] - arrivals[None, :]), axis=1)
        during, quiet = nearest <= 10, nearest > 120
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
