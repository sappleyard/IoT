"""Bridge link: stores the levels the bridge ESP32 relays from the mics.

Reads the bridge's JSON lines over USB serial (see esp32/bridge/main/main.c), stores one row
per mic per second, and every 10 minutes logs a health line per mic: packets received and
lost, restarts, mic faults, signal strength. Waits for the bridge to be plugged in and
reconnects if it's unplugged. Runs as the bridge-link service.
"""

import glob
import json
import logging
import signal
import sys
import time

import serial   # pyserial (apt: python3-serial)

from .config import (BRIDGE_SILENT_S, MICS, PAUSE_FILE, SERIAL_BAUD, SUMMARY_EVERY_S)
from .db import BAND_COLUMNS, connect

log = logging.getLogger("bridge-link")

INSERT = (f"INSERT INTO levels (ts, mic, seq, uptime_ms, flags, laeq, lamax, "
          f"{', '.join(BAND_COLUMNS)}, rssi, cal_db) "
          f"VALUES ({', '.join('?' * (9 + len(BAND_COLUMNS)))})")


def open_bridge():
    """The bridge's serial port, or None if it isn't plugged in."""
    ports = sorted(glob.glob("/dev/serial/by-id/*")) or sorted(glob.glob("/dev/ttyUSB*"))
    if not ports:
        return None
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = ports[0], SERIAL_BAUD, 1
    ser.dtr = ser.rts = False   # don't reset the board or drop it into its bootloader
    try:
        ser.open()
    except (serial.SerialException, OSError) as e:
        log.warning("can't open %s: %s", ports[0], e)
        return None
    log.info("connected to the bridge on %s", ports[0])
    return ser


class MicStats:
    """Counts for one mic between health lines."""

    def __init__(self):
        self.received = self.lost = self.restarts = self.faults = 0
        self.rssi_sum = 0
        self.last_seq = self.last_uptime = None
        self.last_laeq = None

    def add(self, msg):
        if self.last_seq is not None:
            if msg["uptime_ms"] < self.last_uptime:
                self.restarts += 1
            else:
                self.lost += (msg["seq"] - self.last_seq - 1) & 0xFFFF
        self.last_seq, self.last_uptime = msg["seq"], msg["uptime_ms"]
        self.received += 1
        self.faults += msg["flags"] & 1
        self.rssi_sum += msg["rssi"]
        self.last_laeq = msg["laeq"]

    def summary(self):
        total = self.received + self.lost
        return (f"{self.received} received, {self.lost} lost ({100 * self.lost / total:.1f}%), "
                f"{self.restarts} restarts, {self.faults} s mic fault, "
                f"rssi {self.rssi_sum / self.received:.0f} dBm, last LAeq {self.last_laeq} dB(A)")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))   # systemd stop: commit and exit
    db = connect()
    names = {mac: mic["name"] for mac, mic in MICS.items()}
    cal = {mic["name"]: mic["cal_db"] for mic in MICS.values()}
    stats = {name: MicStats() for name in cal}
    warned_unknown = set()
    ser, buf = None, b""
    last_heartbeat = time.monotonic()
    next_summary = time.monotonic() + SUMMARY_EVERY_S
    last_commit = time.monotonic()

    try:
        while True:
            if PAUSE_FILE.exists():
                if ser is not None:
                    ser.close()
                    ser = None
                    log.info("paused: serial port released")
                time.sleep(1)
                last_heartbeat = time.monotonic()
                continue
            if ser is None:
                ser = open_bridge()
                if ser is None:
                    time.sleep(5)
                    continue
                buf = b""

            try:
                buf += ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                log.warning("lost the bridge: %s", e)
                ser.close()
                ser = None
                continue

            *lines, buf = buf.split(b"\n")
            for line in lines:
                now = time.time()
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue   # boot messages and other non-JSON output
                if msg.get("type") == "bridge":
                    last_heartbeat = time.monotonic()
                elif msg.get("type") == "levels":
                    mic = names.get(msg["mac"], msg["mac"])
                    if mic not in cal and mic not in warned_unknown:
                        log.warning("levels from %s, which isn't in MICS in config.py", mic)
                        warned_unknown.add(mic)
                    db.execute(INSERT, (now, mic, msg["seq"], msg["uptime_ms"], msg["flags"],
                                        msg["laeq"], msg["lamax"], *msg["bands"],
                                        msg["rssi"], cal.get(mic)))
                    stats.setdefault(mic, MicStats()).add(msg)

            if time.monotonic() - last_commit >= 1:
                db.commit()
                last_commit = time.monotonic()

            if time.monotonic() - last_heartbeat > BRIDGE_SILENT_S:
                log.warning("no heartbeat from the bridge for %d s", BRIDGE_SILENT_S)
                last_heartbeat = time.monotonic()

            if time.monotonic() >= next_summary:
                for mic, s in stats.items():
                    if s.received:
                        log.info("mic %s: %s", mic, s.summary())
                    else:
                        log.warning("mic %s: nothing received in %d min", mic, SUMMARY_EVERY_S // 60)
                stats = {mic: MicStats() for mic in stats}
                next_summary = time.monotonic() + SUMMARY_EVERY_S
    finally:
        db.commit()


if __name__ == "__main__":
    main()
