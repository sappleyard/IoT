"""The spoken sentence, and turning it into a WAV file with Piper (offline, on the Pi).

    "British Airways 2 7 1, from Frankfurt-am-Main to London Heathrow, at 3,100 feet,
     an Airbus Ay three twenty."

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


def digit_by_digit(digits):
    """'109' -> 'one oh nine'."""
    return " ".join("oh" if d == "0" else ONES[int(d)] for d in digits)


def spoken_model(model):
    """Aircraft model as people say it: A320 -> 'Ay three twenty', A300 -> 'Ay three hundred',
    E190 -> 'E one ninety', 787 -> 'seven eight seven', AW139 -> 'A W one three nine'."""
    def say(m):
        letters, digits = m.group(1), m.group(2)
        if (letters, digits[0]) in (("A", "2"), ("A", "3"), ("E", "1")):    # Airbus, Embraer
            rest = "hundred" if digits[1:] == "00" else two_digits(int(digits[1:]))
            letters = "Ay" if letters == "A" else letters    # a lone "A" is read as "uh"
            return f"{letters} {ONES[int(digits[0])]} {rest}"
        return " ".join([*letters, digit_by_digit(digits)])     # Boeing, helicopters
    model = re.sub(r"(\d)([A-Za-z])", r"\1 \2", model)    # "737MAX" -> "737 MAX"
    return re.sub(r"\b([A-Z]{0,3})(\d{3})\b", say, model)


def article(words):
    """'a' or 'an' by how the words sound: an Airbus, a Boeing, a Eurocopter, a Leonardo."""
    first = words.lower()
    return "a" if first.startswith(("eu", "uni", "one")) or first[0] not in "aeiou" else "an"


def spelled(text):
    """'271' -> '2 7 1', 'HLE27' -> 'H L E 2 7': read character by character."""
    return " ".join(text)


def sentence(d):
    """The announcement for a dict from flights.Lookups.details."""
    if d.get("airline") and d.get("flight_number"):
        # "J C Bamford (Excavators)" -> "J C Bamford"
        airline = re.sub(r"\s*\([^)]*\)", "", d["airline"]).strip()
        who = f"{airline} {spelled(d['flight_number'])}"
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
        parts.append(f"{article(model)} {model}")
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
