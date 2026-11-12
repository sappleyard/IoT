"""Train the aircraft classifier on the windows from dataset.py, and compare it with baselines.

    python3 model/train.py              # one split: 1 fold of 7 for test, 1 for validation
    python3 model/train.py --cv         # every fold is the test fold once: mean +- spread
    python3 model/train.py --export     # train, then export the CNN for the announcer
                                        # (as cnn-YYYY-MM-DD; --export NAME to name it)

Needs torch, numpy and scikit-learn (Mac venv). Writes model/data/model.pt and results.txt
(results_cv.txt for --cv). --export also writes announcer/models/NAME.onnx and NAME.json: the
CNN with its channel scaling built in, and the threshold, feature settings and test scores. Set
MODEL = "NAME" in announcer/aircraft_announcer/config.py and run announcer/deploy.sh to use it.

The time line is cut into fixed CHUNK_MIN-minute chunks, and whole chunks go to training,
validation or test, never single seconds: neighbouring seconds are almost the same (strong
autocorrelation), so a random split would put near-copies of the test windows into training and
make every score look better than it is. Both mics' windows from the same chunk go together
(they hear the same aircraft).

Chunks are dealt out by kind, so every set gets loud and quiet times alike: each chunk is
  busy   (BUSY_PASSES or more audible passes; westerly arrivals overhead every ~90 s),
  some   (1 to BUSY_PASSES - 1), or
  quiet  (none: easterly days, nights, gaps between passes),
and day (07:00-23:00) or night. Within each of those six kinds the chunks are shuffled and dealt
into FOLDS folds in turn, so each fold holds about 1/FOLDS of every kind. The plain run tests on
one fold and validates on another, training on the rest; --cv makes every fold the test fold once.

A window also sees the time just before it (its WINDOW_S seconds plus the background minutes
before those), which can reach back into the previous chunk. Where that chunk went to a different
set, windows in the first GUARD_S seconds of the chunk are dropped, so no input is shared between
sets. 15-minute chunks are about the shortest worth using: the guard is 4.5 minutes, so 5-minute
chunks would lose most of their windows.

Neighbouring chunks share the weather and Heathrow's runway mode, so these scores answer "how
well does it work on other moments of the weeks it has seen". A model meant for new weeks should
also be checked on later data it has never seen.

Three detectors, all tuned on the validation fold and scored on the test fold:
  threshold  the simple rule: LAmax rose at least T dB above the background in the last 10 s
  boosting   gradient-boosted trees on 36 summary numbers per window (scikit-learn)
  cnn        a small 1D convolutional network on the whole 9 x 30 window (PyTorch)
"""

import argparse
import time
from pathlib import Path

import numpy as np
import torch
from sklearn.metrics import roc_auc_score
from torch import nn

from dataset import BG_MINUTES, find_sibling
from aircraft_announcer import features   # importable once dataset has found announcer/

HERE = Path(__file__).resolve().parent
EXPORT_DIR = find_sibling("announcer", "aircraft_announcer") / "models"
DATA = HERE / "data" / "windows.npz"
MODEL = HERE / "data" / "model.pt"
RESULTS = HERE / "data" / "results.txt"
DISTANCE_BINS = [(0, 1), (1, 2), (2, 5), (5, 20)]   # km, slant at closest approach

CHUNK_MIN = 15     # minutes per chunk
FOLDS = 7          # chunks dealt into 7 folds: ~14% test, ~14% validation, ~71% training
BUSY_PASSES = 4    # a chunk with 4+ audible passes is busy
SEED = 0           # which random dealing of chunks into folds
EPOCHS = 15
HELI_WEIGHT = 4.0  # helicopter windows' training loss counts 4x (they're ~3% of passes)
KINDS = ["quiet", "some", "busy"]


# --- The CNN ------------------------------------------------------------------------------

