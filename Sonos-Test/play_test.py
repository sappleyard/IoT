#!/usr/bin/env python3
"""Play a short clip on the Sonos speaker from the Pi.

Finds the speaker with SSDP and serves the clip over HTTP from this machine.

If the speaker supports audio clips (the Kitchen Era 100 does), the clip is sent through
the Sonos local control API (port 1443): it plays over whatever is on, turning that down
while it plays, and the music never stops, so this works for Spotify Connect and AirPlay.

Otherwise, or with --replace, the clip replaces the current source through UPnP
AVTransport (port 1400): the script snapshots what the speaker was doing (source, track,
position, volume, playing or paused) and puts it back afterwards. That can't restart
Spotify Connect or AirPlay, which are run from the phone. Standard library only.

    python3 play_test.py [clip.wav] [--speaker 192.168.1.178] [--volume 25] [--replace]
"""
import argparse
import functools
import http.server
import json
import os
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

SSDP_ADDR = ("239.255.255.250", 1900)
SONOS_PORT = 1400
LOCAL_API_PORT = 1443
# Speakers accept this public key for local control; Home Assistant uses the same one.
LOCAL_API_KEY = "123e4567-e89b-12d3-a456-426655440000"
HTTP_PORT = 8765


def discover_sonos(timeout=3.0):
    """Return the IP of the first Sonos ZonePlayer that answers an SSDP search."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n"
    ).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(msg, SSDP_ADDR)
        try:
            _, (ip, _) = s.recvfrom(4096)
            return ip
        except socket.timeout:
            raise SystemExit("No Sonos speaker found on the network")


def local_ip_for(remote_ip):
    """The LAN address of this machine that the speaker can reach."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((remote_ip, SONOS_PORT))
        return s.getsockname()[0]


