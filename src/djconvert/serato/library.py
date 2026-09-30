"""Read and write a Serato library: ``_Serato_/database V2``, crates, and file tags.

Serato keeps track metadata and file paths in ``database V2`` and crates in
``Subcrates/<Parent>%%<Child>.crate``. Paths have no leading slash and are
relative to the root of the drive holding the ``_Serato_`` folder: ``/`` for
the internal drive on macOS (``~/Music/_Serato_``), the drive letter on Windows,
or the drive's mount point for an external drive with its own ``_Serato_``.

Cue points, loops and beat grids are *not* in the database; they live in the
audio files' tags (:mod:`djconvert.serato.tags`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from ..colours import (
    SERATO_LOOP_COLOUR,
    serato_cue_from_display,
    serato_cue_to_display,
    serato_track_from_display,
    serato_track_to_display,
)
from ..keys import KeyNotation, format_key, parse_key
from ..model import Cue, CueRole, Library, Playlist, TempoMarker, Track, normalise_path
from .binfile import CRATE_VERSION, DATABASE_VERSION, Field, SeratoFormatError, dump, parse
from .markers import GridMarker, Markers2, SeratoCue, SeratoLoop
from .tags import SUPPORTED, read_tags, write_tags

DATABASE_FILE = "database V2"
CRATE_DIR = "Subcrates"
MAX_SAVED_LOOPS = 8
DUPLICATE_MS = 5  # a memory cue this close to a hot cue is not worth a slot

Resolver = Callable[[str], Path | None]  # library path -> readable local file, or None


def find_serato_dir(path: Path) -> Path:
    """Accept the ``_Serato_`` folder, its parent, or the ``database V2`` file."""
    if path.is_file() and path.name == DATABASE_FILE:
        return path.parent
    if (path / DATABASE_FILE).is_file():
        return path
    if (path / "_Serato_" / DATABASE_FILE).is_file():
        return path / "_Serato_"
    raise FileNotFoundError(f"no Serato '{DATABASE_FILE}' found at {path}")


def to_serato_path(location: str, volume_root: str) -> str:
    """Absolute path -> the root-relative form Serato stores."""
    path = normalise_path(location)
    root = normalise_path(volume_root).rstrip("/")
    if root and path.lower().startswith(root.lower() + "/"):
        path = path[len(root) + 1 :]
    elif re.match(r"^[A-Za-z]:/", path):
        path = path[3:]
    return path.lstrip("/")


def from_serato_path(pfil: str, volume_root: str) -> str:
    root = normalise_path(volume_root).rstrip("/")
    return f"{root}/{pfil.lstrip('/')}" if root else f"/{pfil.lstrip('/')}"


# --- reading -------------------------------------------------------------------------


def _text(fields: Field, tag: str) -> str:
    value = fields.get(tag)
    return value.strip() if isinstance(value, str) else ""


def _int(fields: Field, tag: str) -> int | None:
    value = fields.get(tag)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _duration(text: str) -> float:
    """``tlen`` is text; accept seconds or [hh:]mm:ss[.ff]."""
    try:
        seconds = 0.0
        for part in text.split(":"):
            seconds = seconds * 60 + float(part)
        return seconds
    except ValueError:
        return 0.0


def _number(text: str) -> float:
    match = re.match(r"\s*([\d.]+)", text)
    try:
        return float(match.group(1)) if match else 0.0
    except ValueError:
        return 0.0


def _sample_rate(text: str) -> int:
    """``tsmp`` is text such as ``44100``, ``44.1`` or ``44.1k``."""
    value = _number(text)
    return round(value * 1000) if 0 < value < 1000 else int(value)


def cues_from_serato(markers: Markers2) -> list[Cue]:
    cues = [
        Cue(
            CueRole.CUE,
            float(c.position_ms),
            slot=c.index,
            name=c.name,
            colour=serato_cue_to_display(c.colour),
        )
        for c in markers.cues
    ]
    cues += [
        Cue(
            CueRole.LOOP,
            float(lp.start_ms),
            float(lp.end_ms),
            name=lp.name,
            locked=lp.locked,
        )
        for lp in sorted(markers.loops, key=lambda lp: lp.index)
        if lp.end_ms > lp.start_ms
    ]
    return cues


def grid_from_serato(markers: list[GridMarker]) -> list[TempoMarker]:
    grid = []
    for i, marker in enumerate(markers):
        position = marker.position_s * 1000.0
        if marker.bpm is not None:
            bpm = marker.bpm
        else:
            nxt = markers[i + 1].position_s * 1000.0 if i + 1 < len(markers) else None
            if not nxt or nxt <= position or not marker.beats_to_next:
                continue
            bpm = marker.beats_to_next * 60000.0 / (nxt - position)
        if bpm > 0:
            grid.append(TempoMarker(position, bpm, 1))
    return grid


def _track_from_fields(fields: Field, volume_root: str, index: int) -> Track | None:
    pfil = fields.get("pfil")
    if not isinstance(pfil, str) or not pfil:
        return None
    added = _int(fields, "uadd")
    track_number = _text(fields, "ttrk") or ""
    colour = _int(fields, "ulbl")
    return Track(
        id=f"serato:{index}",
        location=from_serato_path(pfil, volume_root),
        title=_text(fields, "tsng"),
        artist=_text(fields, "tart"),
        album=_text(fields, "talb"),
        genre=_text(fields, "tgen"),
        composer=_text(fields, "tcmp"),
        grouping=_text(fields, "tgrp"),
        comment=_text(fields, "tcom"),
        label=_text(fields, "tlbl"),
        remixer=_text(fields, "trmx"),
        year=_text(fields, "ttyr"),
        track_number=int(track_number) if track_number.isdigit() else None,
        duration_s=_duration(_text(fields, "tlen")),
        sample_rate=_sample_rate(_text(fields, "tsmp")),
        bitrate=int(_number(_text(fields, "tbit"))),
        bpm=_number(_text(fields, "tbpm")),
        bpm_locked=bool(fields.get("bbgl")),
        key=parse_key(_text(fields, "tkey")),
        colour=serato_track_to_display(colour) if colour is not None else None,
        play_count=_int(fields, "utpc") or 0,
        date_added=datetime.fromtimestamp(added, UTC).date() if added else None,
    )


@dataclass
class SeratoReadOptions:
    volume_root: str = "/"
    read_file_tags: bool = True


def read_serato(
    path: Path,
    options: SeratoReadOptions,
    resolve: Resolver,
    progress: Callable[[str], None] = print,
) -> Library:
    serato_dir = find_serato_dir(path)
    library = Library(source=f"Serato library at {serato_dir}")
    fields = parse((serato_dir / DATABASE_FILE).read_bytes())
    by_path: dict[str, str] = {}
    for index, entry in enumerate(fields):
        if entry.tag != "otrk":
            continue
        track = _track_from_fields(entry, options.volume_root, index)
        if track is None:
            continue
        library.add_track(track)
        by_path[normalise_path(track.location).lower()] = track.id

    if options.read_file_tags:
        _read_file_tags(library, resolve, progress)

    crate_dir = serato_dir / CRATE_DIR
    crate_files = sorted(crate_dir.glob("*.crate"), key=lambda p: p.stem.lower())
    for crate_file in crate_files:
        names = crate_file.stem.split("%%")
        track_ids = []
        try:
            crate = parse(crate_file.read_bytes())
        except SeratoFormatError as exc:
            library.warnings.append(f"Skipped crate {crate_file.name}: {exc}")
            continue
        for entry in crate:
            ptrk = entry.get("ptrk") if entry.tag == "otrk" else None
            if not isinstance(ptrk, str):
                continue
            key = normalise_path(from_serato_path(ptrk, options.volume_root)).lower()
            if key in by_path:
                track_ids.append(by_path[key])
            else:
                library.warnings.append(f"Crate {' / '.join(names)}: track not in database: {ptrk}")
        _place_crate(library.playlists, names, track_ids)
    smart = list((serato_dir / "SmartCrates").glob("*.scrate"))
    if smart:
        library.warnings.append(
            f"{len(smart)} smart crate(s) not converted (their rules have no equivalent)."
        )
    return library


def _read_file_tags(library: Library, resolve: Resolver, progress: Callable[[str], None]) -> None:
    missing = unreadable = 0
    total = len(library.tracks)
    for n, track in enumerate(library.tracks.values(), 1):
        if n % 200 == 0:
            progress(f"Reading Serato tags {n}/{total}")
        local = resolve(track.location)
        if local is None:
            missing += 1
            continue
        if local.suffix.lower() not in SUPPORTED:
            continue
        try:
            tags = read_tags(local)
        except Exception as exc:  # corrupt files must not stop the whole read
            unreadable += 1
            library.warnings.append(f"{track.display_name}: {exc}")
            continue
        track.cues = cues_from_serato(tags.markers)
        track.grid = grid_from_serato(tags.grid)
        if tags.markers.bpm_locked is not None:
            track.bpm_locked = tags.markers.bpm_locked
        if track.colour is None and tags.markers.track_colour is not None:
            track.colour = serato_track_to_display(tags.markers.track_colour)
    if missing:
        library.warnings.append(
            f"{missing} track file(s) not found, so their cues and beat grids could not be read."
        )
    if unreadable:
        library.warnings.append(f"{unreadable} track file(s) had unreadable Serato tags.")


def _place_crate(root: Playlist, names: list[str], track_ids: list[str]) -> None:
    """Put crate ``A%%B%%C`` at folder A / folder B / crate C.

    A Serato crate can hold tracks *and* subcrates; other programs cannot, so a
    parent crate's own tracks go in a crate of the same name inside its folder.
    """
    node = root
    for name in names[:-1]:
        folder = next((c for c in node.children if c.is_folder and c.name == name), None)
        if folder is None:
            # A crate file for the parent may already exist: turn it into a folder.
            existing = next((c for c in node.children if not c.is_folder and c.name == name), None)
            folder = Playlist.folder(name)
            if existing is not None:
                node.children[node.children.index(existing)] = folder
                if existing.track_ids:
                    folder.children.append(existing)
            else:
                node.children.append(folder)
        node = folder
    name = names[-1]
    folder = next((c for c in node.children if c.is_folder and c.name == name), None)
    target = folder or node
    if folder is not None and not track_ids:
        return
    target.children.append(Playlist(name, track_ids, is_crate=True))


# --- writing -------------------------------------------------------------------------


@dataclass
class SeratoWriteOptions:
    volume_root: str = "/"
    key_notation: KeyNotation = KeyNotation.MUSICAL
    write_file_tags: bool = False
    max_hot_cues: int = 8
    memory_cues_to_hot_cues: bool = True
    base_database: Path | None = None  # merge into this existing database V2


@dataclass
class SeratoWriteResult:
    files: list[Path] = field(default_factory=list)
    tags_written: int = 0
    warnings: list[str] = field(default_factory=list)


def markers_for_track(track: Track, options: SeratoWriteOptions) -> tuple[Markers2, list[str]]:
    """Map a track's cues onto Serato's hot cue and saved loop slots."""
    notes: list[str] = []
    markers = Markers2(bpm_locked=track.bpm_locked)
    if track.colour is not None:
        markers.track_colour = serato_track_from_display(track.colour)
    used: set[int] = set()
    loops: list[Cue] = []
    extra: list[Cue] = []
    for cue in sorted(track.cues, key=lambda c: (c.slot is None, c.slot or 0, c.position_ms)):
        if cue.role is CueRole.LOOP:
            loops.append(cue)
            if cue.slot is None:
                continue
        if cue.slot is not None and cue.slot < options.max_hot_cues and cue.slot not in used:
            used.add(cue.slot)
            markers.cues.append(
                SeratoCue(
                    cue.slot,
                    round(cue.position_ms),
                    serato_cue_from_display(cue.colour, cue.slot),
                    cue.name,
                )
            )
        elif cue.role is not CueRole.LOOP:
            extra.append(cue)
    if options.memory_cues_to_hot_cues:
        free = [s for s in range(options.max_hot_cues) if s not in used]
        for cue in sorted(extra, key=lambda c: c.position_ms):
            if any(abs(c.position_ms - cue.position_ms) < DUPLICATE_MS for c in markers.cues):
                extra.remove(cue)  # already marked by a hot cue
                continue
            if not free:
                break
            slot = free.pop(0)
            name = cue.name or ("" if cue.role is CueRole.CUE else cue.role.value.title())
            markers.cues.append(
                SeratoCue(
                    slot, round(cue.position_ms), serato_cue_from_display(cue.colour, slot), name
                )
            )
            extra.remove(cue)
    if extra:
        notes.append(f"{len(extra)} cue(s) had no free Serato hot cue slot")
    for index, loop in enumerate(sorted(loops, key=lambda c: c.position_ms)[:MAX_SAVED_LOOPS]):
        markers.loops.append(
            SeratoLoop(
                index,
                round(loop.position_ms),
                round(loop.end_ms or loop.position_ms),
                SERATO_LOOP_COLOUR,
                loop.locked,
                loop.name,
            )
        )
    if len(loops) > MAX_SAVED_LOOPS:
        notes.append(f"{len(loops) - MAX_SAVED_LOOPS} loop(s) beyond Serato's 8 saved loops")
    return markers, notes


def grid_to_serato(grid: list[TempoMarker]) -> list[GridMarker]:
    """Convert tempo markers to Serato beat grid markers.

    Serato draws bars from its first marker, so that one is moved to a
    downbeat (exact: the first section's tempo extends backwards). Later
    markers stay put; moving them would change which tempo covers the gap.
    """
    positions: list[tuple[float, float]] = []  # (ms, bpm)
    for marker in grid:
        if marker.bpm <= 0:
            continue
        position = marker.position_ms
        if not positions:
            beat = 60000.0 / marker.bpm
            position -= ((marker.beat - 1) % 4) * beat
            if position < 0:
                position += 4 * beat
        elif position <= positions[-1][0]:
            continue
        positions.append((position, marker.bpm))
    result = []
    for i, (position, bpm) in enumerate(positions):
        if i == len(positions) - 1:
            result.append(GridMarker(position / 1000.0, bpm=bpm))
        else:
            beats = max(1, round((positions[i + 1][0] - position) * bpm / 60000.0))
            result.append(GridMarker(position / 1000.0, beats_to_next=beats))
    return result


def _crate_filename(names: tuple[str, ...]) -> str:
    safe = [re.sub(r'[\\/:*?"<>|]', "-", n).replace("%%", "%").strip() or "Untitled" for n in names]
    return "%%".join(safe) + ".crate"


def _track_fields(track: Track, options: SeratoWriteOptions) -> list[Field]:
    fields = [
        Field("ttyp", track.extension),
        Field("pfil", to_serato_path(track.location, options.volume_root)),
    ]
    texts = [
        ("tsng", track.title),
        ("tart", track.artist),
        ("talb", track.album),
        ("tgen", track.genre),
        ("tcom", track.comment),
        ("tgrp", track.grouping),
        ("tlbl", track.label),
        ("tcmp", track.composer),
        ("trmx", track.remixer),
        ("ttyr", track.year),
        ("tbpm", f"{track.bpm:.2f}" if track.bpm else ""),
        ("tkey", format_key(track.key, options.key_notation)),
    ]
    fields += [Field(tag, value) for tag, value in texts if value]
    if track.date_added:
        stamp = int(datetime(*track.date_added.timetuple()[:3], tzinfo=UTC).timestamp())
        fields += [Field("tadd", str(stamp)), Field("uadd", stamp)]
    if track.colour is not None:
        fields.append(Field("ulbl", serato_track_from_display(track.colour)))
    fields += [Field("bmis", False), Field("bbgl", track.bpm_locked)]
    return fields


def _merge_track(existing: Field, new: list[Field]) -> None:
    assert isinstance(existing.value, list)
    replacing = {f.tag for f in new}
    kept = [f for f in existing.value if f.tag not in replacing]
    existing.value = [*new, *kept]


def write_serato(
    library: Library,
    out_dir: Path,
    options: SeratoWriteOptions,
    resolve: Resolver,
    progress: Callable[[str], None] = print,
) -> SeratoWriteResult:
    result = SeratoWriteResult()
    serato_dir = out_dir / "_Serato_"
    (serato_dir / CRATE_DIR).mkdir(parents=True, exist_ok=True)

    if options.base_database:
        database = parse(options.base_database.read_bytes())
    else:
        database = [Field("vrsn", DATABASE_VERSION)]
    existing = {
        normalise_path(str(f.get("pfil"))).lower(): f
        for f in database
        if f.tag == "otrk" and isinstance(f.get("pfil"), str)
    }
    for track in library.tracks.values():
        fields = _track_fields(track, options)
        key = normalise_path(str(fields[1].value)).lower()
        if key in existing:
            _merge_track(existing[key], fields)
        else:
            entry = Field("otrk", fields)
            database.append(entry)
            existing[key] = entry
    db_path = serato_dir / DATABASE_FILE
    db_path.write_bytes(dump(database))
    result.files.append(db_path)

    for parents, playlist in library.playlists.walk():
        names = (*parents, playlist.name)
        crate: list[Field] = [
            Field("vrsn", CRATE_VERSION),
            Field("osrt", [Field("tvcn", "#"), Field("brev", False)]),
        ]
        for column in ("song", "artist", "bpm", "key", "album", "length", "comment"):
            crate.append(Field("ovct", [Field("tvcn", column), Field("tvcw", "0")]))
        for tid in playlist.track_ids or []:
            if track := library.tracks.get(tid):
                pfil = to_serato_path(track.location, options.volume_root)
                crate.append(Field("otrk", [Field("ptrk", pfil)]))
        crate_path = serato_dir / CRATE_DIR / _crate_filename(names)
        crate_path.write_bytes(dump(crate))
        result.files.append(crate_path)

    if options.write_file_tags:
        _write_file_tags(library, options, resolve, result, progress)
    return result


def _write_file_tags(
    library: Library,
    options: SeratoWriteOptions,
    resolve: Resolver,
    result: SeratoWriteResult,
    progress: Callable[[str], None],
) -> None:
    missing: list[str] = []
    dropped = 0
    total = len(library.tracks)
    for n, track in enumerate(library.tracks.values(), 1):
        if n % 50 == 0:
            progress(f"Writing Serato tags {n}/{total}")
        if not track.cues and not track.grid:
            continue
        local = resolve(track.location)
        if local is None:
            missing.append(track.display_name)
            continue
        if local.suffix.lower() not in SUPPORTED:
            result.warnings.append(
                f"{track.display_name}: Serato tags not supported for this file type"
            )
            continue
        markers, notes = markers_for_track(track, options)
        if notes:
            dropped += 1
        try:
            write_tags(local, markers, grid_to_serato(track.grid))
            result.tags_written += 1
        except Exception as exc:
            result.warnings.append(f"{track.display_name}: could not write tags: {exc}")
    if missing:
        result.warnings.append(
            f"{len(missing)} track file(s) not found; their cues and grids were not written "
            f"(e.g. {', '.join(missing[:3])})."
        )
    if dropped:
        result.warnings.append(
            f"{dropped} track(s) had more cues or loops than Serato has slots; extras were left out."
        )


def today() -> date:
    return datetime.now(UTC).date()