class AircraftCNN(nn.Module):
    """9 channels x WINDOW_S seconds -> one logit (aircraft or not).

    Each channel gets its first difference (dB change per second) alongside it, so the network
    sees both how high each band is above the background and how fast it's rising. Three
    convolutions with growing dilation look at 5, 13 and 29 s spans; pooling the whole window
    (mean and max) makes the answer independent of exactly where in the window the plane is.
    """

    def __init__(self, channels=9, width=32, dropout=0.2):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(2 * channels, width, 5, padding=2), nn.ReLU(),
            nn.Conv1d(width, width, 5, padding=4, dilation=2), nn.ReLU(),
            nn.Conv1d(width, width, 5, padding=8, dilation=4), nn.ReLU(),
        )
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(2 * width, 1))

    def forward(self, x):
        # torch.diff(x, prepend=x[:, :, :1]) written out, since ONNX has no diff operator
        dx = x - torch.cat([x[:, :, :1], x[:, :, :-1]], dim=2)
        h = self.net(torch.cat([x, dx], dim=1))
        return self.head(torch.cat([h.mean(dim=2), h.amax(dim=2)], dim=1)).squeeze(1)


def train_cnn(x_tr, y_tr, x_va, y_va, epochs, seed=0, w_tr=None):
    """Train with class-balanced loss, each training window's loss times w_tr (default 1);
    keep the epoch with the best (unweighted) validation loss."""
    torch.manual_seed(seed)
    model = AircraftCNN(channels=x_tr.shape[1])
    pos_weight = torch.tensor((y_tr == 0).sum() / max((y_tr == 1).sum(), 1), dtype=torch.float32)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=pos_weight, reduction="none")
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    xt, yt = torch.from_numpy(x_tr), torch.from_numpy(y_tr.astype(np.float32))
    wt = torch.from_numpy(np.ones(len(y_tr), np.float32) if w_tr is None else w_tr.astype(np.float32))
    xv, yv = torch.from_numpy(x_va), torch.from_numpy(y_va.astype(np.float32))
    best, best_state = np.inf, None
    for epoch in range(epochs):
        model.train()
        order = torch.randperm(len(xt))
        for i in range(0, len(xt), 256):
            b = order[i:i + 256]
            opt.zero_grad()
            loss = (loss_fn(model(xt[b]), yt[b]) * wt[b]).mean()
            loss.backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            val = loss_fn(model(xv), yv).mean().item()
        print(f"  epoch {epoch + 1:2d}: train loss {loss.item():.3f}, validation loss {val:.3f}")
        if val < best:
            best, best_state = val, {k: v.clone() for k, v in model.state_dict().items()}
    model.load_state_dict(best_state)
    model.eval()
    return model


def cnn_scores(model, x):
    with torch.no_grad():
        return torch.sigmoid(model(torch.from_numpy(x))).numpy()


# --- Baselines ------------------------------------------------------------------------------

def threshold_scores(x):
    """The simple rule's score: how far LAmax (channel 1) rose above background in the last 10 s."""
    return x[:, 1, -10:].max(axis=1)


def summary_features(x):
    """36 numbers per window: per channel, the peak and mean of the last 15 s, the rise from the
    window's first 10 s to that peak, and the spread over the whole window."""
    last = x[:, :, -15:]
    return np.concatenate([last.max(2), last.mean(2), last.max(2) - x[:, :, :10].mean(2),
                           x.std(2)], axis=1)


# --- Chunks ---------------------------------------------------------------------------------

