# ESP32 firmware

Three boards, two firmwares:

| Board | Firmware | Job |
|---|---|---|
| Bridge | [bridge/](bridge/) | Plugged into the Pi by USB. Receives the remote nodes' reports over ESP-NOW and passes them to the Pi as JSON lines, along with its own mic's report. Holds the Pi's commands until each node checks in. Can stream its raw audio to the Pi. |
| Node A, Node B | [node/](node/) | Battery or USB powered, in other rooms. Measures its mic and reports to the bridge. Deep-sleeps between heartbeats, and listens when the Pi asks. |

Code both use is in [components/](components/):

| Component | What it does |
|---|---|
| `protocol` | The `NodeReport` and `NodeCommand` packets, and the JSON line format |
| `mic` | INMP441 over I2S at 16 kHz; 1 s level and peak in dB SPL; 16-bit audio for streaming |
| `radio` | Wi-Fi in station mode (connected to nothing) on a fixed channel, plus ESP-NOW |
| `serial_link` | Line-based USB serial: JSON out, commands in, base64 audio chunks |

Written for **ESP-IDF 5.3 or newer**. The boards in use are **original ESP32 dev boards**
(ESP32-D0WD-V3 rev 3.1, USB-C, CH340 USB-serial chip, 4 MB flash). The code also supports the ESP32-S3 (native
USB, faster for the step 5 model) if you switch later.

The CH340 carries the serial link at 921600 baud, comfortably more than the ~43 KB/s raw
audio stream needs. The Pi sees it as `/dev/ttyUSB0`, the Mac as `/dev/cu.usbserial-*` or
`/dev/cu.wchusbserial*` (macOS has a CH340 driver built in; if no port appears, try a different cable, as some USB-C cables are charge-only).

## Wiring the INMP441

| INMP441 | ESP32 (these boards) | ESP32-S3 |
|---|---|---|
| VDD | 3.3 V | 3.3 V |
| GND | GND | GND |
| SCK (bit clock) | GPIO 26 | GPIO 4 |
| WS (word select) | GPIO 25 | GPIO 5 |
| SD (data) | GPIO 33 | GPIO 6 |
| L/R | GND (left channel) | GND (left channel) |

Change the pins in `idf.py menuconfig` → *INMP441 microphone*.

If a board's level stays at the noise floor whatever you do, the original ESP32 may be reading
the wrong I2S slot: change `I2S_STD_SLOT_LEFT` to `I2S_STD_SLOT_RIGHT` in
[components/mic/mic.c](components/mic/mic.c).

## Install ESP-IDF (once, on the Mac)

```
mkdir -p ~/esp && cd ~/esp
git clone -b v5.5.6 --recursive https://github.com/espressif/esp-idf.git
./esp-idf/install.sh esp32
```

In each new terminal before building: `. ~/esp/esp-idf/export.sh`

**VS Code:** install Espressif's *ESP-IDF* extension (`espressif.esp-idf-extension`), run
*ESP-IDF: Configure ESP-IDF Extension* → *Use existing setup*, and point it at `~/esp/esp-idf`
(tools in `~/.espressif`). Open `esp32/bridge` or `esp32/node` as the folder to get build, flash,
monitor and menuconfig buttons in the status bar. The command line and the extension share the same
install. (PlatformIO isn't used: this is a plain ESP-IDF project, with per-node builds that PlatformIO would need extra setup for.)

## Flash the bridge

Plug the board in by USB-C (use a data cable: some USB-C cables are charge-only).

```
cd Aircraft-Noise/esp32/bridge
idf.py set-target esp32            # once
idf.py flash monitor               # Ctrl+] to quit the monitor
```

It prints a line like `{"type":"bridge","mac":"7C:DF:A1:12:34:56",...}`. Copy that MAC into
`node/sdkconfig.node_a` and `node/sdkconfig.node_b` (`CONFIG_NODE_BRIDGE_MAC`). Left as
`FF:FF:FF:FF:FF:FF`, the nodes still work: they broadcast until the bridge first replies.
Broadcasts get no retries, though.

Then you'll see one report a second from the bridge's own mic:
`{"type":"report","node":0,"seq":12,"level_db":41.3,"peak_db":44.0,...}`.
Clap and check the level jumps. To calibrate, compare it with a phone sound-meter app and
put the difference in `MIC_CAL_OFFSET_DB` in [../pi/aircraft_noise/config.py](../pi/aircraft_noise/config.py).

Finally plug the bridge into the Pi; the `bridge-link` service picks it up automatically.

**Flashing a board that's already plugged into the Pi:** build on the Mac, then
`./flash_via_pi.sh bridge` (or `node_a` / `node_b`). It copies the build to the Pi and flashes
it with esptool there.

## Flash the nodes

```
cd Aircraft-Noise/esp32/node
./flash.sh a                       # node A (node_id 1)
./flash.sh b                       # node B (node_id 2)
./flash.sh a menuconfig            # change node A's settings
```

Each node gets its own build folder (`build_a`, `build_b`) and settings file. For an ESP32-S3: `IDF_TARGET=esp32s3 ./flash.sh a`.

Node settings (menuconfig → *Node settings*):

| Setting | Default | |
|---|---|---|
| Deep sleep between heartbeats | on | Off = always listening, for nodes on USB power |
| Heartbeat interval | 5 s | How often a sleeping node checks in |
| Reply wait | 100 ms | How long it stays awake for the bridge's answer |
| Awake after power-on | 30 s | Window to send `audio on` from a laptop |
| Battery ADC | off | Voltage divider on an ADC1 pin → `battery_mv` |

Shared settings (both firmwares, menuconfig → *ESP-NOW radio*): channel (default 1) and
long-range mode. **These must match on all three boards.**

## How it talks

**Node → bridge** (ESP-NOW, 21 bytes): `NodeReport` in [components/protocol/include/protocol.h](components/protocol/include/protocol.h).
`seq` counts every packet a node sends, so gaps mean lost packets.

**Bridge → node** (ESP-NOW, 4 bytes): `NodeCommand`, sent right after that node's report while it's still awake.

**Bridge → Pi** (USB serial, one line each):
```
{"type":"bridge","mac":"..","channel":1,"long_range":false,"audio":false}   at boot / on "status"
{"type":"report","node":1,"seq":5,"uptime_ms":..,"level_db":48.2,"peak_db":51.0,"p_aircraft":null,"battery_mv":0,"rssi":-63}
{"type":"node_seen","node":1,"mac":".."}       first report from a node
{"type":"queued"|"delivered","node":2,"command":"listen","seconds":90}
A 0 812 <base64>                                 raw audio: node, chunk number, 125 ms of 16-bit PCM
```
`level_db` is `null` on a heartbeat (the node didn't listen). `p_aircraft` stays `null` until step 5.

**Pi → bridge** (USB serial): `listen <node> <seconds>`, `stop <node>`, `audio on`, `audio off`, `status`.

A sleeping node runs this cycle:
```
wake (timer) → send heartbeat → wait 100 ms for a command ─┬─ none → deep sleep 5 s
                                                            └─ listen N → report every 1 s for N s → deep sleep
```

## Range test (step 3)

Put the nodes where they'll live, leave them a day, then on the Pi run
`python3 -m aircraft_noise.nodes.gaps`. If a far room loses more than a few percent, turn on
*Long-range mode* on **all three** boards and re-flash.
