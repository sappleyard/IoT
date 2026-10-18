"""Send a command to the bridge through the running bridge-link service.

    python3 -m aircraft_noise.nodes.send listen 2 90    # node B: listen for 90 s
    python3 -m aircraft_noise.nodes.send stop 2         # node B: back to sleep now
    python3 -m aircraft_noise.nodes.send audio on       # bridge: stream raw audio to the Pi
    python3 -m aircraft_noise.nodes.send status         # bridge: print its MAC and known nodes

The bridge's replies show up in the service log:  journalctl -u bridge-link -f
"""

import socket
import sys

from ..config import COMMAND_PORT


def send(command):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(command.encode(), ("127.0.0.1", COMMAND_PORT))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    send(" ".join(sys.argv[1:]))
