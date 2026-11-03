"""The announcer service: every second, ask the model whether the mics hear an aircraft; if they
do, find which aircraft it is and say so on the Sonos.

    python -m aircraft_announcer              # run (as the aircraft-announcer service does)
    python -m aircraft_announcer --dry-run    # everything except speaking: logs what it would say
    python -m aircraft_announcer --say "Hello"   # speak one sentence now (tests voice + speaker)
    python -m aircraft_announcer --dry-run --model NAME   # try another exported model live

Each heard aircraft gets one row in announcer.db's `heard` table, announced or not (quiet hours,
speaker failure), with what was said: the history v2's "unique aircraft only" filter will use.
The collection database is only read, so collecting carries on exactly as before.
"""

import argparse
import logging
import queue
import sqlite3
import threading
import time
from collections import Counter

import numpy as np

from . import config as C
from .detector import Detector, latest_windows, open_levels
from .flights import Lookups, Tracker
from .sonos import ClipServer, Speaker
from .speech import Voice, sentence

log = logging.getLogger("announcer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS heard (
    ts             REAL NOT NULL,   -- unix time of the window the model fired on (its last second)
    hex            TEXT NOT NULL,   -- ICAO address of the matched aircraft
    callsign       TEXT,
    slant_km       REAL,            -- distance when the heard sound was sent
    alt_ft         INTEGER,         -- above sea level
    probability    REAL,            -- the model's highest probability over the mics
    mics           TEXT,            -- which mics heard it, e.g. 'A,B'
    model          TEXT,            -- the model that heard it
    airline        TEXT,
    origin         TEXT,
    destination    TEXT,
    aircraft_model TEXT,
    text           TEXT,            -- the announcement (said, or would have been)
    status         TEXT NOT NULL    -- announced / quiet hours / dry run / speaker failed
);
CREATE INDEX IF NOT EXISTS heard_ts ON heard (ts);
"""


def quiet_hours(t):
    hour = time.localtime(t).tm_hour
    if C.QUIET_FROM_HOUR > C.QUIET_UNTIL_HOUR:      # e.g. 20:00-09:00, across midnight
        return hour >= C.QUIET_FROM_HOUR or hour < C.QUIET_UNTIL_HOUR
    return C.QUIET_FROM_HOUR <= hour < C.QUIET_UNTIL_HOUR


class Announcer(threading.Thread):
    """Works through heard aircraft one at a time: look up, write the sentence, speak, log.
    Runs apart from the detection loop, so lookups and speaking never make it miss a second."""

    def __init__(self, model_name, dry_run):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.model_name = model_name
        self.dry_run = dry_run
        self.next_free = 0.0      # the detection loop waits for this before queueing another
        self.stats = Counter()
        self.clips_dir = C.CACHE_DIR / "clips"
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.voice = None if dry_run else Voice()
        self.speaker = None if dry_run else Speaker(C.SPEAKER, ClipServer(self.clips_dir, C.HTTP_PORT))

    def busy(self, now):
        return not self.jobs.empty() or now < self.next_free

    def run(self):
        db = sqlite3.connect(C.ANNOUNCER_DB, timeout=30)
        db.executescript(SCHEMA)
        lookups = Lookups(db)
        while True:
            heard, speak = self.jobs.get()
            started = time.time()
            if speak:
                self.next_free = started + C.MIN_GAP_S
            try:
                d = lookups.details(heard)
                text = sentence(d)
                status = "quiet hours" if not speak else "dry run" if self.dry_run else None
                if status is None:
                    clip = self.clips_dir / f"{int(started)}.wav"
                    self.voice.to_wav(text, clip)
                    status = "announced" if self.speaker.play(clip, C.VOLUME) else "speaker failed"
                    for old in sorted(self.clips_dir.glob("*.wav"))[:-10]:
                        old.unlink()
                log.info("%s: %s", status, text)
            except Exception:   # one bad lookup or clip mustn't stop the announcer
                log.exception("announcing %s failed", heard)
                d, text, status = heard, None, "failed"
            self.stats[status] += 1
            db.execute("INSERT INTO heard VALUES (:ts, :hex, :callsign, :slant_km, :alt_ft, "
                       ":probability, :mics, :model, :airline, :origin, :destination, "
                       ":aircraft_model, :text, :status)",
                       {"airline": None, "origin": None, "destination": None, **d,
                        "aircraft_model": d.get("model"), "model": self.model_name,
                        "text": text, "status": status, "alt_ft": d.get("alt_ft")})
            db.commit()
            if speak:
                self.next_free = max(self.next_free, time.time())


def run(dry_run, model):
    detector = Detector(model)
    scores = detector.meta.get("scores", {})
    log.info("model %s (threshold %.3f%s)", model, detector.threshold,
             "".join(f", {k} {v}" for k, v in scores.items()))
    log.info("quiet hours %02d:00-%02d:00, at least %d s between announcements, volume %d%s",
             C.QUIET_FROM_HOUR, C.QUIET_UNTIL_HOUR, C.MIN_GAP_S, C.VOLUME,
             " (dry run: nothing is spoken)" if dry_run else "")
    tracker = Tracker()
    announcer = Announcer(model, dry_run)
    announcer.start()
    levels = open_levels()
    handled = {}              # hex -> when it was last queued (one announcement per pass)
    stats = Counter()
    next_summary = time.time() + C.SUMMARY_EVERY_S

    while True:
        tick = time.time()
        tracker.poll()
        try:
            wins = latest_windows(levels)
        except sqlite3.Error as e:
            log.warning("reading levels: %s", e)
            wins = {}
        stats["judged"] += bool(wins)
        if wins:
            mics = sorted(wins)
            probs = detector.probability(np.stack([wins[m][1] for m in mics]))
            fired = [m for m, p in zip(mics, probs) if p >= detector.threshold]
            if fired:
                stats["heard"] += 1
                t = max(wins[m][0] for m in fired)
                match = tracker.match(t)
                if match is None:
                    stats["no aircraft matched"] += 1
                elif tick - handled.get(match["hex"], 0) > C.REANNOUNCE_AFTER_S:
                    speak = not quiet_hours(tick)
                    if not speak or not announcer.busy(tick):
                        handled[match["hex"]] = tick
                        announcer.jobs.put(({**match, "ts": t, "mics": ",".join(fired),
                                             "probability": float(max(probs))}, speak))
                    # else: still speaking the previous one; try again next second while
                    # this aircraft is still being heard

        if tick >= next_summary:
            log.info("last %d min: %d s judged, %d s heard (%d s with no aircraft to match); "
                     "%s; tracking %d aircraft", C.SUMMARY_EVERY_S // 60, stats["judged"],
                     stats["heard"], stats["no aircraft matched"],
                     ", ".join(f"{v} {k}" for k, v in announcer.stats.items()) or "none heard",
                     len(tracker.tracks))
            stats.clear()
            announcer.stats.clear()
            next_summary = tick + C.SUMMARY_EVERY_S
            handled = {h: ts for h, ts in handled.items() if tick - ts < C.REANNOUNCE_AFTER_S}
        time.sleep(max(0.0, 1.0 - (time.time() - tick)))


def say(text):
    clips = C.CACHE_DIR / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    clip = clips / "say.wav"
    Voice().to_wav(text, clip)
    speaker = Speaker(C.SPEAKER, ClipServer(clips, C.HTTP_PORT))
    print("played" if speaker.play(clip, C.VOLUME) else "not played (see the warning above)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="don't speak, just log")
    parser.add_argument("--say", metavar="TEXT", help="speak TEXT on the Sonos now and exit")
    parser.add_argument("--model", default=C.MODEL,
                        help=f"exported model in models/ (default, from config.py: {C.MODEL})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.say:
        say(args.say)
    else:
        run(args.dry_run, args.model)


if __name__ == "__main__":
    main()
