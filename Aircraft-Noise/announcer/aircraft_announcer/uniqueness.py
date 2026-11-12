"""How unusual a heard aircraft is, so that only the rare ones are announced (v2).

Each heard flight is described by a few features: airline, aircraft model, the far end of its
route (whichever end isn't London Heathrow) and its ADS-B category (heavy, helicopter, light...).
Every feature is scored against the `heard` history only, recency-weighted (a flight seen 60
days ago counts half as much as one seen today, so a type that stops coming becomes rare again).

Why not just surprisal, -log2 P(value)? Routes, airlines and models are long-tailed: hundreds
of places are each heard once or twice, so "a flight to a once-a-week place" happens dozens of
times a day, and surprisal alone would rank every one of those above a rare aircraft model
(even Dublin, 1 flight in 40, is 5.3 bits). So each feature's bits are measured against what
is usual for that feature, mixing two views (UNIQUE_SURPRISAL_SHARE of the second):

    share(v)  = sum over values u of min(weight(u), weight(v)) / total weight
    excess(v) = -log2 (weight(v) / total weight) - entropy of the feature, if > 0
    bits(v)   = 0.3 * -log2 share(v)  +  0.7 * excess(v)

share is like a p-value ("only 2% of flights have a model this rare"), on the same scale
whichever feature it comes from; min() rather than a hard "at most as common" cut, so values
heard equally often aren't ranked by how recently. excess is how much more surprising the
value is than this feature's values usually are (the entropy is the average surprisal), and
keeps "never heard before" above "heard a few times". Either way a first-time place, common
among places, scores less than a first-time airline, and that less than a new aircraft model.

The flight itself is counted before it's scored (a value never heard before is a singleton),
and the percentile ranks it among the recent flights scored the same way, now, as if each had
just been heard. So scores from the first days (when everything is new) and from today are
one calculation and can be compared; the first days don't announce everything.

The flight's score is its rarest feature's bits, plus half of what the airline + model pair
adds over the rarer of the two (an airline heard often, flying a model heard often, but never
together), so features aren't counted twice. Its percentile among the flights heard in the
last UNIQUE_WINDOW_DAYS decides: the top UNIQUE_TOP_PERCENT are announced, with a short
lead-in from the feature that made it rare ("First time heard:", "The first Boeing seven four
seven in two weeks:", "A rare visitor:").

Dropped on purpose: the airframe (almost every airframe is new, so a new one is the norm, not a
surprise), night (announcements are never made at night, and night isn't rare) and the ADS-B
category when the model is known (the model says more: otherwise a daily Loganair Embraer would
be "a rare small aircraft" every day for ever).
No Markov chains: the order flights arrive in means nothing.
"""

import bisect
import logging
import math
import re
import sqlite3
import time
from collections import deque

from .config import (ALWAYS_UNIQUE, HOME_AIRPORT, NOISE_DB, UNIQUE_HALF_LIFE_DAYS, UNIQUE_MIN_HISTORY,
                     UNIQUE_PAIR_WEIGHT, UNIQUE_RESCORE_S, UNIQUE_SURPRISAL_SHARE,
                     UNIQUE_TOP_PERCENT, UNIQUE_WINDOW_DAYS)
from .speech import spoken_model

log = logging.getLogger("announcer")

# ADS-B emitter categories, merged where the difference isn't one people hear
CATEGORIES = {"A1": "light aircraft", "A2": "small aircraft", "A3": "airliner", "A4": "airliner",
              "A5": "heavy", "A6": "fast jet", "A7": "helicopter", "B1": "glider",
              "B2": "balloon", "B4": "microlight", "B6": "drone"}
FEATURES = ("airline", "model", "place", "category")
PAIR = "airline+model"
# The pair is the reason only when it adds at least this much over its rarer half
PAIR_REASON_BITS = 1.0
EPOCH = 1.7e9            # weights grow from here instead of decaying (see Counts)

