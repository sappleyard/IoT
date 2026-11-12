"""How unusual a heard aircraft is: the score behind v2 (only unusual aircraft are announced).

Each heard flight is described by its airline, aircraft model, the far end of its route (the
end that isn't Heathrow) and, only when the model is unknown, its ADS-B category. Each feature
is scored against the `heard` history, recency-weighted (a flight 60 days ago counts half):

    share(v)  = sum over values u of min(weight(u), weight(v)) / total weight
    excess(v) = -log2 (weight(v) / total weight) - entropy of the feature, if > 0
    bits(v)   = 0.3 * -log2 share(v)  +  0.7 * excess(v)

share is like a p-value ("only 2% of flights have a model this rare"); excess is how much more
surprising the value is than that feature's values usually are, so features with many values
(places) don't win just by having many. The flight's score is its rarest feature's bits, plus
half of what the airline + model pair adds. It's ranked among the last 14 days' heard flights
(rescored as if heard now, every 10 min); the top UNIQUE_TOP_PERCENT are unique, with a lead-in
from the feature that made it rare: "First time heard:", "The first Boeing seven four seven in
two weeks:" or "A rare visitor:".
"""

import bisect
import math
import re
import time
from collections import deque

from .config import ALWAYS_UNIQUE, HOME_AIRPORT, UNIQUE_MIN_HISTORY, UNIQUE_TOP_PERCENT
from .speech import spoken_model

WINDOW_DAYS = 14         # the recent heard flights the percentile is taken among
HALF_LIFE_DAYS = 60      # a flight heard this long ago counts half as much as one today
PAIR_WEIGHT = 0.5        # share of a rare airline + model pair's extra rarity that counts
SURPRISAL_SHARE = 0.7    # bits = 0.3 share + 0.7 excess (see above)
RESCORE_S = 600          # how often the recent flights are re-ranked

# ADS-B emitter categories, merged where the difference isn't one people hear
CATEGORIES = {"A1": "light aircraft", "A2": "small aircraft", "A3": "airliner", "A4": "airliner",
              "A5": "heavy", "A6": "fast jet", "A7": "helicopter", "B1": "glider",
              "B2": "balloon", "B4": "microlight", "B6": "drone"}
FEATURES = ("airline", "model", "place", "category")
PAIR = "airline+model"
# The pair is the reason only when it adds at least this much over its rarer half
PAIR_REASON_BITS = 1.0
EPOCH = 1.7e9            # weights grow from here instead of decaying (see Counts)

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
        return (1 - SURPRISAL_SHARE) * rare + SURPRISAL_SHARE * max(0.0, surprise)


class Uniqueness:
    """The heard history's counts, and the recent flights the percentile is taken among."""

    def __init__(self, rows=()):
        half_life = HALF_LIFE_DAYS * 86400
        self.counts = {k: Counts(half_life) for k in (*FEATURES, PAIR)}
        self.flights = 0
        self.recent = deque()          # (ts, features) of the last WINDOW_DAYS
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
        while self.recent and self.recent[0][0] < ts - WINDOW_DAYS * 86400:
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
                score += PAIR_WEIGHT * extra
            if pair >= bits[reason] + PAIR_REASON_BITS:
                reason = PAIR
            bits[PAIR] = pair
        return score, reason, bits

    def score(self, d, ts=None):
        """Add a heard flight to the history and say how unusual it is: a dict with uniqueness
        (bits), percentile (0-100 among recent heard flights), unique (announce it?), always
        (a model in ALWAYS_UNIQUE, unique however common), reason (for the log) and lead_in
        (the words v2 says before the sentence; also the site's highlight label)."""
        ts = time.time() if ts is None else ts
        f = features(d)
        seen = self.add(ts, f)
        score, why, bits = self.raw_score(f)
        if ts - self.rescored >= RESCORE_S:
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
                "lead_in": phrase}


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
