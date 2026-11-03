"""What a model sees: one mic's last WINDOW_S seconds of levels, minus that mic's background.

Shared by training (model/dataset.py builds every training window with it) and the live
announcer (detector input), so the two can never drift apart. numpy only.

A window is 9 channels (LAeq, LAmax, 7 octave bands) x WINDOW_S seconds. From it the background
is subtracted: the median of each channel over the BG_MINUTES whole minutes before the minute the
window starts in. Every value is therefore "dB above the usual level". A model exported for the
announcer records these settings, and the detector refuses one that was trained on others.
"""

import warnings

import numpy as np

BANDS_HZ = [63, 125, 250, 500, 1000, 2000, 4000]
CHANNELS = ["laeq", "lamax"] + [f"band_{hz}" for hz in BANDS_HZ]
WINDOW_S = 30      # seconds of history in each example (causal: only the past, so it runs live)
FILL_GAP_S = 2     # lost packets up to 2 s in a row are filled with the last value
BG_MINUTES = 3     # background = median of the 3 whole minutes before the window starts

# How far back a window's input reaches: its own seconds plus the background minutes before the
# minute it starts in. The live announcer reads this much history.
HISTORY_S = WINDOW_S + 60 * (BG_MINUTES + 1)


def settings():
    """The feature settings, as saved with an exported model."""
    return {"channels": CHANNELS, "window_s": WINDOW_S, "fill_gap_s": FILL_GAP_S,
            "bg_minutes": BG_MINUTES}


def to_grid(rows):
    """Put one mic's rows (ts, 9 channel values, flags) on a whole-second grid starting on a
    minute: (t0, values[n, 9]). Missing seconds and mic-fault seconds are NaN."""
    ts, values, flags = rows[:, 0], rows[:, 1:-1], rows[:, -1]
    t0 = int(ts.min() // 60 * 60)
    n = int((ts.max() - t0) // 60 + 1) * 60
    grid = np.full((n, len(CHANNELS)), np.nan, dtype=np.float32)
    idx = np.round(ts - t0).astype(int)
    keep = idx < n
    grid[idx[keep]] = values[keep]
    # Bridge lost packets of up to FILL_GAP_S seconds with the last value, so one lost packet
    # doesn't throw away every window containing it. Longer gaps and mic faults stay NaN.
    received = ~np.isnan(grid[:, 0])
    last = np.maximum.accumulate(np.where(received, np.arange(n), -1))
    fill = ~received & (last >= 0) & (np.arange(n) - last <= FILL_GAP_S)
    grid[fill] = grid[last[fill]]
    grid[idx[(flags > 0) & keep]] = np.nan
    return t0, grid


def minute_background(grid):
    """Background per minute m: median of each channel over minutes m-BG_MINUTES .. m-1.
    NaN where fewer than half those seconds were received."""
    minutes = grid.reshape(-1, 60, grid.shape[1])
    m = len(minutes)
    bg = np.full((m, grid.shape[1]), np.nan, dtype=np.float32)
    if m <= BG_MINUTES:
        return bg
    stacked = np.concatenate([minutes[i:m - BG_MINUTES + i] for i in range(BG_MINUTES)], axis=1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN minutes
        med = np.nanmedian(stacked, axis=1)
    enough = (~np.isnan(stacked[:, :, 0])).sum(axis=1) >= BG_MINUTES * 30
    med[~enough] = np.nan
    bg[BG_MINUTES:] = med
    return bg


def windows(grid, bg, ends):
    """The windows ending at grid seconds `ends`, background subtracted: (N, 9, WINDOW_S).
    A window with any NaN (a gap, a mic fault, no background) can't be judged."""
    starts = np.asarray(ends) - WINDOW_S + 1
    x = grid[starts[:, None] + np.arange(WINDOW_S)].transpose(0, 2, 1)
    return x - bg[starts // 60][:, :, None]
