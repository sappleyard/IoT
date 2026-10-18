"""Bridge link (step 3): the one program that talks to the bridge ESP32 over USB serial.

- Stores every node report in the database, stamped with the Pi's clock.
- With --audio (or RECORD_BRIDGE_AUDIO in config.py), saves the bridge mic's raw audio as
  hourly WAV files for step 4.
- Passes commands to the bridge. Other programs send them as UDP datagrams to
  127.0.0.1:COMMAND_PORT; nodes/send.py does this for you, e.g. "listen 2 90".

Runs on the Pi as the bridge-link systemd service. To try it by hand, from the pi/ folder:

    python3 -m aircraft_noise.nodes.link
    python3 -m aircraft_noise.nodes.link --port /dev/ttyACM0 --audio
"""

import argparse
import json
import logging
import signal
import socket
import sys
import time

from ..audio.wav_writer import HourlyWavWriter
from ..config import (AUDIO_DIR, COMMAND_PORT, MIC_CAL_OFFSET_DB, RECORD_BRIDGE_AUDIO,
                      SERIAL_BAUD, SERIAL_PORT)
from ..db import connect
from .serial_protocol import Audio, SerialLines, parse_line

log = logging.getLogger("bridge-link")

INSERT_REPORT = """INSERT INTO node_reports
    (ts, node, seq, uptime_ms, level_db, peak_db, p_aircraft, battery_mv, rssi, cal_offset_db)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def store_report(db, msg, ts):
    node = msg.get("node")
    db.execute(INSERT_REPORT, (ts, node, msg.get("seq"), msg.get("uptime_ms"),
                               msg.get("level_db"), msg.get("peak_db"), msg.get("p_aircraft"),
                               msg.get("battery_mv"), msg.get("rssi"),
                               MIC_CAL_OFFSET_DB.get(node, 0.0)))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", default=SERIAL_PORT, help="serial port (default: find it)")
    parser.add_argument("--audio", action="store_true", default=RECORD_BRIDGE_AUDIO,
                        help="save the bridge mic's raw audio as hourly WAVs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    db = connect()
    commands = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        commands.bind(("127.0.0.1", COMMAND_PORT))
    except OSError as e:
        raise SystemExit(f"Can't listen on udp port {COMMAND_PORT} ({e}). Is the bridge-link "
                         "service already running? (sudo systemctl stop bridge-link)")
    commands.setblocking(False)

    def start_audio(link):
        if args.audio:
            link.write_line("audio on")

    link = SerialLines(args.port, SERIAL_BAUD, on_open=start_audio)
    writers = {}          # node -> HourlyWavWriter
    last_commit = time.monotonic()
    log.info("taking commands on udp://127.0.0.1:%d; audio recording %s",
             COMMAND_PORT, "on" if args.audio else "off")

    # systemd stops services with SIGTERM: exit cleanly so the WAV headers get finalised.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            for raw in link.read_lines():
                now = time.time()
                msg = parse_line(raw)
                if isinstance(msg, Audio):
                    if args.audio:
                        if msg.node not in writers:
                            writers[msg.node] = HourlyWavWriter(AUDIO_DIR, msg.node)
                        writers[msg.node].write(msg.pcm, msg.seq, now)
                elif isinstance(msg, dict):
                    if msg.get("type") == "report":
                        store_report(db, msg, now)
                    else:
                        log.info("bridge: %s", json.dumps(msg))
                        if msg.get("type") == "bridge":   # the bridge has just restarted
                            start_audio(link)
                elif msg:
                    log.info("esp: %s", msg[:200])

            # Forward any commands from other programs.
            while True:
                try:
                    data, _ = commands.recvfrom(256)
                except BlockingIOError:
                    break
                cmd = data.decode("utf-8", "replace").strip()
                if cmd:
                    log.info("command -> bridge: %s", cmd)
                    if not link.write_line(cmd):
                        log.warning("bridge not connected, dropped: %s", cmd)

            if time.monotonic() - last_commit >= 1.0:
                db.commit()
                last_commit = time.monotonic()
    finally:
        for writer in writers.values():
            writer.close()
        db.commit()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
