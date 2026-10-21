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
from ..config import (AUDIO_DIR, COMMAND_PORT, KEEP_LISTENING, LISTEN_RENEW_S, LISTEN_SECONDS,
                      MIC_CAL_OFFSET_DB, PAUSE_FILE, RECORD_BRIDGE_AUDIO, SERIAL_BAUD,
                      SERIAL_PORT)
from ..db import connect
from .serial_protocol import Audio, SerialLines, parse_line

log = logging.getLogger("bridge-link")

INSERT_REPORT = """INSERT INTO node_reports
    (ts, node, seq, uptime_ms, level_db, peak_db, level_dba, peak_dba, p_aircraft, battery_mv,
     rssi, cal_offset_db)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def store_report(db, msg, ts):
    node = msg.get("node")
    db.execute(INSERT_REPORT, (ts, node, msg.get("seq"), msg.get("uptime_ms"),
                               msg.get("level_db"), msg.get("peak_db"),
                               msg.get("level_dba"), msg.get("peak_dba"), msg.get("p_aircraft"),
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
    last_audio = time.monotonic()   # when recording is on, re-ask if the audio stops
    next_listen = 0.0               # when to renew "listen" for KEEP_LISTENING nodes
    log.info("taking commands on udp://127.0.0.1:%d; audio recording %s",
             COMMAND_PORT, "on" if args.audio else "off")

    # systemd stops services with SIGTERM: exit cleanly so the WAV headers get finalised.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        paused = False
        while True:
            # Paused: close the WAVs and let go of the serial port (e.g. so a board can be flashed).
            if PAUSE_FILE.exists():
                if not paused:
                    log.info("paused (%s exists): serial port released", PAUSE_FILE)
                    for writer in writers.values():
                        writer.close()
                    db.commit()
                    link.close()
                    paused = True
                time.sleep(1)
                continue
            if paused:
                log.info("resumed")
                paused = False
                last_audio = time.monotonic()

            for raw in link.read_lines():
                now = time.time()
                msg = parse_line(raw)
                if isinstance(msg, Audio):
                    last_audio = time.monotonic()
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
                            next_listen = 0.0             # it forgot any queued commands
                elif msg and sum(c.isprintable() for c in msg) > 0.9 * len(msg):
                    # Firmware log text. (The boot ROM talks at a different baud rate, so its
                    # messages arrive as gibberish: those are skipped.)
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

            # The bridge forgets "audio on" when it restarts, and a command sent while it's still
            # booting can be lost: if no audio has arrived for a while, ask again.
            if args.audio and link.ser is not None and time.monotonic() - last_audio > 10:
                log.warning("no audio for 10 s, sending audio on again")
                start_audio(link)
                last_audio = time.monotonic()

            # Keep the chosen nodes listening. The bridge holds each command until that node
            # checks in, and a node that's already listening just extends its deadline.
            if KEEP_LISTENING and link.ser is not None and time.monotonic() >= next_listen:
                for node in KEEP_LISTENING:
                    link.write_line(f"listen {node} {LISTEN_SECONDS}")
                next_listen = time.monotonic() + LISTEN_RENEW_S

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