# New columns in `heard`, added to existing databases by migrate()
COLUMNS = (("category", "TEXT"),        # ADS-B emitter category, e.g. A5 (heavy)
           ("uniqueness", "REAL"),      # the score in bits (higher = rarer)
           ("percentile", "REAL"),      # among recent heard flights, 0-100
           ("reason", "TEXT"))          # what made it rare, e.g. 'model Boeing 747 (2 before)'


def features(d):
    """The scored features of a flight: a dict from flights.Lookups.details or a `heard` row.
    Unknown values are left out (an unknown model isn't a rare one)."""
    origin, destination = d.get("origin"), d.get("destination")
    model = d.get("model") or d.get("aircraft_model")
    f = {"airline": d.get("airline"),
         # "Atr 72" and "ATR 72" are the same model
         "model": re.sub(r"\s+", " ", model).strip().lower() if model else None,
         "place": destination if origin == HOME_AIRPORT else origin,
         # the category is a coarse stand-in for the model: only used without one
         "category": None if model else CATEGORIES.get(d.get("category"))}
    f = {k: v for k, v in f.items() if v}
    if "airline" in f and "model" in f:
        f[PAIR] = f"{f['airline']} | {f['model']}"
    return f


class Counts:
    """Recency-weighted counts of one feature's values.

    A flight at time t has weight 2^((t - EPOCH) / half-life): newer flights weigh more. Only
    ratios of weights are used, so growing new weights is the same as decaying old ones, and
    adding a flight changes one number instead of every count."""

    def __init__(self, half_life_s):
        self.half_life_s = half_life_s
        self.weight = {}           # value -> summed weight
        self.count = {}            # value -> flights (unweighted)
        self.last = {}             # value -> when last heard
        self.total = 0.0
        self.sorted = None         # (weights ascending, running sums), rebuilt when needed

    def add(self, value, t):
        """Count one flight. Returns (flights with this value before, when last heard)."""
        before = (self.count.get(value, 0), self.last.get(value))
        w = self.weight_at(t)
        self.weight[value] = self.weight.get(value, 0.0) + w
        self.count[value] = before[0] + 1
        self.last[value] = t
        self.total += w
        self.sorted = None
        return before

    def weight_at(self, t):
        return 2 ** ((t - EPOCH) / self.half_life_s)

    def bits(self, value, bump=0.0):
        """How rare the value is, in bits: part -log2 of the share of flights whose value is
        about as rare or rarer, part surprisal beyond the feature's entropy (see the module
        docstring).
        bump: extra weight for this value (an earlier flight's, scored as if heard now)."""
        if value not in self.weight or not self.total:
            return 0.0
        if self.sorted is None:
            weights = sorted(self.weight.values())
            sums, run = [], 0.0
            for w in weights:
                run += w
                sums.append(run)
            # the feature's entropy: how surprising its values are on average
            entropy = math.log2(run) - sum(w * math.log2(w) for w in weights) / run
            self.sorted = (weights, sums, entropy)
        weights, sums, entropy = self.sorted
        w = self.weight[value] + bump
        i = bisect.bisect_right(weights, w)
        # every value counts up to this one's weight: rarer ones in full, commoner ones as if
        # they were as rare as this one (a hard "at most as common" cut would rank values
        # seen equally often by how recently, and the newest flight would always lose)
        tail = (sums[i - 1] if i else 0.0) + w * (len(weights) - i) + bump
        total = self.total + bump
        rare = -math.log2(min(1.0, tail / total))
        # surprisal beyond the feature's average, so features with many values (places)
        # don't win just by having many values
        surprise = -math.log2(w / total) - entropy
        return (1 - UNIQUE_SURPRISAL_SHARE) * rare + UNIQUE_SURPRISAL_SHARE * max(0.0, surprise)


