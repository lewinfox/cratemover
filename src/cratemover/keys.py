"""Musical keys in the notations the three programs use."""

from __future__ import annotations

import re
from enum import StrEnum

from .model import Key


class KeyNotation(StrEnum):
    CAMELOT = "camelot"  # 8A (also called Lancelot in Mixxx)
    OPEN_KEY = "open_key"  # 1m
    MUSICAL = "musical"  # Am


_NOTES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_PITCH = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Camelot number for each tonic, major (B) and minor (A).
_CAMELOT_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}
_CAMELOT_MINOR = {9: 8, 4: 9, 11: 10, 6: 11, 1: 12, 8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7}
_FROM_CAMELOT = {(n, False): t for t, n in _CAMELOT_MAJOR.items()} | {
    (n, True): t for t, n in _CAMELOT_MINOR.items()
}

_CAMELOT_RE = re.compile(r"^\s*0?(\d{1,2})\s*([ABab])\s*$")
_OPEN_KEY_RE = re.compile(r"^\s*(\d{1,2})\s*([mdMD])\s*$")
_MUSICAL_RE = re.compile(
    r"^\s*([A-Ga-g])\s*(#|♯|b|♭|sharp|flat)?\s*(m|min|minor|maj|major|M)?\s*$", re.IGNORECASE
)


def parse_key(text: str | None) -> Key | None:
    """Parse Camelot (``8A``), Open Key (``1m``) or musical (``Am``, ``F#min``, ``Bb``) keys."""
    if not text:
        return None
    text = text.strip()
    if match := _CAMELOT_RE.match(text):
        number, letter = int(match.group(1)), match.group(2).upper()
        tonic = _FROM_CAMELOT.get((number, letter == "A"))
        return Key(tonic, letter == "A") if tonic is not None else None
    if match := _OPEN_KEY_RE.match(text):
        # Open Key 1 = Camelot 8, advancing together.
        number, minor = int(match.group(1)), match.group(2).lower() == "m"
        if not 1 <= number <= 12:
            return None
        return Key(_FROM_CAMELOT[((number + 6) % 12 + 1, minor)], minor)
    if match := _MUSICAL_RE.match(text):
        note, accidental, quality = match.groups()
        tonic = _PITCH[note.upper()]
        if accidental:
            tonic += 1 if accidental.lower() in ("#", "♯", "sharp") else -1
        # A bare "M" is major; "m", "min", "minor" are minor.
        minor = bool(quality) and quality != "M" and quality.lower() in ("m", "min", "minor")
        return Key(tonic % 12, minor)
    return None


def format_key(key: Key | None, notation: KeyNotation) -> str:
    if key is None:
        return ""
    if notation is KeyNotation.MUSICAL:
        return _NOTES[key.tonic] + ("m" if key.minor else "")
    number = (_CAMELOT_MINOR if key.minor else _CAMELOT_MAJOR)[key.tonic]
    if notation is KeyNotation.CAMELOT:
        return f"{number}{'A' if key.minor else 'B'}"
    return f"{(number + 4) % 12 + 1}{'m' if key.minor else 'd'}"


# Mixxx's ChromaticKey enum (library.key_id): 1-12 major from C, 13-24 minor from C.
def mixxx_key_id(key: Key | None) -> int | None:
    if key is None:
        return None
    return key.tonic + (13 if key.minor else 1)


def key_from_mixxx_id(key_id: int | None) -> Key | None:
    if not key_id or not 1 <= key_id <= 24:
        return None
    return Key((key_id - 1) % 12, key_id > 12)
