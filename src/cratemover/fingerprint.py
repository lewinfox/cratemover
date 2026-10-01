"""A library reduced to what two formats can both hold, so a conversion can be checked exactly.

``canonical`` turns a :class:`Library` into plain, sorted data and ``fingerprint`` hashes it.
A conversion from format A to format B is right when the source, reduced and mapped the way
B stores things (:func:`expected`), and the result read back from B (:func:`canonical`) have
the same fingerprint. ``differences`` says what doesn't match when they don't.

Tracks are keyed by their audio file relative to the drive, without its extension, so a file
converted to MP3 still lines up with its original. Tracks whose audio isn't on the drive are
left out: a conversion can't carry them.

Colours (of tracks and cues) aren't compared: each program has its own palette, so they drift
to the nearest colour by design. Timings are compared to the millisecond. Everything else is
compared exactly, except where a format can't hold something. Each exception
is written out in :func:`rules` with its reason. Add one only when a test shows a real format
limit, never to make a check pass.

``SCHEMA`` is part of every canonical form and so of every fingerprint. Bump it whenever the
canonical form or the rules change, so fingerprints made under different rules never compare
equal by accident.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import grid as gridlib
from .model import Cue, CueRole, Format, Library, TempoMarker, Track

SCHEMA = 1

_TRACK_FIELDS = (
    "title", "artist", "album", "album_artist", "genre", "composer", "grouping", "comment",
    "label", "remixer", "year", "track_number", "bpm", "key", "rating", "play_count",
    "date_added",
)  # fmt: skip


@dataclass(frozen=True)
class Rules:
    """How a library is reduced for one direction of conversion."""

    source: Format
    target: Format
    track_fields: tuple[str, ...] = _TRACK_FIELDS
    hot_cue_slots: int = 8  # hot cue slots both formats have (A-H)
    grid_beats: bool = True  # whether the target keeps each grid marker's beat-in-bar
    # Milliseconds the writer moves a track's cues and grid when it converts the audio to MP3
    # (LAME's encoder delay). Applied to the source when a track's extension changes.
    transcode_shift_ms: float = 0.0


def rules(source: Format, target: Format) -> Rules:
    """The rules for checking a conversion from ``source`` to ``target``."""
    from .pioneer.usb import TRANSCODE_OFFSET_MS

    r = Rules(source, target)
    if Format.SERATO in (source, target):
        r = replace(
            r,
            # Serato has no star rating. Its play count field (utpc) is a guess at what Serato
            # means by it, so cratemover doesn't write it.
            track_fields=tuple(f for f in r.track_fields if f not in ("rating", "play_count")),
        )
    if target == Format.SERATO:
        # Serato's beat grid markers sit on downbeats and don't store beat-in-bar, so a later
        # marker that falls mid-bar loses its place in the bar.
        r = replace(r, grid_beats=False)
    if target == Format.REKORDBOX_USB:
        r = replace(
            r,
            transcode_shift_ms=TRANSCODE_OFFSET_MS,
            # A Rekordbox stick has no grouping: neither export.pdb's track row nor OneLibrary's
            # content table has the field (docs/rekordbox-usb-format.md).
            track_fields=tuple(f for f in r.track_fields if f != "grouping"),
        )
    return r


# --- reducing a library ---------------------------------------------------------------------


def track_key(track: Track, root: Path) -> str:
    """A track's audio file on the drive, without its extension."""
    location = _local(track, root)
    with contextlib.suppress(ValueError):
        location = location.relative_to(root)
    return str(location.with_suffix("")).lower()


def _local(track: Track, root: Path) -> Path:
    location = Path(track.extra.get("local") or track.location)
    if not location.is_absolute() or root not in location.parents:
        location = root / str(track.location).lstrip("/\\")
    return location


def _value(v: Any) -> Any:
    if isinstance(v, float):
        return round(v, 3)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "__dataclass_fields__"):
        return {k: _value(getattr(v, k)) for k in v.__dataclass_fields__}
    if isinstance(v, (list, tuple)):
        return [_value(x) for x in v]
    if hasattr(v, "value"):  # enums
        return v.value
    return v


def _ms(v: float | None, shift: float = 0.0) -> float | None:
    return None if v is None else float(round(v + shift))


def _cue(cue: Cue, shift: float = 0.0) -> dict[str, Any]:
    return {
        "role": cue.role.value,
        "slot": cue.slot,
        "position_ms": _ms(cue.position_ms, shift),
        "end_ms": _ms(cue.end_ms, shift),
        "name": cue.name,
    }


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda c: json.dumps(c, sort_keys=True))


def _track(track: Track, r: Rules, cues: list[dict[str, Any]], shift: float) -> dict[str, Any]:
    out: dict[str, Any] = {name: _value(getattr(track, name)) for name in r.track_fields}
    out["cues"] = _sorted(cues)
    out["grid"] = _grid(track.grid, r, shift)
    return out


# How far past the last grid marker beats are compared. Fixed, so both sides list the same
# beats however long each format says the track is.
_GRID_TAIL_BEATS = 64


def _grid(markers: list[TempoMarker], r: Rules, shift: float) -> list[list[int]]:
    """Where every beat falls, in whole milliseconds, with its beat-in-bar.

    Formats write the same grid differently (Rekordbox lists every beat, Serato stores a few
    markers), so grids are compared by their beats, from the start of the track to
    ``_GRID_TAIL_BEATS`` past the last marker.
    """
    if not markers:
        return []
    shifted = sorted(gridlib.shift(markers, shift), key=lambda m: m.position_ms)
    last = shifted[-1]
    beat_ms = 60000.0 / last.bpm
    # A grid with one marker is the same wherever along it that marker sits, so measure from
    # its first beat in the track rather than from the marker.
    anchor = last.position_ms % beat_ms if len(shifted) == 1 else last.position_ms
    end = anchor + (_GRID_TAIL_BEATS - 0.5) * beat_ms  # mid-beat, so no beat sits on the edge
    return [
        [round(position), beat if r.grid_beats else 0]
        for position, beat in gridlib.beat_positions(shifted, end)
    ]


