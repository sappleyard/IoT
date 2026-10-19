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
`--volume 0-100` to set the volume first. The clip replaces whatever the speaker was playing.
