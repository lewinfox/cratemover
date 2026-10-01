"""Decoder offsets between each program and the reference timeline (Rekordbox's).

Mixxx skips some leading audio that Rekordbox and Serato play (MP3 encoder
delay depending on the LAME/Xing header, AAC priming samples), so a cue at the
same timestamp lands on different audio. :func:`mixxx_offset_ms` gives the
milliseconds to *add* to a Mixxx position to get the reference position.

MP3 measurements from https://github.com/FrankwaP/mixxx-utils (via the
MixxxToRekordbox forks); Mixxx's own Serato importer uses the same ~26 ms
for MAD/FFmpeg (``src/track/serato/tags.cpp``). MP4 handling from
``mixxx-to-rekordbox`` in this repo.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal

from mutagen.mp3 import HeaderNotFoundError, MPEGFrame
from mutagen.mp3._util import XingHeader, XingHeaderError

Mp3Decoder = Literal["MAD", "CoreAudio", "FFmpeg"]
MP3_DECODERS: tuple[Mp3Decoder, ...] = ("MAD", "CoreAudio", "FFmpeg")

# Offset per Mixxx MP3 decoder and header case (ms):
#   A: no Xing/Info header, B: Xing without LAME tag,
#   C: LAME tag without a valid music CRC, D: LAME tag with CRC.
# Mixxx's own Rekordbox importer (src/library/rekordbox/rekordboxfeature.cpp) uses these.
_MP3_OFFSETS: dict[str, dict[str, float]] = {
    "MAD": {"A": 26, "D": 26},
    "CoreAudio": {"A": 12, "B": 13, "C": 26, "D": 50},
    "FFmpeg": {"D": 26},
}


@dataclass(frozen=True)
class Offset:
    ms: float
    warning: str | None = None


def _first_frame(f: BinaryIO) -> MPEGFrame:
    header = f.read(10)
    start = 0
    if header[:3] == b"ID3":
        size = 0
        for byte in header[6:10]:
            size = (size << 7) | (byte & 0x7F)
        start = 10 + size + (10 if header[5] & 0x10 else 0)
    f.seek(start)
    data = f.read(64 * 1024)
    for i in range(len(data) - 4):
        if data[i] == 0xFF and data[i + 1] & 0xE0 == 0xE0:
            f.seek(start + i)
            try:
                return MPEGFrame(f)
            except HeaderNotFoundError:
                continue
    raise HeaderNotFoundError("no MPEG frame found")


def mp3_header_case(path: Path) -> Literal["A", "B", "C", "D"]:
    with open(path, "rb") as f:
        frame = _first_frame(f)
        f.seek(frame.frame_offset + XingHeader.get_offset(frame))
        try:
            xing = XingHeader(f)
        except XingHeaderError:
            return "A"
    if xing.lame_header is None:
        return "B"
    return "D" if xing.lame_header.music_crc > 0 else "C"


def mp3_offset_ms(path: Path, decoder: Mp3Decoder) -> float:
    return _MP3_OFFSETS[decoder].get(mp3_header_case(path), 0)


# --- MP4 -------------------------------------------------------------------------

DEFAULT_AAC_PRIMING_SAMPLES = 2112  # iTunes encoder default
FALLBACK_MP4_OFFSET_MS = 48


def _iter_boxes(f: BinaryIO, start: int, end: int) -> Iterator[tuple[str, int, int]]:
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, box_type = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            (size,) = struct.unpack(">Q", f.read(8))
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError(f"invalid MP4 box size {size}")
        yield box_type.decode("latin-1"), pos + header, min(pos + size, end)
        pos += size


def _find_box(f: BinaryIO, start: int, end: int, *path: str) -> tuple[int, int] | None:
    for box_type, box_start, box_end in _iter_boxes(f, start, end):
        if box_type == path[0]:
            return (
                (box_start, box_end)
                if len(path) == 1
                else _find_box(f, box_start, box_end, *path[1:])
            )
    return None


def _read(f: BinaryIO, box: tuple[int, int]) -> bytes:
    f.seek(box[0])
    return f.read(box[1] - box[0])


def mp4_priming_ms(path: Path) -> float:
    """Encoder delay of the first audio track: edit list, else iTunSMPB, else the AAC default."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        moov = _find_box(f, 0, f.tell(), "moov")
        if not moov:
            raise ValueError("no moov box")
        for box_type, trak_start, trak_end in _iter_boxes(f, *moov):
            if box_type != "trak":
                continue
            hdlr = _find_box(f, trak_start, trak_end, "mdia", "hdlr")
            if not hdlr or _read(f, hdlr)[8:12] != b"soun":
                continue
            mdhd = _read(f, _find_box(f, trak_start, trak_end, "mdia", "mdhd") or (0, 0))
            timescale = struct.unpack_from(">I", mdhd, 20 if mdhd[0] == 1 else 12)[0]
            stsd = _find_box(f, trak_start, trak_end, "mdia", "minf", "stbl", "stsd")
            codec = _read(f, stsd)[12:16] if stsd else b""
            priming = 0
            if elst := _find_box(f, trak_start, trak_end, "edts", "elst"):
                data = _read(f, elst)
                (count,) = struct.unpack_from(">I", data, 4)
                offset = 8
                for _ in range(count):
                    if data[0] == 1:
                        _, media_time, _ = struct.unpack_from(">QqI", data, offset)
                        offset += 20
                    else:
                        _, media_time, _ = struct.unpack_from(">IiI", data, offset)
                        offset += 12
                    if media_time != -1:
                        priming = media_time
                        break
            if not priming and codec == b"mp4a":
                priming = DEFAULT_AAC_PRIMING_SAMPLES
            return priming * 1000.0 / timescale if timescale else 0.0
    raise ValueError("no audio track")


def serato_offset_ms(path: Path | None, extension: str) -> float:
    """Milliseconds Serato places MP3 cues later than Rekordbox (Serato = reference + this).

    Mixxx's two importers disagree by exactly one MPEG frame for MP3s with a
    Xing/Info header but no valid LAME tag (cases B and C): with MAD, Mixxx is
    26 ms behind Serato for every MP3 (24 ms at 48 kHz; ``src/track/serato/tags.cpp``)
    but behind Rekordbox only for cases A and D (``rekordboxfeature.cpp``).
    So Serato counts that Info frame and Rekordbox doesn't.
    """
    if extension != "mp3" or path is None:
        return 0.0
    try:
        if mp3_header_case(path) not in ("B", "C"):
            return 0.0
        with open(path, "rb") as f:
            rate = _first_frame(f).sample_rate
        return 1152 * 1000.0 / rate
    except Exception:
        return 0.0


def mixxx_offset_ms(path: Path | None, extension: str, decoder: Mp3Decoder = "MAD") -> Offset:
    """Milliseconds to add to Mixxx positions to reach the reference (Rekordbox) timeline."""
    if extension in ("m4a", "mp4", "aac"):
        if path is None:
            return Offset(FALLBACK_MP4_OFFSET_MS, "file not found; assumed standard AAC priming")
        try:
            return Offset(mp4_priming_ms(path))
        except Exception as exc:
            return Offset(FALLBACK_MP4_OFFSET_MS, f"could not read MP4 encoder delay: {exc}")
    if extension == "mp3":
        if path is None:
            return Offset(_MP3_OFFSETS[decoder].get("D", 0), "file not found; assumed a LAME MP3")
        try:
            return Offset(mp3_offset_ms(path, decoder))
        except Exception as exc:
            return Offset(0, f"could not read MP3 headers: {exc}")
    return Offset(0)
