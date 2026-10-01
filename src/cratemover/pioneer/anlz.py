"""Read and write Rekordbox analysis files (``ANLZ0000.DAT`` / ``.EXT`` / ``.2EX``).

A ``PMAI`` header followed by tagged big-endian sections. Layouts from Deep
Symmetry's analysis; the section order, constants and the analysis-folder hash
from M-Igashi/baken (MIT), which were checked against Rekordbox 6.6-7.x output:

* ``.DAT``: PPTH PVBR PQTZ PWAV PWV2 PCOB(hot A-C) PCOB(memory)
* ``.EXT``: PPTH PWV3 PCOB(hot D-H) PCOB(empty) PCO2(hot) PCO2(memory) PQT2 PWV5 PWV4
* ``.2EX``: PPTH PWV7 PWV6 PWVC
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from ..model import Cue, CueRole, TempoMarker

HEADER_TAIL = bytes([0, 0, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0])
HOT, MEMORY = 1, 0
NO_LOOP = 0xFFFFFFFF
DAT_HOT_CUES = 3  # hot cues A-C also go in the .DAT for old players


@dataclass
class Section:
    tag: str
    data: bytes  # the whole section, header included

    @staticmethod
    def build(tag: str, header_len: int, payload: bytes) -> Section:
        return Section(
            tag, tag.encode() + struct.pack(">II", header_len, 12 + len(payload)) + payload
        )


@dataclass
class AnlzFile:
    sections: list[Section] = field(default_factory=list)
    header_tail: bytes = HEADER_TAIL

    @classmethod
    def parse(cls, data: bytes) -> AnlzFile:
        if data[:4] != b"PMAI":
            raise ValueError("not a Rekordbox analysis file")
        (header_len,) = struct.unpack_from(">I", data, 4)
        f = cls(header_tail=data[12:28])
        at = header_len
        while at + 12 <= len(data):
            tag = data[at : at + 4].decode("latin-1")
            (length,) = struct.unpack_from(">I", data, at + 8)
            if length < 12 or at + length > len(data):
                raise ValueError(f"corrupt {tag} section at {at}")
            f.sections.append(Section(tag, data[at : at + length]))
            at += length
        return f

    def to_bytes(self) -> bytes:
        body = b"".join(s.data for s in self.sections)
        return b"PMAI" + struct.pack(">II", 28, 28 + len(body)) + self.header_tail + body

    def get(self, tag: str) -> Section | None:
        return next((s for s in self.sections if s.tag == tag), None)

    def all(self, tag: str) -> list[Section]:
        return [s for s in self.sections if s.tag == tag]

    @property
    def path(self) -> str:
        s = self.get("PPTH")
        if s is None:
            return ""
        (n,) = struct.unpack_from(">I", s.data, 12)
        return s.data[16 : 16 + n].decode("utf-16-be", errors="replace").rstrip("\x00")


# --- the analysis folder --------------------------------------------------------------


def anlz_dir(usb_path: str) -> str:
    """``/PIONEER/USBANLZ/Pxxx/xxxxxxxx`` for a stick path; players compute this themselves."""
    h = 0
    data = usb_path.encode("utf-16-le")
    for (unit,) in struct.iter_unpack("<H", data):
        h = (h * 0x5BC9 + unit) & 0xFFFFFFFF
        h = (h * 0x93B5 + unit) & 0xFFFFFFFF
    r = h % 200003
    p = (r & 1) | ((r >> 1) & 2) | ((r >> 4) & 4) | ((r >> 4) & 8) | ((r >> 5) & 0x10)
    p |= ((r >> 8) & 0x20) | ((r >> 10) & 0x40)
    return f"/PIONEER/USBANLZ/P{p:03X}/{r:08X}"


# --- reading --------------------------------------------------------------------------


def read_grid(f: AnlzFile) -> list[TempoMarker]:
    """Tempo sections from the PQTZ beat grid (one entry per beat, whole milliseconds)."""
    s = f.get("PQTZ")
    if s is None:
        return []
    (count,) = struct.unpack_from(">I", s.data, 20)
    grid: list[TempoMarker] = []
    since = 0
    for i in range(count):
        beat, tempo, time = struct.unpack_from(">HHI", s.data, 24 + 8 * i)
        bpm = tempo / 100.0
        if bpm <= 0:
            continue
        if grid:
            since += 1
            expected = grid[-1].position_ms + since * 60000.0 / grid[-1].bpm
            if abs(grid[-1].bpm - bpm) < 0.001 and abs(time - expected) <= 2.0:
                continue
        grid.append(TempoMarker(float(time), bpm, beat or 1))
        since = 0
    return grid


def _pcob_cues(s: Section) -> list[Cue]:
    (count,) = struct.unpack_from(">H", s.data, 18)
    cues, at = [], 24
    for _ in range(count):
        if s.data[at : at + 4] != b"PCPT":
            break
        (length,) = struct.unpack_from(">I", s.data, at + 8)
        (hot,) = struct.unpack_from(">I", s.data, at + 12)
        kind = s.data[at + 28]
        start, loop = struct.unpack_from(">II", s.data, at + 32)
        is_loop = kind == 2 and loop != NO_LOOP
        cues.append(
            Cue(
                CueRole.LOOP if is_loop else CueRole.CUE,
                float(start),
                float(loop) if is_loop else None,
                slot=hot - 1 if hot else None,
            )
        )
        at += length
    return cues


def _pco2_cues(s: Section) -> list[Cue]:
    (count,) = struct.unpack_from(">H", s.data, 16)
    cues, at = [], 20
    for _ in range(count):
        if s.data[at : at + 4] != b"PCP2":
            break
        (length,) = struct.unpack_from(">I", s.data, at + 8)
        e = s.data[at : at + length]
        (hot,) = struct.unpack_from(">I", e, 12)
        kind = e[16]
        start, loop = struct.unpack_from(">II", e, 20)
        (comment_len,) = struct.unpack_from(">I", e, 0x28) if len(e) >= 0x2C else (0,)
        name = e[0x2C : 0x2C + comment_len].decode("utf-16-be", errors="replace").rstrip("\x00")
        colour = None
        tail = 0x2C + comment_len
        if hot and len(e) >= tail + 4:
            r, g, b = e[tail + 1 : tail + 4]
            colour = (r << 16) | (g << 8) | b or None
        is_loop = kind == 2 and loop != NO_LOOP
        cues.append(
            Cue(
                CueRole.LOOP if is_loop else CueRole.CUE,
                float(start),
                float(loop) if is_loop else None,
                slot=hot - 1 if hot else None,
                name=name,
                colour=colour,
            )
        )
        at += length
    return cues


def read_cues(dat: AnlzFile | None, ext: AnlzFile | None) -> list[Cue]:
    """Cues from the .EXT's PCO2 lists (names, colours, all 8 hot cues), else the .DAT's PCOB."""
    if ext is not None and ext.all("PCO2"):
        return sorted(
            (c for s in ext.all("PCO2") for c in _pco2_cues(s)), key=lambda c: c.position_ms
        )
    cues: list[Cue] = []
    for f in (dat, ext):
        if f is not None:
            cues += [c for s in f.all("PCOB") for c in _pcob_cues(s)]
    seen: set[tuple[int | None, int]] = set()
    unique = []
    for c in sorted(cues, key=lambda c: c.position_ms):
        if (c.slot, round(c.position_ms)) not in seen:
            seen.add((c.slot, round(c.position_ms)))
            unique.append(c)
    return unique


# --- writing ---------------------------------------------------------------------------


def ppth(usb_path: str) -> Section:
    body = (usb_path + "\x00").encode("utf-16-be")
    return Section.build("PPTH", 16, struct.pack(">I", len(body)) + body)


def pvbr(mp3_frames: int | None) -> Section:
    payload = bytearray(4 + 400 * 4 + 4)
    if mp3_frames:
        struct.pack_into(">I", payload, 1604, (mp3_frames * 1152) & 0xFFFFFFFF)
    return Section.build("PVBR", 0x10, bytes(payload))


def beats(grid: list[TempoMarker], duration_ms: float) -> list[tuple[int, int, int]]:
    """(beat in bar, BPM x 100, ms) for every beat, the way baken expands TEMPO entries."""
    out = []
    for k, marker in enumerate(grid):
        if marker.bpm <= 0:
            continue
        period = 60000.0 / marker.bpm
        start, number = marker.position_ms, min(max(marker.beat, 1), 4)
        if k == 0:  # extend the grid back to the start of the track
            back = int(start // period)
            start -= back * period
            number = (number - 1 - back) % 4 + 1
        if k + 1 < len(grid):
            count = max(1, round((grid[k + 1].position_ms - start) / period))
        else:
            count = max(0, math.ceil((duration_ms - start) / period))
        for i in range(count):
            out.append((number, round(marker.bpm * 100), round(start + i * period)))
            number = number % 4 + 1
    return out


def pqtz(grid: list[TempoMarker], duration_ms: float) -> Section:
    entries = beats(grid, duration_ms)
    payload = struct.pack(">III", 0, 0x00080000, len(entries))
    payload += b"".join(struct.pack(">HHI", *e) for e in entries)
    return Section.build("PQTZ", 0x18, payload)


def pqt2_empty() -> Section:
    payload = bytearray(44)
    payload[4:8] = b"\x01\x00\x00\x02"
    return Section.build("PQT2", 0x38, bytes(payload))


def _is_loop(c: Cue) -> bool:
    return c.is_loop and c.end_ms is not None and c.end_ms > c.position_ms


def _loop_ms(c: Cue) -> int:
    return round(c.end_ms) if _is_loop(c) and c.end_ms is not None else NO_LOOP


def pcob(kind: int, cues: list[Cue]) -> Section:
    n = len(cues)
    memory_count = n - 1 if kind == MEMORY and cues else NO_LOOP
    payload = bytearray(struct.pack(">IHHI", kind, 0, n, memory_count))
    for i, c in enumerate(cues):
        hot = (c.slot or 0) + 1 if kind == HOT else 0
        if kind == HOT:
            first = last = 0xFFFF
        else:
            first = 0xFFFF if i == 0 else i - 1
            last = 0xFFFF if i + 1 == n else i + 1
        payload += b"PCPT" + struct.pack(">IIIII", 0x1C, 0x38, hot, 0, 0x10000)
        payload += struct.pack(">HHB", first, last, 2 if _is_loop(c) else 1) + b"\x00\x03\xe8"
        payload += struct.pack(">II", max(0, round(c.position_ms)), _loop_ms(c)) + bytes(16)
    return Section.build("PCOB", 0x18, bytes(payload))


def _rekordbox_cue_code(colour: int) -> int:
    from ..colours import REKORDBOX_CUE_COLOURS

    return REKORDBOX_CUE_COLOURS.index(colour) + 1 if colour in REKORDBOX_CUE_COLOURS else 0


def pco2(kind: int, cues: list[Cue], bpm: float) -> Section:
    payload = bytearray(struct.pack(">IHH", kind, len(cues), 0))
    for c in cues:
        comment = (c.name + "\x00").encode("utf-16-be") if c.name else b""
        length = 0x2C + len(comment) + 4 + 40
        e = bytearray(
            b"PCP2" + struct.pack(">III", 0x10, length, (c.slot or 0) + 1 if kind == HOT else 0)
        )
        e += bytes([2 if _is_loop(c) else 1]) + b"\x00\x03\xe8"
        e += struct.pack(">II", max(0, round(c.position_ms)), _loop_ms(c))
        e += bytes([0, 1, 0, 0, 0, 0, 0, 0])  # memory cue colour id, then constants
        num = den = 0
        if _is_loop(c) and bpm > 0 and c.end_ms is not None:
            n_beats = (c.end_ms - c.position_ms) * bpm / 60000.0
            if round(n_beats) >= 1 and abs(n_beats - round(n_beats)) < 0.02:
                num, den = round(n_beats), 1
        e += struct.pack(">HHI", num, den, len(comment)) + comment
        if kind == HOT and c.colour is not None:
            e += bytes(
                [
                    _rekordbox_cue_code(c.colour),
                    (c.colour >> 16) & 0xFF,
                    (c.colour >> 8) & 0xFF,
                    c.colour & 0xFF,
                ]
            )
        else:
            e += bytes(4)
        payload += e.ljust(length, b"\x00")
    return Section.build("PCO2", 0x14, bytes(payload))


def cue_lists(cues: list[Cue]) -> tuple[list[Cue], list[Cue]]:
    """Hot cues A-H and memory cues/loops, in the reverse order Rekordbox writes them.

    Memory cues at the same spot as another memory cue (a main cue on the intro,
    say) are written once; intro/outro markers get their name.
    """
    hot = [
        c
        for c in cues
        if c.slot is not None and c.slot < 8 and c.role in (CueRole.CUE, CueRole.LOOP)
    ]
    memory: list[Cue] = []
    seen: set[tuple[bool, int]] = set()
    for c in sorted(
        (c for c in cues if c not in hot), key=lambda c: (c.position_ms, c.role is CueRole.MAIN)
    ):
        key = (_is_loop(c), round(c.position_ms))
        if key in seen:
            continue
        seen.add(key)
        if not c.name and c.role in (CueRole.INTRO, CueRole.OUTRO):
            c = Cue(
                c.role, c.position_ms, c.end_ms, c.slot, c.role.value.title(), c.colour, c.locked
            )
        memory.append(c)
    return (
        sorted(hot, key=lambda c: c.slot or 0, reverse=True),
        sorted(memory, key=lambda c: c.position_ms, reverse=True),
    )


def preview_section(tag: str, data: bytes) -> Section:
    return Section.build(tag, 0x14, struct.pack(">II", len(data), 0x10000) + data)


def detail_section(tag: str, entry_bytes: int, unknown: int, data: bytes) -> Section:
    return Section.build(
        tag, 0x18, struct.pack(">III", entry_bytes, len(data) // entry_bytes, unknown) + data
    )
