"""Runs an exported model on the live mic levels: "is an aircraft being heard right now?"

A model is a pair of files in announcer/models/, written by model/train.py --export NAME:
  NAME.onnx   any network: input (N, 9, WINDOW_S) windows from features.py, output (N,)
              probabilities (the channel scaling is built in)
  NAME.json   the threshold to use and the feature settings it was trained with, plus notes
              on how it was trained and scored
ONNX runs any architecture, so a retrained or different network needs no code change here.
"""

import json
import sqlite3
import time

import numpy as np

from . import features
from .config import MODELS_DIR, NOISE_DB, STALE_LEVELS_S


class Detector:
    def __init__(self, name):
        import onnxruntime   # here, so the rest of the package can be imported without it

        self.name = name
        self.meta = json.loads((MODELS_DIR / f"{name}.json").read_text())
        if self.meta["features"] != features.settings():
            raise SystemExit(f"Model {name} was trained on features {self.meta['features']}, "
                             f"but features.py makes {features.settings()}: retrain or revert")
        self.threshold = float(self.meta["threshold"])
        self.session = onnxruntime.InferenceSession(str(MODELS_DIR / f"{name}.onnx"),
                                                    providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name

    def probability(self, x):
        """Aircraft probability for each window in x (N, 9, WINDOW_S)."""
        return self.session.run(None, {self.input: x.astype(np.float32)})[0].reshape(-1)


_mics = {"names": [], "checked": 0.0}


def mic_names(db):
    """The mic names in the levels table, refreshed every 10 minutes. Walks the (mic, ts) index
    one mic at a time, since SELECT DISTINCT would read all of the millions of rows."""
    if time.time() - _mics["checked"] > 600:
        _mics["names"] = [m for (m,) in db.execute(
            "WITH RECURSIVE m(mic) AS (SELECT min(mic) FROM levels UNION ALL "
            "SELECT (SELECT min(mic) FROM levels WHERE mic > m.mic) FROM m WHERE mic IS NOT NULL) "
            "SELECT mic FROM m WHERE mic IS NOT NULL")]
        _mics["checked"] = time.time()
    return _mics["names"]


def latest_windows(db, now=None):
    """Each mic's window ending at its latest received second: {mic: (end_ts, x[9, W])}.
    Mics with stale data, gaps or no background yet are left out."""
    now = time.time() if now is None else now
    cols = ", ".join(f"{c} + cal_db" for c in features.CHANNELS)
    out = {}
    for mic in mic_names(db):
        rows = np.array(db.execute(
            f"SELECT ts, {cols}, flags FROM levels WHERE mic = ? AND ts > ? AND ts <= ? "
            "AND cal_db IS NOT NULL ORDER BY ts",
            (mic, now - features.HISTORY_S - 60, now)).fetchall(),
            dtype=float).reshape(-1, len(features.CHANNELS) + 2)
        if not len(rows) or rows[-1, 0] < now - STALE_LEVELS_S:
            continue
        t0, grid = features.to_grid(rows)
        # The newest second on the grid (to_grid drops a reading that rounds past its end).
        seconds = np.round(rows[:, 0] - t0).astype(int)
        end = int(seconds[seconds < len(grid)][-1])
        if end < features.HISTORY_S - 1:
            continue
        x = features.windows(grid, features.minute_background(grid), [end])[0]
        if not np.isnan(x).any():
            out[mic] = (t0 + end, x)
    return out


def open_levels():
    """Read-only connection to the collection database (never blocks the loggers' writes)."""
    return sqlite3.connect(f"file:{NOISE_DB}?mode=ro", uri=True, timeout=10)