class Uniqueness:
    """The heard history's counts, and the recent flights the percentile is taken among."""

    def __init__(self, rows=()):
        half_life = UNIQUE_HALF_LIFE_DAYS * 86400
        self.counts = {k: Counts(half_life) for k in (*FEATURES, PAIR)}
        self.flights = 0
        self.recent = deque()          # (ts, features) of the last UNIQUE_WINDOW_DAYS
        self.recent_scores = []        # their scores, sorted, against the counts at rescored
        self.rescored = 0.0
        for row in rows:
            self.add(row["ts"], features(row))

    @classmethod
    def from_db(cls, db):
        """Counts from every row of `heard`, oldest first."""
        names = ("ts", "airline", "origin", "destination", "aircraft_model", "category")
        rows = db.execute(f"SELECT {', '.join(names)} FROM heard ORDER BY ts")
        return cls(dict(zip(names, row)) for row in rows)

    def add(self, ts, f):
        seen = {k: self.counts[k].add(v, ts) for k, v in f.items()}
        self.flights += 1
        self.recent.append((ts, f))
        while self.recent and self.recent[0][0] < ts - UNIQUE_WINDOW_DAYS * 86400:
            self.recent.popleft()
        return seen

    def raw_score(self, f, bump=0.0):
        """(score in bits, reason feature, bits per feature) against the current counts.
        bump: see Counts.bits."""
        bits = {k: self.counts[k].bits(v, bump) for k, v in f.items() if k != PAIR}
        if not bits:
            return 0.0, None, bits
        reason = max(bits, key=bits.get)
        score = bits[reason]
        if PAIR in f:
            # the pair counts only for what it adds over its rarer half
            pair = self.counts[PAIR].bits(f[PAIR], bump)
            extra = pair - max(bits["airline"], bits["model"])
            if extra > 0:
                score += UNIQUE_PAIR_WEIGHT * extra
            if pair >= bits[reason] + PAIR_REASON_BITS:
                reason = PAIR
            bits[PAIR] = pair
        return score, reason, bits

    def score(self, d, ts=None):
        """Add a heard flight to the history and say how unusual it is: a dict with uniqueness
        (bits), percentile (0-100 among recent heard flights), unique (announce it?), always
        (a model in ALWAYS_UNIQUE, unique however common), reason
        (for the log), lead_in (said before the sentence, or None) and phrase (those words
        whether or not it is unique)."""
        ts = time.time() if ts is None else ts
        f = features(d)
        seen = self.add(ts, f)
        score, why, bits = self.raw_score(f)
        if ts - self.rescored >= UNIQUE_RESCORE_S:
            # Scores of earlier flights shift as the history grows, so rank against what
            # they'd score if heard now (the same counts, and their own flight as new as this
            # one), not what they scored then.
            now = self.counts["airline"].weight_at(ts)
            self.recent_scores = sorted(
                self.raw_score(g, now - self.counts["airline"].weight_at(t))[0]
                for t, g in self.recent)
            self.rescored = ts
        ranked = self.recent_scores
        below = bisect.bisect_left(ranked, score)
        level = bisect.bisect_right(ranked, score) - below
        percentile = 100 * (below + level / 2) / len(ranked) if ranked else 0.0
        unique = (self.flights > UNIQUE_MIN_HISTORY and why is not None
                  and percentile >= 100 - UNIQUE_TOP_PERCENT)
        reason = None
        if why:
            before, last = seen[why]
            reason = (f"{why} {f[why]} ({before} before"
                      + (f", last {(ts - last) / 86400:.0f} days ago)" if last else ")"))
        phrase = lead_in(why, d, seen[why], ts) if why else None
        model = f.get("model", "")
        special = next((words for key, words in ALWAYS_UNIQUE.items() if key in model), None)
        if special:
            unique, phrase, reason = True, special, f"always: {model}"
        return {"uniqueness": round(score, 2), "percentile": round(percentile, 1),
                "unique": unique, "always": bool(special), "reason": reason,
                "lead_in": phrase if unique else None,
                "phrase": phrase}     # the same words for any flight (the site's highlights)


NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve".split()


def lead_in(why, d, seen, ts):
    """A few words before the usual sentence, from what made the flight rare."""
    before, last = seen
    if before == 0:
        return "First time heard:"
    days = (ts - last) / 86400 if last else 0
    if days < 3:
        return "A rare visitor:"
    weeks = round(days / 7)
    gap = (f"{NUMBERS[weeks]} weeks" if days >= 14 and weeks < len(NUMBERS)
           else f"{NUMBERS[round(days)]} days" if round(days) < len(NUMBERS) else "months")
    model = d.get("model") or d.get("aircraft_model")
    what = {"model": spoken_model(model) if model else None,
            "airline": re.sub(r"\s*\([^)]*\)", "", d.get("airline") or "") + " flight",
            "category": CATEGORIES.get(d.get("category")),
            "place": f"flight {'to' if d.get('origin') == HOME_AIRPORT else 'from'} "
                     f"{d.get('destination') if d.get('origin') == HOME_AIRPORT else d.get('origin')}",
            PAIR: f"{d.get('airline')} {spoken_model(model) if model else ''}".strip()}.get(why)
    return f"The first {what} in {gap}:" if what else "A rare visitor:"


# --- Bringing an existing `heard` table up to date --------------------------------------------

def migrate(db, lookups):
    """Add the v2 columns to `heard` (safe to run on every start). The first time, also fill in
    the old rows so the history isn't split in two: the ADS-B category from the collection's
    passes, and airline, route and model re-derived from the callsign and address with the
    cached lookups (airport names changed style on 2026-11-12: 'Amsterdam Airport Schiphol'
    is now 'Amsterdam'). What was said (`text`) is left as it was."""
    have = {row[1] for row in db.execute("PRAGMA table_info(heard)")}
    missing = [(name, kind) for name, kind in COLUMNS if name not in have]
    for name, kind in missing:
        db.execute(f"ALTER TABLE heard ADD COLUMN {name} {kind}")
    db.commit()
    if "category" not in dict(missing):
        return
    started = time.time()
    rows = db.execute("SELECT rowid, ts, hex, callsign, airline, origin, destination, "
                      "aircraft_model FROM heard").fetchall()
    log.info("bringing %d heard rows up to date for the uniqueness score", len(rows))
    try:
        noise = sqlite3.connect(f"file:{NOISE_DB}?mode=ro", uri=True, timeout=30)
        passes = {}
        for hex_, first, last, category in noise.execute(
                "SELECT lower(hex), first_ts, last_ts, category FROM passes "
                "WHERE category IS NOT NULL"):
            passes.setdefault(hex_, []).append((first, last, category))
        noise.close()
    except sqlite3.Error as e:
        log.warning("no categories for old heard rows (%s): %s", NOISE_DB, e)
        passes = {}
    routes, models = {}, {}
    for rowid, ts, hex_, callsign, airline, origin, destination, model in rows:
        category = next((c for first, last, c in passes.get(hex_.lower(), ())
                         if first - 300 <= ts <= last + 300), None)
        try:
            if callsign not in routes:
                d = lookups.details({"hex": hex_, "callsign": callsign})
                routes[callsign] = (d["airline"], d["origin"], d["destination"])
                models.setdefault(hex_, d["model"])
            if hex_ not in models:
                models[hex_] = lookups.aircraft_model(hex_)
            new = (*routes[callsign], models[hex_])
        except Exception:     # a failed lookup keeps what the row had
            log.exception("re-deriving %s %s", hex_, callsign)
            new = (None,) * 4
        old = (airline, origin, destination, model)
        db.execute("UPDATE heard SET category = ?, airline = ?, origin = ?, destination = ?, "
                   "aircraft_model = ? WHERE rowid = ?",
                   (category, *(n or o for n, o in zip(new, old)), rowid))
    db.commit()
    log.info("heard rows up to date in %.0f s", time.time() - started)
