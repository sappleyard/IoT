"""The spoken sentence, and turning it into a WAV file with Piper (offline, on the Pi).

    "British Airways 2 7 1, from Frankfurt-am-Main to London Heathrow, at 3,100 feet,
     an Airbus A three twenty."

Any part that isn't known is left out; with nothing but a callsign it says that alone.
"""

import re
import wave

from .config import CACHE_DIR, VOICE

ONES = "zero one two three four five six seven eight nine".split()
TEENS = ("ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
         "nineteen").split()
TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def two_digits(n):
    """10-99 as words, the way aircraft numbers are said: 20 -> 'twenty', 21 -> 'twenty one'."""
    if n < 10:
        return f"oh {ONES[n]}"
    if n < 20:
        return TEENS[n - 10]
    return TENS[n // 10] + (f" {ONES[n % 10]}" if n % 10 else "")


def spoken_model(model):
    """Aircraft model as people say it: A320 -> 'A three twenty', 787 -> 'seven eight seven'."""
    def say(m):
        letter, digits = m.group(1), m.group(2)
        if letter == "A" and len(digits) == 3:            # Airbus A320, A350, A380
            return f"A {ONES[int(digits[0])]} {two_digits(int(digits[1:]))}"
        if not letter and len(digits) == 3:               # Boeing 737, 777, 787
            return " ".join(ONES[int(d)] for d in digits)
        return m.group(0)
    model = re.sub(r"(\d)([A-Za-z])", r"\1 \2", model)    # "737MAX" -> "737 MAX"
    return re.sub(r"\b(A?)(\d{3})\b", say, model)


def spelled(text):
    """'271' -> '2 7 1', 'HLE27' -> 'H L E 2 7': read character by character."""
    return " ".join(text)


def sentence(d):
    """The announcement for a dict from flights.Lookups.details."""
    if d.get("airline") and d.get("flight_number"):
        who = f"{d['airline']} {spelled(d['flight_number'])}"
    elif d.get("callsign"):
        who = spelled(d["callsign"])
    else:
        who = "An unidentified aircraft"
    parts = [who]
    origin, destination = d.get("origin"), d.get("destination")
    if origin and destination:
        parts.append(f"from {origin} to {destination}")
    elif origin:
        parts.append(f"from {origin}")
    elif destination:
        parts.append(f"to {destination}")
    if d.get("alt_ft") is not None:
        parts.append(f"at {round(d['alt_ft'], -2):,.0f} feet")
    if d.get("model"):
        model = spoken_model(d["model"])
        parts.append(("an " if model[0].upper() in "AEIOU" else "a ") + model)
    return ", ".join(parts) + "."


class Voice:
    """A Piper voice, loaded once (about 3 s); each sentence then takes about 1 s."""

    def __init__(self):
        from piper import PiperVoice   # here, so the rest of the package imports without it

        path = CACHE_DIR / "voices" / f"{VOICE}.onnx"
        if not path.exists():
            raise SystemExit(f"No voice at {path}: run setup/install.sh")
        self.voice = PiperVoice.load(str(path))

    def to_wav(self, text, path):
        with wave.open(str(path), "wb") as w:
            self.voice.synthesize_wav(text, w)
