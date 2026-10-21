"""What the ESP32s send over USB serial, and a reader that copes with unplugging.

A board sends a mix of text lines and binary audio frames:

    {"type": "report", ...}      JSON line: a node report, or a status message from the firmware
    anything else ending in \n  firmware log text
    A5 5A <header> <payload> <crc>   binary audio frame (see below)

Audio frame, little-endian: A5 5A | node u8 | seq u16 | n u16 | n bytes mu-law | CRC-16/CCITT u16.
The payload is 125 ms of 16 kHz mono as 8-bit G.711 mu-law (half the size of 16-bit, which the
CH340 USB chip needs); it's decoded back to 16-bit PCM here. The CRC covers node..payload, so a
frame damaged on the way is dropped, never written into a WAV. 0xA5 never appears in text lines.

Lines sent to a board are plain commands, e.g. "listen 2 90" (see esp32/README.md).
"""

import binascii
import glob
import json
import logging
import struct
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


FRAME_MAGIC = b"\xa5\x5a"
FRAME_HEADER = 7              # magic + node + seq + n
FRAME_MAX_SAMPLES = 8192


@dataclass
class Audio:
    node: int
    seq: int
    pcm: bytes                # 16-bit little-endian PCM, ready for a WAV file


def _ulaw_to_linear(u):
    """G.711 mu-law byte -> 16-bit sample (the inverse of the firmware's linear_to_ulaw)."""
    u = ~u & 0xFF
    x = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 0x07)
    x -= 0x84
    return -x if u & 0x80 else x


_ULAW_TO_PCM = [struct.pack("<h", _ulaw_to_linear(u)) for u in range(256)]


def parse_line(raw):
    """Return a dict (JSON message), an Audio chunk, or a str (log text).

    raw is one item from SerialLines.read_lines(): a text line, or an already decoded Audio frame.
    """
    if isinstance(raw, Audio):
        return raw
    line = raw.strip()
    if line.startswith(b"{"):
        try:
            return json.loads(line)
        except ValueError:
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
        self.bad_frames = 0      # audio frames dropped for a bad CRC
        self.skip_text = False   # after a damaged frame, its leftover bytes aren't a text line

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

    def close(self):
        """Let go of the port. The next read_lines() reopens it."""
        if self.ser is not None:
            self._close()

    def read_lines(self):
        """Text lines (bytes) and Audio frames received so far, in order (often none).

        Waits up to ~0.2 s, never raises.
        """
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
        return self._split()

    def _split(self):
        """Cut self.buf into text lines and audio frames, keeping any incomplete tail."""
        items, buf, i = [], self.buf, 0
        while True:
            nl, mg = buf.find(b"\n", i), buf.find(FRAME_MAGIC, i)
            if mg != -1 and (nl == -1 or mg < nl):
                if buf[i:mg].strip() and not self.skip_text:   # stray text before the frame
                    items.append(buf[i:mg])
                self.skip_text = False
                if len(buf) < mg + FRAME_HEADER:
                    i = mg
                    break
                node, seq, n = struct.unpack_from("<BHH", buf, mg + 2)
                if n > FRAME_MAX_SAMPLES:      # not really a frame: look further on
                    i = mg + 1
                    continue
                end = mg + FRAME_HEADER + n + 2
                if len(buf) < end:
                    i = mg
                    break
                body = buf[mg + 2:end - 2]
                if binascii.crc_hqx(body, 0xFFFF) == struct.unpack_from("<H", buf, end - 2)[0]:
                    pcm = b"".join(map(_ULAW_TO_PCM.__getitem__, body[5:]))
                    items.append(Audio(node, seq, pcm))
                    i = end
                else:                          # damaged: resync on the next magic
                    self.bad_frames += 1
                    self.skip_text = True
                    i = mg + 1
            elif nl != -1:
                if not self.skip_text:
                    items.append(buf[i:nl])
                self.skip_text = False
                i = nl + 1
            else:
                break
        self.buf = buf[i:]
        if len(self.buf) > 65536:   # garbage with no newline or frame (wrong baud rate?)
            self.buf = b""
        return items

    def write_line(self, text):
        if self.ser is None:
            return False
        try:
            # The leading newline ends any half-received line (e.g. a command that arrived while
            # the board was still starting up), so it can't get glued onto this one.
            self.ser.write(b"\n" + text.encode() + b"\n")
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("lost the serial port: %s", e)
            self._close()
            return False