def soap(speaker, service, action, args):
    """Send a UPnP SOAP action to the speaker and return its output fields as a dict."""
    paths = {
        "AVTransport": "/MediaRenderer/AVTransport/Control",
        "RenderingControl": "/MediaRenderer/RenderingControl/Control",
    }
    urn = f"urn:schemas-upnp-org:service:{service}:1"
    body_args = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{urn}">{body_args}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode()
    req = urllib.request.Request(
        f"http://{speaker}:{SONOS_PORT}{paths[service]}",
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{urn}#{action}"',
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        root = ET.fromstring(resp.read())
    response = root.find(".//{%s}%sResponse" % (urn, action))
    return {el.tag: el.text or "" for el in response}


def zone_name(speaker):
    with urllib.request.urlopen(f"http://{speaker}:{SONOS_PORT}/xml/device_description.xml", timeout=5) as r:
        m = re.search(r"<roomName>(.*?)</roomName>", r.read().decode())
    return m.group(1) if m else "?"


def transport_state(speaker):
    return soap(speaker, "AVTransport", "GetTransportInfo", {"InstanceID": 0})["CurrentTransportState"]


def get_volume(speaker):
    resp = soap(speaker, "RenderingControl", "GetVolume", {"InstanceID": 0, "Channel": "Master"})
    return int(resp["CurrentVolume"])


def set_volume(speaker, volume):
    soap(speaker, "RenderingControl", "SetVolume",
         {"InstanceID": 0, "Channel": "Master", "DesiredVolume": volume})


def snapshot(speaker):
    """What the speaker is doing now, enough to put it back with restore()."""
    media = soap(speaker, "AVTransport", "GetMediaInfo", {"InstanceID": 0})
    pos = soap(speaker, "AVTransport", "GetPositionInfo", {"InstanceID": 0})
    return {
        "uri": media["CurrentURI"],
        "meta": media["CurrentURIMetaData"],
        "track": pos["Track"],
        "position": pos["RelTime"],
        "state": transport_state(speaker),
        "volume": get_volume(speaker),
    }


def restore(speaker, snap):
    """Put the speaker back to a snapshot: same source, track, position, volume and play state."""
    if snap["uri"]:
        soap(speaker, "AVTransport", "SetAVTransportURI",
             {"InstanceID": 0, "CurrentURI": snap["uri"], "CurrentURIMetaData": snap["meta"]})
        seeks = []
        if snap["uri"].startswith("x-rincon-queue:"):
            seeks.append(("TRACK_NR", snap["track"]))
        if re.fullmatch(r"\d+:\d\d:\d\d", snap["position"]) and snap["position"] != "0:00:00":
            seeks.append(("REL_TIME", snap["position"]))
        for unit, target in seeks:
            try:
                soap(speaker, "AVTransport", "Seek", {"InstanceID": 0, "Unit": unit, "Target": target})
            except urllib.error.HTTPError:
                pass  # radio streams and line-in can't seek
    set_volume(speaker, snap["volume"])
    if snap["state"] != "PLAYING":
        return
    soap(speaker, "AVTransport", "Play", {"InstanceID": 0, "Speed": 1})
    if snap["uri"] and seeks and not wait_for_playing(speaker):
        # Some sources accept the seek but then won't start from there (e.g. a file
        # server without HTTP range support). Fall back to playing from the start.
        soap(speaker, "AVTransport", "SetAVTransportURI",
             {"InstanceID": 0, "CurrentURI": snap["uri"], "CurrentURIMetaData": snap["meta"]})
        soap(speaker, "AVTransport", "Play", {"InstanceID": 0, "Speed": 1})


def wait_for_playing(speaker, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if transport_state(speaker) == "PLAYING":
            return True
        time.sleep(0.5)
    return False


def local_api(speaker, method, path, body=None):
    """Call the speaker's local control API (HTTPS with a self-signed certificate)."""
    req = urllib.request.Request(
        f"https://{speaker}:{LOCAL_API_PORT}/api/v1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-Sonos-Api-Key": LOCAL_API_KEY, "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
        return json.load(resp)


def audio_clip_player_id(speaker):
    """The speaker's player ID if it can play audio clips over other audio, else None."""
    try:
        info = local_api(speaker, "GET", "/players/local/info")
    except (OSError, ValueError):
        return None
    if "AUDIO_CLIP" in info.get("device", {}).get("capabilities", []):
        return info["playerId"]
    return None


def play_audio_clip(speaker, player_id, url, volume):
    body = {
        "name": "Sonos-Test",
        "appId": "com.sebpi.sonostest",
        "streamUrl": url,
        "clipType": "CUSTOM",
        "priority": "LOW",
    }
    if volume is not None:
        body["volume"] = volume
    return local_api(speaker, "POST", f"/players/{player_id}/audioClip", body)


def clip_seconds(path):
    try:
        with wave.open(path) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError):
        return 5.0


def serve(directory, port):
    """Serve directory over HTTP; the returned event is set once a file has been sent."""
    fetched = threading.Event()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            super().do_GET()
            fetched.set()

    handler = functools.partial(Handler, directory=directory)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, fetched


def replace_and_restore(speaker, url, volume):
    """Play the clip in place of the current source, then put the old source back."""
    snap = snapshot(speaker)
    print(f"Was {snap['state']}: {snap['uri'] or 'nothing'} (track {snap['track']} at {snap['position']})")

    # Pause first so the old source doesn't keep playing while the clip loads.
    if snap["state"] == "PLAYING":
        soap(speaker, "AVTransport", "Pause", {"InstanceID": 0})
    if volume is not None:
        set_volume(speaker, volume)

    soap(speaker, "AVTransport", "SetAVTransportURI",
         {"InstanceID": 0, "CurrentURI": url, "CurrentURIMetaData": ""})
    soap(speaker, "AVTransport", "Play", {"InstanceID": 0, "Speed": 1})

    # Wait for the clip to start and finish.
    started = False
    deadline = time.time() + 30
    while time.time() < deadline:
        state = transport_state(speaker)
        started = started or state == "PLAYING"
        if started and state != "PLAYING" and state != "TRANSITIONING":
            break
        time.sleep(0.5)
    print("Clip done" if started else f"Speaker never started playing (state {state})")

    restore(speaker, snap)
    print(f"Restored: {transport_state(speaker)}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clip", nargs="?", default=os.path.join(here, "test.wav"))
    p.add_argument("--speaker", help="speaker IP (default: discover with SSDP)")
    p.add_argument("--volume", type=int, help="volume 0-100 for the clip")
    p.add_argument("--replace", action="store_true",
                   help="replace the current source instead of playing over it")
    args = p.parse_args()

    speaker = args.speaker or discover_sonos()
    print(f"Speaker: {zone_name(speaker)} ({speaker})")

    clip = os.path.abspath(args.clip)
    server, fetched = serve(os.path.dirname(clip), HTTP_PORT)
    url = f"http://{local_ip_for(speaker)}:{HTTP_PORT}/{os.path.basename(clip)}"
    print(f"Serving {url}")

    player_id = None if args.replace else audio_clip_player_id(speaker)
    if player_id:
        play_audio_clip(speaker, player_id, url, args.volume)
        print("Playing as an audio clip over the current audio")
        if fetched.wait(15):
            time.sleep(clip_seconds(clip) + 1)
            print("Clip done")
        else:
            print("Speaker never fetched the clip")
    else:
        replace_and_restore(speaker, url, args.volume)
    server.shutdown()

if __name__ == "__main__":
    main()
