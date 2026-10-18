"""The text lines the ESP32s print over USB serial, and a reader that copes with unplugging.

Every line from a board is one of:

    {"type": "report", ...}      JSON: a node report, or a status message from the firmware
    A <node> <seq> <base64>      a chunk of raw audio: 16 kHz, 16-bit little-endian, mono
    anything else                firmware log text

Lines sent to a board are plain commands, e.g. "listen 2 90" (see esp32/README.md).
"""

import base64
import binascii
import glob
import json
import logging
import time
from dataclasses import dataclass

import serial  # pyserial: apt install python3-serial (Pi) / pip install pyserial (laptop)

from ..config import SERIAL_BAUD

log = logging.getLogger("serial")

# Where an ESP32 shows up, most specific first (Linux, then macOS).
PORT_PATTERNS = [
    "/dev/serial/by-id/*Espressif*",
    "/dev/serial/by-id/*",
    "/dev/ttyACM*",
    "/dev/ttyUSB*",
    "/dev/cu.usbmodem*",
    "/dev/cu.usbserial*",
    "/dev/cu.SLAB_USBtoUART*",
    "/dev/cu.wchusbserial*",
]


@dataclass
class Audio:
    node: int
    seq: int
    pcm: bytes


def parse_line(raw):
    """Return a dict (JSON message), an Audio chunk, or a str (log text)."""
    line = raw.strip()
    if line.startswith(b"{"):
        try:
            return json.loads(line)
        except ValueError:
            pass
    elif line.startswith(b"A "):
        try:
            _, node, seq, data = line.split(b" ", 3)
            return Audio(int(node), int(seq), base64.b64decode(data, validate=True))
        except (ValueError, binascii.Error):
            pass
    return line.decode("utf-8", "replace")


def find_port():
    for pattern in PORT_PATTERNS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


class SerialLines:
    """Whole lines from an ESP32 on a serial port.

    Waits for the board to appear and reopens the port if it is unplugged or resets, so the
    caller can just loop on read_lines(). on_open(self) is called after every (re)connect.
    """

    def __init__(self, port=None, baud=SERIAL_BAUD, on_open=None):
        self.port = port
        self.baud = baud
        self.on_open = on_open
        self.ser = None
        self.buf = b""
        self.said_waiting = False

    def _open(self):
        port = self.port or find_port()
        if port is None:
            raise serial.SerialException("no ESP32 serial port found")
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = port, self.baud, 0.2
        ser.dtr = ser.rts = False   # don't reset the board or drop it into its bootloader
        ser.open()
        log.info("connected to %s", port)
        self.ser, self.buf, self.said_waiting = ser, b"", False
        if self.on_open:
            self.on_open(self)

    def _close(self):
        try:
            self.ser.close()
        except (serial.SerialException, OSError):
            pass
        self.ser = None

    def read_lines(self):
        """Complete lines received so far (often none). Waits up to ~0.2 s, never raises."""
        if self.ser is None:
            try:
                self._open()
            except (serial.SerialException, OSError) as e:
                if not self.said_waiting:
                    log.warning("waiting for the ESP32 (%s)", e)
                    self.said_waiting = True
                time.sleep(2)
                return []
        try:
            self.buf += self.ser.read(self.ser.in_waiting or 1)
        except (serial.SerialException, OSError) as e:
            log.warning("lost the serial port: %s", e)
            self._close()
            return []
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > 65536:   # garbage with no newline (wrong baud rate?)
            self.buf = b""
        return lines

    def write_line(self, text):
        if self.ser is None:
            return False
        try:
            self.ser.write(text.encode() + b"\n")
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("lost the serial port: %s", e)
            self._close()
            return False
