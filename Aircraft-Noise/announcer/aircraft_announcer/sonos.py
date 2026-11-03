"""Play a spoken clip on the Sonos speaker, over whatever is playing.

The Pi serves the clip over HTTP and asks the speaker to fetch it. If the speaker supports
audio clips (the Kitchen Era 100 does), the clip goes through the Sonos local control API
(port 1443): it plays over the current audio, turning that down while it plays, and the music
never stops, so Spotify Connect and AirPlay carry on afterwards.

Otherwise the clip replaces the current source through UPnP AVTransport (port 1400): what the
speaker was doing (source, track, position, volume, playing or paused) is saved first and put
back afterwards. That can't restart Spotify Connect or AirPlay, which are run from the phone.
Standard library only. (Proven in the earlier Sonos-Test; this is that code made reusable.)
"""

import functools
import http.server
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
import wave
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

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

    # --- UPnP (port 1400) ---------------------------------------------------------------------
    def soap(self, service, action, args):
        """Send a UPnP SOAP action to the speaker and return its output fields as a dict."""
        paths = {"AVTransport": "/MediaRenderer/AVTransport/Control",
                 "RenderingControl": "/MediaRenderer/RenderingControl/Control"}
        urn = f"urn:schemas-upnp-org:service:{service}:1"
        body_args = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        body = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:{action} xmlns:u="{urn}">{body_args}</u:{action}>'
                "</s:Body></s:Envelope>").encode()
        req = urllib.request.Request(
            f"http://{self.ip}:{SONOS_PORT}{paths[service]}", data=body,
            headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{urn}#{action}"'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            root = ET.fromstring(resp.read())
        response = root.find(".//{%s}%sResponse" % (urn, action))
        return {el.tag: el.text or "" for el in response}

    def transport_state(self):
        return self.soap("AVTransport", "GetTransportInfo", {"InstanceID": 0})["CurrentTransportState"]

    def get_volume(self):
        return int(self.soap("RenderingControl", "GetVolume",
                             {"InstanceID": 0, "Channel": "Master"})["CurrentVolume"])

    def set_volume(self, volume):
        self.soap("RenderingControl", "SetVolume",
                  {"InstanceID": 0, "Channel": "Master", "DesiredVolume": volume})

    def snapshot(self):
        """What the speaker is doing now, enough to put it back with restore()."""
        media = self.soap("AVTransport", "GetMediaInfo", {"InstanceID": 0})
        pos = self.soap("AVTransport", "GetPositionInfo", {"InstanceID": 0})
        return {"uri": media["CurrentURI"], "meta": media["CurrentURIMetaData"],
                "track": pos["Track"], "position": pos["RelTime"],
                "state": self.transport_state(), "volume": self.get_volume()}

    def restore(self, snap):
        """Put the speaker back to a snapshot: same source, track, position, volume and state."""
        seeks = []
        if snap["uri"]:
            self.soap("AVTransport", "SetAVTransportURI",
                      {"InstanceID": 0, "CurrentURI": snap["uri"], "CurrentURIMetaData": snap["meta"]})
            if snap["uri"].startswith("x-rincon-queue:"):
                seeks.append(("TRACK_NR", snap["track"]))
            if re.fullmatch(r"\d+:\d\d:\d\d", snap["position"]) and snap["position"] != "0:00:00":
                seeks.append(("REL_TIME", snap["position"]))
            for unit, target in seeks:
                try:
                    self.soap("AVTransport", "Seek", {"InstanceID": 0, "Unit": unit, "Target": target})
                except urllib.error.HTTPError:
                    pass  # radio streams and line-in can't seek
        self.set_volume(snap["volume"])
        if snap["state"] != "PLAYING":
            return
        self.soap("AVTransport", "Play", {"InstanceID": 0, "Speed": 1})
        if snap["uri"] and seeks and not self.wait_for_playing():
            # Some sources accept the seek but then won't start from there (e.g. a file server
            # without HTTP range support). Fall back to playing from the start.
            self.soap("AVTransport", "SetAVTransportURI",
                      {"InstanceID": 0, "CurrentURI": snap["uri"], "CurrentURIMetaData": snap["meta"]})
            self.soap("AVTransport", "Play", {"InstanceID": 0, "Speed": 1})

    def wait_for_playing(self, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.transport_state() == "PLAYING":
                return True
            time.sleep(0.5)
        return False

    def replace_and_restore(self, url, volume):
        """Play the clip in place of the current source, then put the old source back."""
        snap = self.snapshot()
        if snap["state"] == "PLAYING":    # pause first so the old source doesn't play over it
            self.soap("AVTransport", "Pause", {"InstanceID": 0})
        self.set_volume(volume)
        self.soap("AVTransport", "SetAVTransportURI",
                  {"InstanceID": 0, "CurrentURI": url, "CurrentURIMetaData": ""})
        self.soap("AVTransport", "Play", {"InstanceID": 0, "Speed": 1})
        started = False
        deadline = time.time() + 30
        while time.time() < deadline:
            state = self.transport_state()
            started = started or state == "PLAYING"
            if started and state not in ("PLAYING", "TRANSITIONING"):
                break
            time.sleep(0.5)
        self.restore(snap)
        return started

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
                return self.replace_and_restore(url, volume)
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
