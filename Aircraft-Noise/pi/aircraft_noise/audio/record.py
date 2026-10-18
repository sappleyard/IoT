"""Record raw audio from a node plugged in by USB (step 4).

For the remote spots: put a node where it will live, plug it into a laptop and record.
Also prints the node's 1 s levels, which is handy for calibrating against a phone sound meter.

    pip install pyserial                  # once, on the laptop
    cd Aircraft-Noise/pi
    python3 -m aircraft_noise.audio.record --out ~/recordings
    python3 -m aircraft_noise.audio.record --port /dev/cu.usbmodem101 --out ~/recordings

A node only accepts "audio on" for 30 s after power-on, so start this right after plugging
it in (or press the board's RST button). Afterwards copy the WAVs into the Pi's audio folder
(~/aircraft-noise-data/audio): the labeller reads the start time from the file names, so the
laptop's clock needs to be right (it is if it syncs time automatically).
"""

import argparse
import logging
import time
from pathlib import Path

from ..nodes.serial_protocol import Audio, SerialLines, parse_line
from .wav_writer import HourlyWavWriter


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial port (default: find it)")
    parser.add_argument("--out", type=Path, default=Path("recordings"), help="folder for WAVs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    link = SerialLines(args.port)
    writers = {}
    last_audio = 0.0
    last_ask = 0.0
    print(f"Recording to {args.out.resolve()} (Ctrl+C to stop)")

    try:
        while True:
            for raw in link.read_lines():
                msg = parse_line(raw)
                if isinstance(msg, Audio):
                    if msg.node not in writers:
                        writers[msg.node] = HourlyWavWriter(args.out, msg.node)
                        print(f"Audio arriving from node {msg.node}")
                    writers[msg.node].write(msg.pcm, msg.seq, time.time())
                    last_audio = time.monotonic()
                elif isinstance(msg, dict) and msg.get("type") == "report":
                    print(f"node {msg['node']}: level {msg['level_db']} dB, "
                          f"peak {msg['peak_db']} dB (uncalibrated)")
                elif isinstance(msg, dict):
                    print(msg)
            # Keep asking until audio flows (the node may still be booting).
            if time.monotonic() - last_audio > 2 and time.monotonic() - last_ask > 1:
                link.write_line("audio on")
                last_ask = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        for w in writers.values():
            w.close()


if __name__ == "__main__":
    main()
