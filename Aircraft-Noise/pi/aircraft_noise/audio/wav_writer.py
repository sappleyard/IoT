"""Write the mic audio stream into hourly WAV files named after their start time."""

import calendar
import re
import struct
import time
from pathlib import Path

from ..config import SAMPLE_RATE

HEADER_UPDATE_S = 5
NAME_RE = re.compile(r"node(\d+)_(\d{8}T\d{6})\.(\d{3})Z\.wav$")


def wav_name(node, start_ts):
    """node0_20261018T140000.125Z.wav: node number and UTC time of the first sample."""
    ms = int(round((start_ts % 1) * 1000)) % 1000
    return f"node{node}_{time.strftime('%Y%m%dT%H%M%S', time.gmtime(int(start_ts)))}.{ms:03d}Z.wav"


def parse_wav_name(path):
    """(node, start_ts) from a file name made by wav_name, or None."""
    m = NAME_RE.search(Path(path).name)
    if not m:
        return None
    start = calendar.timegm(time.strptime(m.group(2), "%Y%m%dT%H%M%S")) + int(m.group(3)) / 1000
    return int(m.group(1)), start


class WavFile:
    """16-bit mono WAV whose header is rewritten as it grows, so a crash or power cut
    still leaves a playable file."""

    def __init__(self, path, rate=SAMPLE_RATE):
        self.f = open(path, "wb")
        self.rate = rate
        self.frames = 0
        self.update_header()

    def update_header(self):
        data_bytes = self.frames * 2
        self.f.seek(0)
        self.f.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + data_bytes, b"WAVE",
                                 b"fmt ", 16, 1, 1, self.rate, self.rate * 2, 2, 16,
                                 b"data", data_bytes))
        self.f.seek(0, 2)
        self.f.flush()

    def write(self, pcm):
        self.f.write(pcm)
        self.frames += len(pcm) // 2

    def close(self):
        self.update_header()
        self.f.close()


class HourlyWavWriter:
    """Turns one node's numbered audio chunks into one WAV per clock hour.

    A few lost chunks are filled with silence so the timing stays right; a longer gap
    (or the board restarting) starts a new file with a fresh start time.
    """

    MAX_FILL_CHUNKS = 16   # 2 s at 125 ms per chunk

    def __init__(self, directory, node, rate=SAMPLE_RATE):
        self.dir = Path(directory)
        self.node = node
        self.rate = rate
        self.file = None
        self.start_ts = 0.0
        self.last_seq = 0
        self.last_header = 0.0

    def write(self, pcm, seq, arrival_ts):
        frames = len(pcm) // 2
        if self.file is not None:
            missing = (seq - self.last_seq - 1) & 0xFFFF
            if missing > self.MAX_FILL_CHUNKS:
                self.close()
            else:
                self.file.write(b"\0\0" * frames * missing)
                end_ts = self.start_ts + self.file.frames / self.rate
                if int(end_ts // 3600) != int(self.start_ts // 3600):
                    self.close()
        if self.file is None:
            # The chunk took its own length to record, so its first sample is that much older.
            self.start_ts = arrival_ts - frames / self.rate
            self.dir.mkdir(parents=True, exist_ok=True)
            self.file = WavFile(self.dir / wav_name(self.node, self.start_ts), self.rate)
        self.file.write(pcm)
        self.last_seq = seq
        if time.monotonic() - self.last_header > HEADER_UPDATE_S:
            self.file.update_header()
            self.last_header = time.monotonic()

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
