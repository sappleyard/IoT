"""Play a spoken clip on the Sonos speaker, over whatever is playing.

The Pi serves the clip over HTTP and asks the speaker to fetch it through the Sonos local
control API's audio clips (port 1443; the Kitchen Era 100 supports them): it plays over the
current audio, turning that down while it plays, and the music never stops. Standard library only.
"""

import functools
import http.server
import json
import logging
import socket
import ssl
import threading
import time
import urllib.request
import wave

log = logging.getLogger("announcer")

SSDP_ADDR = ("239.255.255.250", 1900)
SONOS_PORT = 1400
LOCAL_API_PORT = 1443
# Speakers accept this public key for local control; Home Assistant uses the same one.
LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"


def discover(timeout=3.0):
    """The IP of the first Sonos ZonePlayer that answers an SSDP search, or None."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
           'MAN: "ssdp:discover"\r\nMX: 2\r\n'
           "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, SSDP_ADDR)
        try:
            return s.recvfrom(4096)[1][0]
        except socket.timeout:
            return None


def local_ip_for(remote_ip):
    """The LAN address of this machine that the speaker can reach."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_ip, SONOS_PORT))
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
        self.fetched = threading.Event()
        fetched = self.fetched

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                super().do_GET()
                fetched.set()

            def log_message(self, *args):
                pass

        handler = functools.partial(Handler, directory=str(directory))
        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
        self.port = port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class Speaker:
    def __init__(self, ip, clip_server):
        self.ip_setting = ip      # None: find one on the network, and again if it moves
        self.ip = ip
        self.clips = clip_server

    # --- Local control API (port 1443) -------------------------------------------------------
    def local_api(self, method, path, body=None):
        """Call the speaker's local control API (HTTPS with a self-signed certificate)."""
        req = urllib.request.Request(
            f"https://{self.ip}:{LOCAL_API_PORT}/api/v1{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Sonos-Api-Key": LOCAL_API_KEY, "Content-Type": "application/json"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            return json.load(resp)

    def audio_clip_player_id(self):
        """The speaker's player ID if it can play audio clips over other audio, else None."""
        try:
            info = self.local_api("GET", "/players/local/info")
        except (OSError, ValueError):
            return None
        if "AUDIO_CLIP" in info.get("device", {}).get("capabilities", []):
            return info["playerId"]
        return None

    # --- Playing ------------------------------------------------------------------------------
    def play(self, clip, volume):
        """Play clip (a file in the clip server's folder) and wait until it has finished.
        Returns True if the speaker played it."""
        if self.ip is None:
            self.ip = discover()
            if self.ip is None:
                log.warning("no Sonos speaker found on the network")
                return False
        try:
            url = f"http://{local_ip_for(self.ip)}:{self.clips.port}/{clip.name}"
            player_id = self.audio_clip_player_id()
            if not player_id:
                log.warning("Sonos %s can't play audio clips", self.ip)
                return False
            self.clips.fetched.clear()
            self.local_api("POST", f"/players/{player_id}/audioClip", {
                "name": "Aircraft announcer", "appId": "com.sebpi.aircraftannouncer",
                "streamUrl": url, "clipType": "CUSTOM", "priority": "LOW", "volume": volume})
            if not self.clips.fetched.wait(15):
                log.warning("the speaker never fetched %s", url)
                return False
            time.sleep(clip_seconds(clip) + 1)
            return True
        except (OSError, ValueError) as e:
            log.warning("Sonos %s: %s", self.ip, e)
            if self.ip_setting is None:
                self.ip = None    # look for it again next time (its address may have changed)
            return False
