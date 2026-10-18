# ADS-B Test

Receive aircraft ADS-B broadcasts (1090 MHz) with an RTL-SDR stick on the Raspberry Pi
and read them from Python.

## Setup (once)

Copy the setup script to the Pi and run it with sudo:

```
scp pi/setup_pi.sh sebpi@sebpi:~
ssh -t sebpi@sebpi sudo bash setup_pi.sh
```

It blocks the TV tuner drivers, installs readsb (decoder) and tar1090 (web map).
Then the map is at http://sebpi/tar1090 from any device on the tailnet.

Optionally set the receiver location so distances show up:

```
ssh -t sebpi@sebpi sudo readsb-set-location <latitude> <longitude>
```

## Usage

```
python3 adsb_monitor.py          # live table
python3 adsb_monitor.py --once   # single snapshot
```

No extra Python packages needed.
