# Sonos-Test

Play a sound on the Sonos speaker (Kitchen, 192.168.1.178) from the Pi.

`play_test.py` finds the speaker with an SSDP search, serves the clip from a small HTTP server
on port 8765, and tells the speaker to fetch and play it through its UPnP AVTransport service
on port 1400. Standard library only; nothing to install.

`test.wav` is the word "test", made on the Mac with `say -o test.aiff "test"` and
`afconvert -f WAVE -d LEI16@44100 test.aiff test.wav` (the Pi has no text-to-speech engine).

## Run

```sh
ssh sebpi@sebpi 'mkdir -p ~/sonos-test'
scp play_test.py test.wav sebpi@sebpi:~/sonos-test/
ssh sebpi@sebpi 'python3 ~/sonos-test/play_test.py --volume 25'
```

Options: a different clip path as the first argument, `--speaker IP` to skip discovery,
`--volume 0-100` to set the volume for the clip.

## Resuming what was playing

Before the clip the script saves the speaker's source, queue track, position in the track,
volume and play state. After the clip it puts all of that back and presses play again if
the speaker was playing (it stays paused or stopped if it was). If a source won't restart
at the saved position, it plays that source from the start.

Sources that come back: the Sonos queue (tracks from the Sonos app), radio, and plain
HTTP streams. Spotify Connect and AirPlay sessions are run from the phone, so the speaker
can't restart them by itself; they will stay stopped after the clip.
