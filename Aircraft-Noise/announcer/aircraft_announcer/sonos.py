"""Play a spoken clip on the Sonos speaker with SoCo.

The Pi serves the clip over HTTP and the speaker plays it in place of whatever it was playing.
What it was doing (source, track, position, volume, playing or paused) is saved first with a
SoCo snapshot and put back afterwards. Spotify Connect and AirPlay are run from the phone, so
they can't be restarted: after an announcement they stay stopped.
"""

import functools
import http.server
import logging
import socket
import threading
import time
import wave

import soco
from soco.snapshot import Snapshot

log = logging.getLogger("announcer")


def local_ip_for(remote_ip):
    """The LAN address of this machine that the speaker can reach."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_ip, 1400))
        return s.getsockname()[0]


def clip_seconds(path):
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        return 5.0


class ClipServer:
    """Serves the clips folder over HTTP for the speaker to fetch; runs for the program's life."""

    def __init__(self, directory, port):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        handler = functools.partial(Handler, directory=str(directory))
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        self.port = port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Speaker:
    def __init__(self, ip, clip_server):
        self.ip = ip      # None: find one on the network each time
        self.clips = clip_server

    def play(self, clip, volume):
        """Play clip (a file in the clip server's folder), wait for it, then put back what was
        playing. Returns True if the speaker played it."""
        try:
            speaker = soco.SoCo(self.ip) if self.ip else soco.discovery.any_soco()
            if speaker is None:
                log.warning("no Sonos speaker found on the network")
                return False
            speaker = speaker.group.coordinator
            snap = Snapshot(speaker)
            snap.snapshot()
            speaker.volume = volume
            speaker.play_uri(f"http://{local_ip_for(speaker.ip_address)}:{self.clips.port}/"
                             f"{clip.name}", title="Aircraft announcer")
            time.sleep(clip_seconds(clip) + 1)
            snap.restore()
            return True
        except Exception as e:     # network, UPnP or speaker errors: skip this announcement
            log.warning("Sonos: %s", e)
            return False