def _form(library: Library, root: Path, tracks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    keys = {t.id: track_key(t, root) for t in library.tracks.values()}
    lists = []
    for parents, playlist in library.playlists.walk():
        if playlist.track_ids is not None:
            members = [keys[i] for i in playlist.track_ids if keys.get(i) in tracks]
            lists.append({"path": [*parents, playlist.name], "tracks": members})
    return {"schema": SCHEMA, "tracks": dict(sorted(tracks.items())), "playlists": _sorted(lists)}


def _on_drive(library: Library, root: Path) -> list[Track]:
    return [t for t in library.tracks.values() if _local(t, root).is_file()]


def canonical(library: Library, root: Path, r: Rules) -> dict[str, Any]:
    """A library read from ``r.target``, as plain sorted data."""
    tracks = {}
    for track in _on_drive(library, root):
        cues = [_cue(c) for c in track.cues if c.slot is None or c.slot < r.hot_cue_slots]
        tracks[track_key(track, root)] = _track(track, r, cues, 0.0)
    return _form(library, root, tracks)


def expected(source: Library, root: Path, r: Rules, converted: set[str]) -> dict[str, Any]:
    """What ``canonical`` of the conversion's result should be, worked out from the source.

    ``converted`` holds the keys of tracks whose audio the conversion changed (e.g. to MP3).
    """
    tracks = {}
    for track in _on_drive(source, root):
        key = track_key(track, root)
        shift = r.transcode_shift_ms if key in converted else 0.0
        tracks[key] = _track(track, r, _expected_cues(track, r, shift), shift)
    return _form(source, root, tracks)


def _expected_cues(track: Track, r: Rules, shift: float) -> list[dict[str, Any]]:
    cues = [c for c in track.cues if c.slot is None or c.slot < r.hot_cue_slots]
    if r.target != Format.SERATO:
        return [_cue(c, shift) for c in cues]
    # Serato has hot cues (slots), saved loops (no slot) and nothing else, so:
    # - a hot loop becomes a hot cue in its slot plus a saved loop;
    # - memory cues, and main/intro/outro cues, go into free hot cue slots in time order,
    #   unless a hot cue already marks that spot (within DUPLICATE_MS), or no slot is free.
    from .serato.library import DUPLICATE_MS

    out: list[dict[str, Any]] = []
    hot: dict[int, float] = {}
    rest: list[Cue] = []
    for c in cues:
        if c.role is CueRole.LOOP:
            out.append(_cue(replace(c, slot=None), shift))
            if c.slot is not None:
                out.append(_cue(replace(c, role=CueRole.CUE, end_ms=None), shift))
                hot[c.slot] = c.position_ms
        elif c.slot is not None:
            out.append(_cue(replace(c, role=CueRole.CUE), shift))
            hot[c.slot] = c.position_ms
        else:
            rest.append(c)
    free = [s for s in range(r.hot_cue_slots) if s not in hot]
    for c in sorted(rest, key=lambda c: c.position_ms):
        if any(abs(p - c.position_ms) < DUPLICATE_MS for p in hot.values()) or not free:
            continue
        slot = free.pop(0)
        name = c.name or ("" if c.role is CueRole.CUE else c.role.value.title())
        out.append(_cue(Cue(CueRole.CUE, c.position_ms, slot=slot, name=name), shift))
        hot[slot] = c.position_ms
    return out


def converted(source: Library, result: Library, root: Path) -> set[str]:
    """Keys of tracks whose audio file changed format in the conversion (e.g. Ogg to MP3)."""
    before = {track_key(t, root): t.extension for t in source.tracks.values()}
    return {
        key
        for t in result.tracks.values()
        if (key := track_key(t, root)) in before and before[key] != t.extension
    }


def check(source: Library, result: Library, root: Path, r: Rules) -> list[str]:
    """Differences between what the conversion should have produced and what it did."""
    want = expected(source, root, r, converted(source, result, root))
    got = canonical(result, root, r)
    if fingerprint(want) == fingerprint(got):
        return []
    # Beats are compared in whole milliseconds, so the same beat can round to neighbouring
    # values in two formats (Rekordbox stores whole ms, Serato fractions). Allow that, and
    # nothing more.
    for key in want["tracks"].keys() & got["tracks"].keys():
        a, b = want["tracks"][key]["grid"], got["tracks"][key]["grid"]
        if len(a) == len(b) and all(
            x[1] == y[1] and abs(x[0] - y[0]) <= 1 for x, y in zip(a, b, strict=True)
        ):
            got["tracks"][key]["grid"] = a
    return differences(want, got)


# --- comparing ------------------------------------------------------------------------------


def fingerprint(form: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(form, sort_keys=True).encode()).hexdigest()


def differences(want: Any, got: Any, path: str = "") -> list[str]:
    """Where two canonical forms differ, as readable lines."""
    if isinstance(want, dict) and isinstance(got, dict):
        out = []
        for k in sorted(want.keys() | got.keys(), key=str):
            where = f"{path}/{k}" if path else str(k)
            if k not in got:
                out.append(f"{where}: missing")
            elif k not in want:
                out.append(f"{where}: unexpected")
            else:
                out += differences(want[k], got[k], where)
        return out
    if want != got:
        return [f"{path}: expected {json.dumps(want)}, got {json.dumps(got)}"]
    return []
