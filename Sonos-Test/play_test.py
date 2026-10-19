#!/usr/bin/env python3
"""Play a short clip on the Sonos speaker from the Pi.

Finds the speaker with SSDP, serves the clip over HTTP from this machine, and tells
the speaker to fetch and play it through its UPnP AVTransport service (port 1400).
Standard library only.

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
import urllib.request
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
    """Send a UPnP SOAP action to the speaker and return the response body."""
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
        return resp.read().decode()


def zone_name(speaker):
    with urllib.request.urlopen(f"http://{speaker}:{SONOS_PORT}/xml/device_description.xml", timeout=5) as r:
        m = re.search(r"<roomName>(.*?)</roomName>", r.read().decode())
    return m.group(1) if m else "?"


def transport_state(speaker):
    resp = soap(speaker, "AVTransport", "GetTransportInfo", {"InstanceID": 0})
    m = re.search(r"<CurrentTransportState>(\w+)</CurrentTransportState>", resp)
    return m.group(1) if m else "UNKNOWN"


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

    if args.volume is not None:
        soap(speaker, "RenderingControl", "SetVolume",
             {"InstanceID": 0, "Channel": "Master", "DesiredVolume": args.volume})

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
    print("Done" if started else f"Speaker never started playing (state {state})")
    server.shutdown()


if __name__ == "__main__":
    main()
