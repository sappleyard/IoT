"""Cut recorded audio into 1 s clips and label each one from the ADS-B log (step 4).

Nobody labels anything by hand: the flight history says when a plane was close.

For a clip centred on time t:
  - Sound from an aircraft at slant range d left it d / 343 s earlier, so the aircraft's
    range is looked up at that earlier time.
  - aircraft:     the nearest aircraft is closer than --aircraft-km (default 3 km).
  - no_aircraft:  nothing closer than --clear-km (default 15 km) for --clear-window s
                  (default 60) either side, and the ADS-B logger was running all that time.
  - anything in between is ambiguous and skipped.

    python3 -m aircraft_noise.labelling.label_clips --dry-run    # just count, to tune thresholds
    python3 -m aircraft_noise.labelling.label_clips              # every WAV in the audio folder
    python3 -m aircraft_noise.labelling.label_clips ~/recordings/*.wav --aircraft-km 2.5

Clips go to <dataset>/aircraft/ and <dataset>/no_aircraft/, listed in <dataset>/labels.csv.
Listen to ~50 of each and adjust the thresholds until nearly all are right.
"""

import argparse
import bisect
import csv
import sys
import time
import wave
from pathlib import Path

from ..audio.wav_writer import parse_wav_name
from ..config import (AIRCRAFT_MAX_KM, AUDIO_DIR, CLEAR_MIN_KM, CLEAR_WINDOW_S, DATASET_DIR,
                      RECEIVER_ALT_M, SAMPLE_RATE, SPEED_OF_SOUND_MS)
from ..db import connect, receiver_location
from ..geo import slant_km

MAX_INTERPOLATE_GAP_S = 10   # interpolate range between positions up to this far apart
MAX_NEAREST_S = 3            # otherwise use a single position only if this close in time
MAX_DELAY_S = 60             # sound delay from the furthest aircraft that matters


class Track:
    """One aircraft's slant range over time."""

    def __init__(self, hex_):
        self.hex = hex_
        self.callsign = None
        self.times = []
        self.ranges = []

    def add(self, ts, km, callsign):
        self.times.append(ts)
        self.ranges.append(km)
        self.callsign = callsign or self.callsign

    def range_at(self, t):
        """Slant range in km at time t, or None if there's no position near then."""
        i = bisect.bisect_left(self.times, t)
        if 0 < i < len(self.times):
            t0, t1 = self.times[i - 1], self.times[i]
            if t1 - t0 <= MAX_INTERPOLATE_GAP_S and t1 > t0:
                f = (t - t0) / (t1 - t0)
                return self.ranges[i - 1] + f * (self.ranges[i] - self.ranges[i - 1])
        nearby = [j for j in (i - 1, i) if 0 <= j < len(self.times)
                  and abs(self.times[j] - t) <= MAX_NEAREST_S]
        if not nearby:
            return None
        return self.ranges[min(nearby, key=lambda j: abs(self.times[j] - t))]

    def range_heard_at(self, t):
        """Range when the sound reaching the mic at time t left the aircraft."""
        d = self.range_at(t)
        for _ in range(2):   # the delay depends on the range, so refine twice
            if d is None:
                return None
            d = self.range_at(t - d * 1000 / SPEED_OF_SOUND_MS)
        return d


def load_tracks(db, receiver, t0, t1):
    tracks = {}
    rows = db.execute("SELECT ts, hex, callsign, lat, lon, alt_baro_ft, alt_geom_ft "
                      "FROM aircraft_positions WHERE ts BETWEEN ? AND ? ORDER BY ts", (t0, t1))
    for ts, hex_, callsign, lat, lon, alt_baro, alt_geom in rows:
        alt = alt_geom if alt_geom is not None else alt_baro
        km = slant_km(receiver, lat, lon, alt, RECEIVER_ALT_M)
        tracks.setdefault(hex_, Track(hex_)).add(ts, km, callsign)
    return tracks


def load_coverage(db, t0, t1):
    return db.execute("SELECT start_ts, end_ts FROM logger_coverage "
                      "WHERE end_ts >= ? AND start_ts <= ?", (t0, t1)).fetchall()


