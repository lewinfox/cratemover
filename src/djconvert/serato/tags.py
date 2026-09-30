"""Read and write Serato's cue and beat grid tags inside audio files.

Where Serato keeps them (Holzhaus/serato-tags ``docs/fileformats.md``):

* MP3, AIFF, WAV: ID3v2 ``GEOB`` frames named ``Serato Markers2``, ``Serato Markers_``
  and ``Serato BeatGrid``.
* FLAC: Vorbis comments ``SERATO_MARKERS_V2`` and ``SERATO_BEATGRID`` (base64).
* MP4/M4A: freeform atoms ``----:com.serato.dj:markersv2``, ``markers`` and ``beatgrid``.
* Ogg Vorbis: Vorbis comment ``SERATO_MARKERS2`` (base64 of the bare entry list).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import mutagen
from mutagen.aiff import AIFF
from mutagen.flac import FLAC
from mutagen.id3 import GEOB, ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggvorbis import OggVorbis
from mutagen.wave import WAVE

from .binfile import SeratoFormatError
from .markers import (
    V1_CUES,
    GridMarker,
    Markers2,
    SeratoCue,
    SeratoLoop,
    _b64decode_lenient,
    _b64encode_serato,
    decode_beatgrid,
    decode_beatgrid_b64,
    decode_markers1_id3,
    decode_markers2_b64,
    decode_markers2_id3,
    dump_markers2_payload,
    encode_beatgrid,
    encode_beatgrid_b64,
    encode_markers1_id3,
    encode_markers1_mp4,
    encode_markers2_b64,
    encode_markers2_id3,
    parse_markers2_payload,
)

log = logging.getLogger(__name__)

ID3_TYPES = (".mp3", ".aif", ".aiff", ".wav")
SUPPORTED = (*ID3_TYPES, ".flac", ".m4a", ".mp4", ".ogg")

_MP4_MARKERS2 = "----:com.serato.dj:markersv2"
_MP4_MARKERS1 = "----:com.serato.dj:markers"
_MP4_BEATGRID = "----:com.serato.dj:beatgrid"


@dataclass
class SeratoTrackTags:
    markers: Markers2 = field(default_factory=Markers2)
    grid: list[GridMarker] = field(default_factory=list)
    found: bool = False  # any Serato tag was present


def _merge_v1(markers: Markers2, v1: tuple[list[SeratoCue], list[SeratoLoop]]) -> None:
    """Apply ``Serato Markers_`` over Markers2 the way Serato does (Mixxx ``getCueInfos``).

    Markers_ wins for the first 5 cues and 9 loops; a cue in Markers2 whose
    Markers_ slot is empty counts as unset.
    """
    v1_cues, v1_loops = v1
    by_index = {c.index: c for c in markers.cues}
    for index in range(V1_CUES):
        by_index.pop(index, None)
    for cue in v1_cues:
        old = next((c for c in markers.cues if c.index == cue.index), None)
        cue.name = old.name if old else ""
        by_index[cue.index] = cue
    markers.cues = sorted(by_index.values(), key=lambda c: c.index)
    loops = {lp.index: lp for lp in markers.loops}
    for loop in v1_loops:
        old = loops.get(loop.index)
        loop.name = old.name if old else ""
        loop.colour = old.colour if old else loop.colour
        loops[loop.index] = loop
    markers.loops = sorted(loops.values(), key=lambda lp: lp.index)


def _open(path: Path) -> mutagen.FileType | None:
    suffix = path.suffix.lower()
    cls = {
        ".mp3": MP3,
        ".aif": AIFF,
        ".aiff": AIFF,
        ".wav": WAVE,
        ".flac": FLAC,
        ".m4a": MP4,
        ".mp4": MP4,
        ".ogg": OggVorbis,
    }.get(suffix)
    return cls(path) if cls else None


def read_tags(path: Path) -> SeratoTrackTags:
    """Read Serato data from ``path``. Raises OSError / mutagen errors for unreadable files."""
    result = SeratoTrackTags()
    audio = _open(path)
    if audio is None or audio.tags is None:
        return result
    suffix = path.suffix.lower()
    try:
        if suffix in ID3_TYPES:
            tags = audio.tags
            if frame := tags.get("GEOB:Serato Markers2"):
                result.markers = decode_markers2_id3(frame.data)
                result.found = True
            if frame := tags.get("GEOB:Serato Markers_"):
                _merge_v1(result.markers, decode_markers1_id3(frame.data))
                result.found = True
            if frame := tags.get("GEOB:Serato BeatGrid"):
                result.grid = decode_beatgrid(frame.data)
                result.found = True
        elif suffix == ".flac":
            if values := audio.tags.get("SERATO_MARKERS_V2"):
                result.markers = decode_markers2_b64(values[0].encode())
                result.found = True
            if values := audio.tags.get("SERATO_BEATGRID"):
                result.grid = decode_beatgrid_b64(values[0].encode())
                result.found = True
        elif suffix in (".m4a", ".mp4"):
            if values := audio.tags.get(_MP4_MARKERS2):
                result.markers = decode_markers2_b64(bytes(values[0]))
                result.found = True
            if values := audio.tags.get(_MP4_BEATGRID):
                result.grid = decode_beatgrid_b64(bytes(values[0]))
                result.found = True
        elif suffix == ".ogg":
            if values := audio.tags.get("SERATO_MARKERS2"):
                result.markers = parse_markers2_payload(_b64decode_lenient(values[0].encode()))
                result.found = True
    except (SeratoFormatError, ValueError, IndexError) as exc:
        raise SeratoFormatError(f"unreadable Serato tags: {exc}") from exc
    return result


def write_tags(path: Path, markers: Markers2, grid: list[GridMarker]) -> None:
    """Replace the Serato cue and grid tags in ``path``, keeping everything else.

    Unknown Markers2 entries already in the file (Flip performances) are kept.
    """
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"Serato tags are not supported for {suffix} files")
    try:
        existing = read_tags(path).markers
        markers.other = markers.other or existing.other
    except SeratoFormatError:
        pass

    if suffix in ID3_TYPES:
        _write_id3(path, markers, grid)
        return
    audio = _open(path)
    assert audio is not None
    if audio.tags is None:
        audio.add_tags()
    if suffix == ".flac":
        audio.tags["SERATO_MARKERS_V2"] = encode_markers2_b64(markers).decode()
        if grid:
            audio.tags["SERATO_BEATGRID"] = encode_beatgrid_b64(grid).decode()
        elif "SERATO_BEATGRID" in audio.tags:
            del audio.tags["SERATO_BEATGRID"]
    elif suffix in (".m4a", ".mp4"):
        audio.tags[_MP4_MARKERS2] = [MP4FreeForm(encode_markers2_b64(markers))]
        audio.tags[_MP4_MARKERS1] = [MP4FreeForm(encode_markers1_mp4(markers))]
        if grid:
            audio.tags[_MP4_BEATGRID] = [MP4FreeForm(encode_beatgrid_b64(grid))]
        elif _MP4_BEATGRID in audio.tags:
            del audio.tags[_MP4_BEATGRID]
    elif suffix == ".ogg":
        payload = dump_markers2_payload(markers)
        audio.tags["SERATO_MARKERS2"] = _b64encode_serato(payload, chop_padding=True).decode()
    audio.save()


def _write_id3(path: Path, markers: Markers2, grid: list[GridMarker]) -> None:
    suffix = path.suffix.lower()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
    else:
        audio = _open(path)
        assert audio is not None
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

    def geob(name: str, data: bytes) -> None:
        tags.setall(
            f"GEOB:{name}",
            [GEOB(encoding=0, mime="application/octet-stream", filename="", desc=name, data=data)],
        )

    geob("Serato Markers2", encode_markers2_id3(markers))
    geob("Serato Markers_", encode_markers1_id3(markers))
    if grid:
        geob("Serato BeatGrid", encode_beatgrid(grid))
    else:
        tags.delall("GEOB:Serato BeatGrid")
    if suffix == ".mp3":
        # Serato writes ID3v2.4; keep v2.3 files as they are.
        tags.save(path, v2_version=4 if tags.version >= (2, 4, 0) else 3)
    else:
        audio.save()
