# Sonos-Test

Play a sound on the Sonos speaker (Kitchen, 192.168.1.178) from the Pi.

`play_test.py` finds the speaker with an SSDP search and shares the clip from a small HTTP
server on port 8765. Standard library only; nothing to install.

By default it sends the clip as a Sonos **audio clip** through the speaker's local control
API (HTTPS, port 1443). The clip plays over whatever is on and turns it down while it plays;
the music never stops, so Spotify Connect, AirPlay and everything else carry on afterwards.

`test.wav` is the word "test", made on the Mac with `say -o test.aiff "test"` and
`afconvert -f WAVE -d LEI16@44100 test.aiff test.wav` (the Pi has no text-to-speech engine).

## Run

```sh
ssh sebpi@sebpi 'mkdir -p ~/sonos-test'
scp play_test.py test.wav sebpi@sebpi:~/sonos-test/
ssh sebpi@sebpi 'python3 ~/sonos-test/play_test.py --volume 25'
```

Options: a different clip path as the first argument, `--speaker IP` to skip discovery,
`--volume 0-100` for the clip's volume, `--replace` to use the fallback below.

## Fallback: replace and restore

Speakers without audio clip support (or `--replace`) get the clip through UPnP AVTransport
(port 1400), which replaces the current source. Before the clip the script saves the source,
queue track, position, volume and play state, and puts them back afterwards. That works for
the Sonos queue, radio and HTTP streams, but not Spotify Connect or AirPlay, which are run
from the phone and stay stopped.
