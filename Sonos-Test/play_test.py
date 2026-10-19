#!/usr/bin/env python3
"""Play a short clip on the Sonos speaker from the Pi.

Finds the speaker with SSDP, serves the clip over HTTP from this machine, and tells
the speaker to fetch and play it through its UPnP AVTransport service (port 1400).
Before the clip it snapshots what the speaker was doing (source, track, position,
volume, playing or paused) and puts that back afterwards. Standard library only.

    python3 play_test.py [clip.wav] [--speaker 192.168.1.178] [--volume 25]
"""
import argparse
import functools
import http.server
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SSDP_ADDR = ("239.255.255.250", 1900)
SONOS_PORT = 1400
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


def serve(directory, port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("clip", nargs="?", default=os.path.join(here, "test.wav"))
    p.add_argument("--speaker", help="speaker IP (default: discover with SSDP)")
    p.add_argument("--volume", type=int, help="set volume 0-100 before playing")
    args = p.parse_args()

    speaker = args.speaker or discover_sonos()
    print(f"Speaker: {zone_name(speaker)} ({speaker})")

    clip = os.path.abspath(args.clip)
    server = serve(os.path.dirname(clip), HTTP_PORT)
    url = f"http://{local_ip_for(speaker)}:{HTTP_PORT}/{os.path.basename(clip)}"
    print(f"Serving {url}")

    snap = snapshot(speaker)
    print(f"Was {snap['state']}: {snap['uri'] or 'nothing'} (track {snap['track']} at {snap['position']})")

    # Pause first so the old source doesn't keep playing while the clip loads.
    if snap["state"] == "PLAYING":
        soap(speaker, "AVTransport", "Pause", {"InstanceID": 0})
    if args.volume is not None:
        set_volume(speaker, args.volume)

    soap(speaker, "AVTransport", "SetAVTransportURI",
         {"InstanceID": 0, "CurrentURI": url, "CurrentURIMetaData": ""})
    soap(speaker, "AVTransport", "Play", {"InstanceID": 0, "Speed": 1})

    # Keep serving until the speaker has fetched and finished the clip.
    started = False
    deadline = time.time() + 30
    while time.time() < deadline:
        state = transport_state(speaker)
        started = started or state == "PLAYING"
        if started and state != "PLAYING" and state != "TRANSITIONING":
            break
        time.sleep(0.5)
    print("Clip done" if started else f"Speaker never started playing (state {state})")
    server.shutdown()

    restore(speaker, snap)
    print(f"Restored: {transport_state(speaker)}")


if __name__ == "__main__":
    main()
