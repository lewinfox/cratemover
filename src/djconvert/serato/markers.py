"""Encode and decode the Serato cue and beat grid tags.

Layouts are from Holzhaus/serato-tags (docs/) and Mixxx's
``src/track/serato/{markers,markers2,beatgrid}.cpp``:

* ``Serato Markers2``: hot cues, saved loops, track colour, BPM lock. Stored
  as base64 with a line break every 72 characters.
* ``Serato Markers_``: an older fixed layout with the first 5 cues and 9
  loops. Serato prefers it over Markers2 where both exist, so we write both.
* ``Serato BeatGrid``: beat grid markers (float32 seconds).

Positions here are in milliseconds (Markers) or seconds (BeatGrid), exactly as
stored. Colours are the *stored* values; see :mod:`djconvert.colours`.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field

from .binfile import SeratoFormatError

# --- Markers2 --------------------------------------------------------------------


@dataclass
class SeratoCue:
    index: int
    position_ms: int
    colour: int  # stored colour
    name: str = ""


@dataclass
class SeratoLoop:
    index: int
    start_ms: int
    end_ms: int
    colour: int = 0x27AAE1
    locked: bool = False
    name: str = ""


@dataclass
class Markers2:
    cues: list[SeratoCue] = field(default_factory=list)
    loops: list[SeratoLoop] = field(default_factory=list)
    track_colour: int | None = None  # stored colour
    bpm_locked: bool | None = None
    other: list[tuple[str, bytes]] = field(default_factory=list)  # FLIP etc., kept verbatim


def _b64decode_lenient(text: bytes) -> bytes:
    text = bytes(c for c in text if c not in b"\n\r\x00 ")
    # Serato sometimes writes one character more than a multiple of four.
    if len(text) % 4 == 1:
        text += b"A"
    return base64.b64decode(text + b"=" * (-len(text) % 4))


def _b64encode_serato(data: bytes, chop_padding: bool) -> bytes:
    """Base64 with a newline every 72 characters, as Serato writes it (Mixxx ``base64encode``)."""
    out = bytearray()
    for offset in range(0, len(data), 54):
        if offset:
            out += b"\n"
        block = data[offset : offset + 54]
        out += base64.b64encode(block).rstrip(b"=")
        if chop_padding and len(block) % 3:
            del out[-1]
    return bytes(out)


def _cstring(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("utf-8", errors="replace"), end + 1


def parse_markers2_payload(payload: bytes) -> Markers2:
    """Parse the decoded Markers2 entry list (starts with ``01 01``)."""
    if payload[:2] != b"\x01\x01":
        raise SeratoFormatError("unexpected Markers2 version")
    markers = Markers2()
    pos = 2
    while pos < len(payload) and payload[pos] != 0:
        name, pos = _cstring(payload, pos)
        (length,) = struct.unpack_from(">I", payload, pos)
        pos += 4
        data = payload[pos : pos + length]
        pos += length
        if name == "CUE" and len(data) >= 13:
            index, position = struct.unpack_from(">BI", data, 1)
            colour = int.from_bytes(data[7:10], "big")
            label, _ = _cstring(data, 12)
            markers.cues.append(SeratoCue(index, position, colour, label))
        elif name == "LOOP" and len(data) >= 21:
            index, start, end = struct.unpack_from(">BII", data, 1)
            colour = int.from_bytes(data[15:18], "big")
            label, _ = _cstring(data, 20)
            markers.loops.append(SeratoLoop(index, start, end, colour, bool(data[19]), label))
        elif name == "COLOR" and len(data) == 4:
            markers.track_colour = int.from_bytes(data[1:4], "big")
        elif name == "BPMLOCK" and len(data) == 1:
            markers.bpm_locked = bool(data[0])
        else:
            markers.other.append((name, data))
    return markers


def dump_markers2_payload(markers: Markers2) -> bytes:
    entries: list[tuple[str, bytes]] = []
    if markers.track_colour is not None:
        entries.append(("COLOR", b"\x00" + markers.track_colour.to_bytes(3, "big")))
    for cue in sorted(markers.cues, key=lambda c: c.index):
        data = struct.pack(">BBIB", 0, cue.index, max(0, cue.position_ms), 0)
        data += cue.colour.to_bytes(3, "big") + b"\x00\x00" + cue.name.encode() + b"\x00"
        entries.append(("CUE", data))
    for loop in sorted(markers.loops, key=lambda lp: lp.index):
        data = struct.pack(">BBII", 0, loop.index, max(0, loop.start_ms), max(0, loop.end_ms))
        data += b"\xff\xff\xff\xff\x00" + loop.colour.to_bytes(3, "big")
        data += bytes([0, loop.locked]) + loop.name.encode() + b"\x00"
        entries.append(("LOOP", data))
    entries += markers.other
    if markers.bpm_locked is not None:
        entries.append(("BPMLOCK", bytes([markers.bpm_locked])))
    out = bytearray(b"\x01\x01")
    for name, data in entries:
        out += name.encode("ascii") + b"\x00" + struct.pack(">I", len(data)) + data
    out += b"\x00"
    return bytes(out)


MARKERS2_MIN_SIZE = 470  # Serato pads the tag to at least this many bytes


def decode_markers2_id3(tag: bytes) -> Markers2:
    """The GEOB payload used in MP3 and AIFF files."""
    if tag[:2] != b"\x01\x01":
        raise SeratoFormatError("unexpected Markers2 tag header")
    return parse_markers2_payload(_b64decode_lenient(tag[2:].split(b"\x00", 1)[0]))


def encode_markers2_id3(markers: Markers2) -> bytes:
    data = b"\x01\x01" + _b64encode_serato(dump_markers2_payload(markers), chop_padding=True)
    return data.ljust(max(MARKERS2_MIN_SIZE, len(data) + 1), b"\x00")


_PREFIX = b"application/octet-stream\x00\x00"


def _unwrap(text: bytes, name: bytes) -> bytes:
    """Decode the outer base64 of FLAC/MP4 values and strip the MIME/name prefix."""
    decoded = _b64decode_lenient(text)
    prefix = _PREFIX + name + b"\x00"
    if not decoded.startswith(prefix):
        raise SeratoFormatError(f"unexpected {name.decode()} prefix")
    return decoded[len(prefix) :]


def _wrap(payload: bytes, name: bytes, *, newlines: bool) -> bytes:
    data = _PREFIX + name + b"\x00" + payload
    if newlines:
        return _b64encode_serato(data, chop_padding=False)
    return base64.b64encode(data).rstrip(b"=")


def decode_markers2_b64(text: bytes) -> Markers2:
    """FLAC ``SERATO_MARKERS_V2`` and MP4 ``markersv2`` values."""
    return decode_markers2_id3(_unwrap(text, b"Serato Markers2"))


def encode_markers2_b64(markers: Markers2) -> bytes:
    inner = b"\x01\x01" + _b64encode_serato(dump_markers2_payload(markers), chop_padding=True)
    size = max(MARKERS2_MIN_SIZE, len(_PREFIX) + 16 + len(inner) + 1)
    padded = (_PREFIX + b"Serato Markers2\x00" + inner).ljust(size, b"\x00")
    return _b64encode_serato(padded, chop_padding=False)


# --- Markers_ (v1) ------------------------------------------------------------------

V1_CUES, V1_LOOPS = 5, 9
_V1_UNSET = 0x7F7F7F7F


def _to_serato32(value: int) -> int:
    a, b, c = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    z = c & 0x7F
    y = ((c >> 7) | (b << 1)) & 0x7F
    x = ((b >> 6) | (a << 2)) & 0x7F
    w = a >> 5
    return (w << 24) | (x << 16) | (y << 8) | z


def _from_serato32(value: int) -> int:
    w, x, y, z = (value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    c = (z & 0x7F) | ((y & 0x01) << 7)
    b = ((y & 0x7F) >> 1) | ((x & 0x03) << 6)
    a = ((x & 0x7F) >> 2) | ((w & 0x07) << 5)
    return (a << 16) | (b << 8) | c


def encode_markers1_id3(markers: Markers2) -> bytes:
    """``Serato Markers_`` for MP3/AIFF, mirroring the cues and loops in ``markers``."""
    out = bytearray(struct.pack(">HI", 0x0205, V1_CUES + V1_LOOPS))
    cues = {c.index: c for c in markers.cues}
    loops = {lp.index: lp for lp in markers.loops}
    for i in range(V1_CUES):
        if cue := cues.get(i):
            out += struct.pack(">BIBI", 0, _to_serato32(cue.position_ms), 0x7F, _V1_UNSET)
            out += b"\x00\x7f\x7f\x7f\x7f\x7f" + struct.pack(">IBB", _to_serato32(cue.colour), 1, 0)
        else:
            out += struct.pack(">BIBI", 0x7F, _V1_UNSET, 0x7F, _V1_UNSET)
            out += b"\x00\x7f\x7f\x7f\x7f\x7f" + struct.pack(">IBB", 0, 0, 0)
    for i in range(V1_LOOPS):
        if loop := loops.get(i):
            out += struct.pack(
                ">BIBI", 0, _to_serato32(loop.start_ms), 0, _to_serato32(loop.end_ms)
            )
            out += b"\x00\x7f\x7f\x7f\x7f\x7f"
            out += struct.pack(">IBB", _to_serato32(0x27AAE1), 3, loop.locked)
        else:
            out += struct.pack(">BIBI", 0x7F, _V1_UNSET, 0x7F, _V1_UNSET)
            out += b"\x00\x7f\x7f\x7f\x7f\x7f" + struct.pack(">IBB", 0, 3, 0)
    colour = 0xFFFFFF if markers.track_colour is None else markers.track_colour
    out += struct.pack(">I", _to_serato32(colour))
    return bytes(out)


def decode_markers1_id3(data: bytes) -> tuple[list[SeratoCue], list[SeratoLoop]]:
    version, count = struct.unpack_from(">HI", data)
    if version != 0x0205:
        raise SeratoFormatError("unexpected Markers_ version")
    cues, loops = [], []
    for i in range(count):
        entry = data[6 + 22 * i : 6 + 22 * (i + 1)]
        if len(entry) < 22:
            break
        has_start, start, has_end, end = struct.unpack_from(">BIBI", entry)
        colour, kind, locked = struct.unpack_from(">IBB", entry, 16)
        if has_start == 0x7F:
            continue
        if kind == 1:
            cues.append(SeratoCue(i, _from_serato32(start), _from_serato32(colour)))
        elif kind == 3 and has_end != 0x7F:
            loops.append(
                SeratoLoop(
                    i - V1_CUES, _from_serato32(start), _from_serato32(end), locked=bool(locked)
                )
            )
    return cues, loops


def encode_markers1_mp4(markers: Markers2) -> bytes:
    """MP4 ``markers`` atom value: the v1 layout with plain integers, base64-wrapped."""
    out = bytearray(struct.pack(">HI", 0x0205, V1_CUES + V1_LOOPS))
    cues = {c.index: c for c in markers.cues}
    loops = {lp.index: lp for lp in markers.loops}
    for i in range(V1_CUES):
        cue = cues.get(i)
        if cue:
            out += struct.pack(">II", cue.position_ms, _V1_UNSET)
            out += b"\x00\xff\xff\xff\xff\x00" + cue.colour.to_bytes(3, "big") + bytes([1, 0])
        else:
            out += struct.pack(">II", 0xFFFFFFFF, 0xFFFFFFFF)
            out += b"\x00\xff\xff\xff\xff\x00" + bytes(3) + bytes([0, 0])
    for i in range(V1_LOOPS):
        loop = loops.get(i)
        if loop:
            out += struct.pack(">II", loop.start_ms, loop.end_ms)
            out += b"\x00\xff\xff\xff\xff\x00" + (0x27AAE1).to_bytes(3, "big")
            out += bytes([3, loop.locked])
        else:
            out += struct.pack(">II", 0xFFFFFFFF, 0xFFFFFFFF)
            out += b"\x00\xff\xff\xff\xff\x00" + bytes(3) + bytes([3, 0])
    colour = 0xFFFFFF if markers.track_colour is None else markers.track_colour
    out += b"\x00" + colour.to_bytes(3, "big")
    return _wrap(bytes(out), b"Serato Markers_", newlines=True)


# --- BeatGrid ------------------------------------------------------------------------


@dataclass
class GridMarker:
    position_s: float
    bpm: float | None = None  # terminal marker
    beats_to_next: int | None = None  # non-terminal markers


def decode_beatgrid(data: bytes) -> list[GridMarker]:
    if len(data) < 6 or data[:2] != b"\x01\x00":
        raise SeratoFormatError("unexpected BeatGrid header")
    (count,) = struct.unpack_from(">I", data, 2)
    markers = []
    for i in range(count):
        pos = 6 + 8 * i
        if pos + 8 > len(data):
            raise SeratoFormatError("truncated BeatGrid")
        (position,) = struct.unpack_from(">f", data, pos)
        if i == count - 1:
            (bpm,) = struct.unpack_from(">f", data, pos + 4)
            markers.append(GridMarker(position, bpm=bpm))
        else:
            (beats,) = struct.unpack_from(">I", data, pos + 4)
            markers.append(GridMarker(position, beats_to_next=beats))
    return markers


def encode_beatgrid(markers: list[GridMarker], footer: int = 0) -> bytes:
    out = bytearray(struct.pack(">HI", 0x0100, len(markers)))
    for i, marker in enumerate(markers):
        if i == len(markers) - 1:
            out += struct.pack(">ff", marker.position_s, marker.bpm or 0.0)
        else:
            out += struct.pack(">fI", marker.position_s, marker.beats_to_next or 0)
    out.append(footer)
    return bytes(out)


def decode_beatgrid_b64(text: bytes) -> list[GridMarker]:
    # The value carries one extra character after the base64 data.
    return decode_beatgrid(_unwrap(text[:-1], b"Serato BeatGrid"))


def encode_beatgrid_b64(markers: list[GridMarker]) -> bytes:
    return _wrap(encode_beatgrid(markers), b"Serato BeatGrid", newlines=True) + b"A"