def make_chunks(d, chunk_s):
    """Each window's chunk (index into the returned chunk start times), and each chunk's kind:
    0-2 = quiet/some/busy (audible passes, counting each pass once across both mics) + 3 at night."""
    starts, chunk = np.unique(d["t"] // chunk_s * chunk_s, return_inverse=True)
    pos = d["y"] == 1
    pairs = np.unique(np.column_stack([chunk[pos], d["pass_id"][pos]]), axis=0)
    passes = np.bincount(pairs[:, 0], minlength=len(starts))
    busyness = np.digitize(passes, [1, BUSY_PASSES])
    hour = np.zeros(len(starts), int)
    hour[chunk] = d["hour"]     # chunks are whole-hour aligned, so one hour per chunk
    night = (hour < 7) | (hour >= 23)
    return starts, chunk, busyness + 3 * night


def kind_name(k):
    return f"{KINDS[k % 3]} {'night' if k >= 3 else 'day'}"


def deal_folds(kinds, folds, rng):
    """Shuffle the chunks of each kind and deal them into folds in turn (from a random fold, so
    the leftovers don't always land in fold 0)."""
    fold = np.zeros(len(kinds), int)
    for k in np.unique(kinds):
        idx = rng.permutation(np.flatnonzero(kinds == k))
        fold[idx] = (np.arange(len(idx)) + rng.integers(folds)) % folds
    return fold


def split(d, starts, chunk_s, fold, test_fold, val_fold, guard_s, use):
    """Boolean masks (train, validation, test) over the windows, with the guards applied, and
    the windows the guards dropped."""
    role = np.where(fold == test_fold, 2, np.where(fold == val_fold, 1, 0))
    # The role of the chunk just before each chunk (-1: no windows there, so nothing to share).
    before = np.minimum(np.searchsorted(starts, starts - chunk_s), len(starts) - 1)
    role_before = np.where(starts[before] == starts - chunk_s, role[before], -1)
    chunk = d["chunk"]
    w_role, w_before = role[chunk], role_before[chunk]
    guarded = (d["t"] - starts[chunk] < guard_s) & (w_before >= 0) & (w_before != w_role)
    keep = use & ~guarded
    return keep & (w_role == 0), keep & (w_role == 1), keep & (w_role == 2), guarded & use


# --- Evaluation ------------------------------------------------------------------------------

def best_threshold(scores, y):
    """The threshold that maximises F1 (chosen on validation, applied unchanged to test)."""
    candidates = np.unique(np.quantile(scores, np.linspace(0, 1, 201)))
    f1 = [f1_score(scores >= c, y) for c in candidates]
    return candidates[int(np.argmax(f1))]


def f1_score(pred, y):
    tp = (pred & (y == 1)).sum()
    return 2 * tp / max(pred.sum() + (y == 1).sum(), 1)


def report(name, pred, d, s, say):
    """Per-second and per-pass scores on the test windows s (a boolean mask over d)."""
    y = d["y"][s]
    p = pred
    tp, fp = (p & (y == 1)).sum(), (p & (y == 0)).sum()
    fn = (~p & (y == 1)).sum()
    precision, recall = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    false_alarm = fp / max((y == 0).sum(), 1)
    say(f"{name:<10} precision {precision:.2f}  recall {recall:.2f}  "
        f"F1 {2 * precision * recall / max(precision + recall, 1e-9):.2f}  "
        f"false alarms {100 * false_alarm:.2f}% of quiet seconds "
        f"(~{false_alarm * 3600:.0f} s per quiet hour)")

    # Per pass: was each (mic, pass) detected in any of its windows?
    pos = y == 1
    keys = d["mic"][s][pos].astype(np.int64) * 10**7 + d["pass_id"][s][pos]
    uniq, inv = np.unique(keys, return_inverse=True)
    detected = np.bincount(inv, weights=p[pos]) > 0
    slant = np.zeros(len(uniq))
    slant[inv] = d["slant"][s][pos]
    heli = np.zeros(len(uniq), bool)
    heli[inv] = d["heli"][s][pos] == 1
    parts = []
    for lo, hi in DISTANCE_BINS:
        b = (slant >= lo) & (slant < hi) & ~heli
        if b.any():
            parts.append(f"{lo}-{hi} km {100 * detected[b].mean():.0f}% of {b.sum()}")
    if heli.any():
        parts.append(f"helicopters {100 * detected[heli].mean():.0f}% of {heli.sum()}")
    say(f"{'':<10} passes detected: " + ", ".join(parts))
    for m, mic in enumerate(d["mic_names"]):
        mm = d["mic"][s] == m
        if mm.any():
            tp_m = (p[mm] & (y[mm] == 1)).sum()
            say(f"{'':<10} mic {mic}: precision {tp_m / max(p[mm].sum(), 1):.2f}, "
                f"recall {tp_m / max((y[mm] == 1).sum(), 1):.2f}")
    # By kind of chunk: does it work in loud and quiet times alike?
    parts = []
    for k in np.unique(d["kind"][s]):
        kk = d["kind"][s] == k
        q, a = kk & (y == 0), kk & (y == 1)
        part = f"{kind_name(k)}: "
        part += f"recall {p[a].mean():.2f}, " if a.sum() >= 20 else ""
        parts.append(part + (f"false alarms {100 * p[q].mean():.1f}%" if q.any() else "no quiet"))
    say(f"{'':<10} by kind: " + "; ".join(parts))


def describe_sets(d, sets, guarded, say):
    """Chunks and windows of each kind in each set."""
    say(f"  chunks of each kind, then windows:\n  {'':<12}"
        + "".join(f"{kind_name(k):>13}" for k in range(6)) + f"{'aircraft':>10}{'quiet':>8}")
    for name, s in sets:
        n_chunks = [len(np.unique(d["chunk"][s & (d["kind"] == k)])) for k in range(6)]
        say(f"  {name:<12}" + "".join(f"{n:>13}" for n in n_chunks)
            + f"{int(d['y'][s].sum()):>10}{int((d['y'][s] == 0).sum()):>8}")
    say(f"  {int(guarded.sum())} windows ({100 * guarded.mean():.1f}%) dropped by the guards")


def run(d, tr, va, te, say, seed=SEED, heli_weight=HELI_WEIGHT, epochs=EPOCHS):
    """Train the three detectors on tr, choose thresholds on va, report on te.
    Helicopter windows' training loss counts heli_weight times.
    Returns (cnn model, scale, cnn threshold, summary numbers for the test fold)."""
    # Scale each channel by its spread in the training data (the values are dB above background).
    x, y = d["x"], d["y"]
    w_tr = np.where((d["heli"][tr] == 1) & (y[tr] == 1), heli_weight, 1.0)
    scale = x[tr].std(axis=(0, 2), keepdims=True)[0] + 1e-6
    xs = (x / scale).astype(np.float32)
    scores = {}

    say("Test results (thresholds chosen on validation):")
    t_thr = best_threshold(threshold_scores(x[va]), y[va])
    scores["threshold"] = (threshold_scores(x[te]), t_thr)
    report("threshold", scores["threshold"][0] >= t_thr, d, te, say)
    say(f"{'':<10} (LAmax at least {t_thr:.1f} dB above background)")

    from sklearn.ensemble import HistGradientBoostingClassifier
    gb = HistGradientBoostingClassifier(max_iter=200, class_weight="balanced", random_state=seed)
    gb.fit(summary_features(x[tr]), y[tr], sample_weight=w_tr)
    g_thr = best_threshold(gb.predict_proba(summary_features(x[va]))[:, 1], y[va])
    scores["boosting"] = (gb.predict_proba(summary_features(x[te]))[:, 1], g_thr)
    report("boosting", scores["boosting"][0] >= g_thr, d, te, say)

    print("Training the CNN:")
    started = time.time()
    model = train_cnn(xs[tr], y[tr], xs[va], y[va], epochs, seed, w_tr)
    c_thr = best_threshold(cnn_scores(model, xs[va]), y[va])
    scores["cnn"] = (cnn_scores(model, xs[te]), c_thr)
    report("cnn", scores["cnn"][0] >= c_thr, d, te, say)
    say(f"{'':<10} ({sum(p.numel() for p in model.parameters())} parameters, "
        f"trained in {time.time() - started:.0f} s)")
    return model, scale, c_thr, {name: summarise(sc, thr, d, te) for name, (sc, thr) in scores.items()}


def summarise(score, thr, d, s):
    """AUC (all, daytime), recall and false-alarm rates (day, night) on the windows s."""
    y, hour = d["y"][s], d["hour"][s]
    day = (hour >= 7) & (hour < 23)
    pred = score >= thr

    def auc(m):
        both = len(np.unique(y[m])) == 2 and (y[m] == 1).sum() >= 100
        return roc_auc_score(y[m], score[m]) if both else np.nan

    def rate(m):
        return pred[m].mean() if m.sum() >= 100 else np.nan

    def passes_found(heli):
        """(mic, pass) pairs with any window over the threshold, and how many there were."""
        m = (y == 1) & (d["heli"][s] == heli)
        keys = d["mic"][s][m].astype(np.int64) * 10**7 + d["pass_id"][s][m]
        _, inv = np.unique(keys, return_inverse=True)
        return (int((np.bincount(inv, weights=pred[m]) > 0).sum()), int(inv.max() + 1 if m.any() else 0))

    return {"auc": auc(np.ones_like(day)), "auc_day": auc(day), "recall": rate(y == 1),
            "fa_day": rate((y == 0) & day), "fa_night": rate((y == 0) & ~day),
            "planes": passes_found(0), "helis": passes_found(1)}


class Exported(nn.Module):
    """The CNN as the announcer runs it: raw features in, aircraft probability out."""

    def __init__(self, model, scale):
        super().__init__()
        self.model = model
        self.register_buffer("scale", torch.from_numpy(scale.astype(np.float32)))

    def forward(self, x):
        return torch.sigmoid(self.model(x / self.scale))


def export(name, model, scale, threshold, scores, d, tr):
    """Write announcer/models/NAME.onnx and NAME.json, and check the ONNX file gives the same
    probabilities as PyTorch."""
    import json

    EXPORT_DIR.mkdir(exist_ok=True)
    onnx_path, meta_path = EXPORT_DIR / f"{name}.onnx", EXPORT_DIR / f"{name}.json"
    wrapped = Exported(model, scale).eval()
    example = torch.from_numpy(d["x"][:64].astype(np.float32))
    torch.onnx.export(wrapped, (example,), str(onnx_path), input_names=["windows"],
                      output_names=["probability"], dynamic_axes={"windows": {0: "n"},
                                                                  "probability": {0: "n"}},
                      dynamo=False)
    try:
        import onnxruntime
        got = onnxruntime.InferenceSession(str(onnx_path)).run(None, {"windows": example.numpy()})[0]
        with torch.no_grad():
            want = wrapped(example).numpy()
        assert np.allclose(got, want, atol=1e-5), "ONNX and PyTorch disagree"
    except ImportError:
        print("(onnxruntime isn't installed here, so the export wasn't checked)")

    def pct(found_total):
        found, total = found_total
        return f"{100 * found / max(total, 1):.0f}% of {total}"

    t = d["t"][tr]
    meta = {
        "name": name,
        "kind": "cnn",
        "created": time.strftime("%Y-%m-%d %H:%M"),
        "threshold": float(threshold),
        "features": features.settings(),
        "trained_on": {"from": time.strftime("%Y-%m-%d", time.localtime(t.min())),
                       "to": time.strftime("%Y-%m-%d", time.localtime(t.max())),
                       "windows": int(tr.sum()), "heli_weight": HELI_WEIGHT,
                       "chunk_min": CHUNK_MIN, "folds": FOLDS, "seed": SEED},
        # Scores on the test fold (chunks never seen in training or threshold tuning).
        "scores": {"auc": round(float(scores["auc"]), 3), "recall": round(float(scores["recall"]), 3),
                   "false alarms by day": f"{100 * scores['fa_day']:.1f}%",
                   "false alarms at night": f"{100 * scores['fa_night']:.1f}%",
                   "aeroplanes detected": pct(scores["planes"]),
                   "helicopters detected": pct(scores["helis"])},
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"Exported for the announcer -> {onnx_path} and {meta_path.name}\n"
          f'To use it: MODEL = "{name}" in announcer/aircraft_announcer/config.py, then deploy.')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cv", action="store_true", help="each fold is the test fold once")
    parser.add_argument("--export", metavar="NAME", nargs="?",
                        const=time.strftime("cnn-%Y-%m-%d"),
                        help="also export the CNN for the announcer (default name cnn-DATE)")
    args = parser.parse_args()
    if args.export and args.cv:
        raise SystemExit("--export goes with a plain run (one model), not --cv")

    d = dict(np.load(DATA))
    use = np.ones(len(d["y"]), bool)
    # The earliest second a window's input reaches back to: its own seconds, then the whole
    # background minutes before the minute it starts in.
    guard_s = d["x"].shape[2] + 60 * (BG_MINUTES + 1)
    chunk_s = 60 * CHUNK_MIN
    starts, d["chunk"], kinds = make_chunks(d, chunk_s)
    d["kind"] = kinds[d["chunk"]]
    fold = deal_folds(kinds, FOLDS, np.random.default_rng(SEED))
    report_lines = []

    def say(line=""):
        print(line)
        report_lines.append(line)

    say(f"{len(starts)} chunks of {CHUNK_MIN} min, dealt into {FOLDS} folds; guard {guard_s} s; "
        f"helicopter weight {HELI_WEIGHT:g}")
    say("Chunks of each kind: " + ", ".join(f"{kind_name(k)} {(kinds == k).sum()}" for k in range(6)))

    if not args.cv:
        tr, va, te, guarded = split(d, starts, chunk_s, fold, 0, 1, guard_s, use)
        describe_sets(d, [("train", tr), ("validation", va), ("test", te)], guarded, say)
        model, scale, c_thr, summary = run(d, tr, va, te, say)
        torch.save({"state_dict": model.state_dict(), "scale": scale, "threshold": float(c_thr),
                    "channels": list(d["channels"]), "window_s": d["x"].shape[2],
                    "train_chunks": starts[np.unique(d["chunk"][tr])].tolist()}, MODEL)
        RESULTS.write_text("\n".join(report_lines) + "\n")
        print(f"\nModel -> {MODEL}\nResults -> {RESULTS}")
        if args.export:
            export(args.export, model, scale, c_thr, summary["cnn"], d, tr)
        return

    folds = []
    for test_fold in range(FOLDS):
        tr, va, te, guarded = split(d, starts, chunk_s, fold, test_fold, (test_fold + 1) % FOLDS,
                                    guard_s, use)
        say(f"\n=== Test fold {test_fold + 1} of {FOLDS} ===")
        describe_sets(d, [("train", tr), ("validation", va), ("test", te)], guarded, say)
        folds.append(run(d, tr, va, te, say)[3])
    say(f"\nCross-validation over {len(folds)} folds: mean +- standard deviation; "
        "passes detected: all folds' test passes together")
    for name in folds[0]:
        parts = []
        for key, label in [("planes", "aeroplanes"), ("helis", "helicopters")]:
            found, total = np.array([f[name][key] for f in folds]).sum(axis=0)
            parts.append(f"{label} {100 * found / max(total, 1):.0f}% of {total}")
        for key, label in [("auc", "AUC"), ("auc_day", "daytime AUC"), ("recall", "recall"),
                           ("fa_day", "false alarms by day"), ("fa_night", "false alarms at night")]:
            v = np.array([f[name][key] for f in folds], float)
            v = v[~np.isnan(v)]
            pct = 100 if key.startswith("fa") else 1
            fmt = ".1f" if pct == 100 else ".3f"
            unit = "%" if pct == 100 else ""
            parts.append(f"{label} {pct * v.mean():{fmt}}{unit} +- {pct * v.std():{fmt}}{unit}")
        say(f"{name:<10} " + ", ".join(parts))
    cv_path = RESULTS.with_name("results_cv.txt")
    cv_path.write_text("\n".join(report_lines) + "\n")
    print(f"\nResults -> {cv_path}")


if __name__ == "__main__":
    main()