def label_file(db, receiver, path, args, out_dir, writer):
    parsed = parse_wav_name(path)
    if parsed is None:
        print(f"skip {path.name}: name doesn't hold a start time")
        return {}
    node, start = parsed
    with wave.open(str(path)) as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            print(f"skip {path.name}: not 16 kHz 16-bit mono")
            return {}
        frames = w.getnframes()
        n_clips = frames // SAMPLE_RATE
        end = start + n_clips

        margin = args.clear_window + MAX_DELAY_S
        tracks = load_tracks(db, receiver, start - margin, end + margin)
        coverage = load_coverage(db, start - margin, end + margin)
        # Times of every position inside the "clear" radius, for the no_aircraft test.
        near_times = sorted(t for tr in tracks.values()
                            for t, km in zip(tr.times, tr.ranges) if km < args.clear_km)
        # Only aircraft that ever get close enough can make an "aircraft" clip.
        candidates = [tr for tr in tracks.values() if min(tr.ranges) < args.aircraft_km + 2]

        counts = {"aircraft": 0, "no_aircraft": 0, "skipped": 0}
        for i in range(n_clips):
            pcm = w.readframes(SAMPLE_RATE)
            t = start + i + 0.5
            nearest, nearest_track = None, None
            for tr in candidates:
                d = tr.range_heard_at(t)
                if d is not None and (nearest is None or d < nearest):
                    nearest, nearest_track = d, tr

            lo, hi = t - args.clear_window, t + args.clear_window
            if nearest is not None and nearest < args.aircraft_km:
                label = "aircraft"
            elif (bisect.bisect_left(near_times, lo) == bisect.bisect_right(near_times, hi)
                  and any(s <= lo and e >= hi for s, e in coverage)):
                label = "no_aircraft"
            else:
                counts["skipped"] += 1
                continue
            counts[label] += 1

            if not args.dry_run:
                clip = out_dir / label / f"{path.stem}_{i:04d}.wav"
                with wave.open(str(clip), "wb") as c:
                    c.setnchannels(1)
                    c.setsampwidth(2)
                    c.setframerate(SAMPLE_RATE)
                    c.writeframes(pcm)
                writer.writerow([
                    f"{label}/{clip.name}", label, node,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                    "" if nearest is None else f"{nearest:.2f}",
                    nearest_track.hex if nearest_track else "",
                    (nearest_track.callsign or "") if nearest_track else "",
                ])
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wavs", nargs="*", type=Path,
                        help=f"WAV files (default: all in {AUDIO_DIR})")
    parser.add_argument("--out", type=Path, default=DATASET_DIR)
    parser.add_argument("--aircraft-km", type=float, default=AIRCRAFT_MAX_KM)
    parser.add_argument("--clear-km", type=float, default=CLEAR_MIN_KM)
    parser.add_argument("--clear-window", type=float, default=CLEAR_WINDOW_S)
    parser.add_argument("--dry-run", action="store_true", help="count labels, write nothing")
    args = parser.parse_args()

    db = connect()
    receiver = receiver_location(db)
    if receiver is None:
        sys.exit("The database has no receiver location. Run sudo readsb-set-location "
                 "<lat> <lon> on the Pi and let the ADS-B logger run.")

    wavs = args.wavs or sorted(AUDIO_DIR.glob("*.wav"))
    if not wavs:
        sys.exit(f"No WAV files found in {AUDIO_DIR}")

    writer = None
    csv_file = None
    if not args.dry_run:
        for label in ("aircraft", "no_aircraft"):
            (args.out / label).mkdir(parents=True, exist_ok=True)
        csv_file = open(args.out / "labels.csv", "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["clip", "label", "node", "time_utc", "nearest_km", "hex", "callsign"])

    totals = {"aircraft": 0, "no_aircraft": 0, "skipped": 0}
    for path in wavs:
        counts = label_file(db, receiver, path, args, args.out, writer)
        if counts:
            print(f"{path.name}: {counts['aircraft']} aircraft, "
                  f"{counts['no_aircraft']} no aircraft, {counts['skipped']} skipped")
            for k in totals:
                totals[k] += counts[k]
    if csv_file:
        csv_file.close()
    print(f"\nTotal: {totals['aircraft']} aircraft, {totals['no_aircraft']} no aircraft, "
          f"{totals['skipped']} ambiguous" + ("" if args.dry_run else f"  ->  {args.out}"))


if __name__ == "__main__":
    main()
