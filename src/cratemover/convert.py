"""Read a library in one format, remap paths, write it in another."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import grid as gridlib
from .keys import KeyNotation
from .mixxx import MixxxReadOptions, MixxxWriteOptions, find_database, read_mixxx, write_mixxx
from .model import Format, Library, Playlist
from .offsets import Mp3Decoder
from .paths import apply_rules, infer_access_rules, make_resolver
from .pioneer.usb import OneLibraryMode
from .rekordbox_xml import RekordboxWriteOptions, read_rekordbox_xml, write_rekordbox_xml
from .serato.library import (
    CRATE_DIR,
    DATABASE_FILE,
    SeratoReadOptions,
    SeratoWriteOptions,
    find_serato_dir,
    read_serato,
    write_serato,
)
from .sync import Direction, Prefer, SyncOptions, merge

FORMATS: dict[Format, str] = {
    Format.MIXXX: "Mixxx (mixxxdb.sqlite)",
    Format.REKORDBOX_USB: "Rekordbox USB stick (PIONEER/export.pdb)",
    Format.REKORDBOX_XML: "Rekordbox XML",
    Format.REKORDBOX_DB: "Rekordbox 6/7 library (master.db)",
    Format.SERATO: "Serato (_Serato_ folder, computer or USB)",
}
SOURCE_FORMATS = FORMATS
TARGET_FORMATS = {k: v for k, v in FORMATS.items() if k != Format.REKORDBOX_DB}
SYNC_FORMATS = TARGET_FORMATS  # sync writes back in place

Progress = Callable[[str], None]
Resolver = Callable[[str], Path | None]


def _noop(_: str) -> None:
    pass


# --- options -------------------------------------------------------------------------


@dataclass
class ReadOptions:
    format: Format
    path: str
    access_rules: list[tuple[str, str]] = field(default_factory=list)
    mp3_decoder: Mp3Decoder = Mp3Decoder.MAD
    serato_root: str = "/"
    read_file_tags: bool = True
    # Folders to look for the music in when the library's own paths don't exist here
    # (another OS, another mount point). Found mappings are added to access_rules.
    music_roots: list[str] = field(default_factory=list)


@dataclass
class WriteOptions:
    format: Format
    output_dir: str
    path_rules: list[tuple[str, str]] = field(default_factory=list)
    key_notation: KeyNotation | None = None  # None: the target's usual notation
    mp3_decoder: Mp3Decoder = Mp3Decoder.MAD
    memory_cues_to_hot_cues: bool = True
    # Rekordbox
    rekordbox_memory_cues: bool = True
    rekordbox_hot_cues_as_memory: bool = False
    # Serato
    serato_root: str = "/"
    serato_write_tags: bool = False
    serato_max_hot_cues: int = 8
    serato_base_database: str = ""
    # Mixxx
    mixxx_base_database: str = ""
    mixxx_playlists_as_crates: bool = False
    # USB sticks (Rekordbox, and Serato with serato_root set to the stick)
    copy_missing: bool = True  # copy tracks that aren't on the stick onto it
    waveforms: bool = True  # measure Rekordbox waveforms with ffmpeg
    device_name: str = ""
    onelibrary: OneLibraryMode = OneLibraryMode.AUTO
    usb_xml: bool = True  # also put a rekordbox.xml for importing into rekordbox on the stick
    usb_xml_root: str = (
        ""  # how the rekordbox computer sees the stick (E:/, /Volumes/STICK); "" = here
    )
    # Update the library at output_dir in place (with backups) instead of writing a new one
    in_place: bool = False
    # Selection: playlist paths ("Folder / Name"); empty means everything
    playlists: list[str] = field(default_factory=list)


def read_library(options: ReadOptions, progress: Progress = _noop) -> Library:
    resolve = make_resolver(options.access_rules)
    path = Path(options.path)
    if options.format == Format.MIXXX:
        library = read_mixxx(
            path, MixxxReadOptions(mp3_decoder=options.mp3_decoder), resolve, progress
        )
    elif options.format == Format.REKORDBOX_XML:
        library = read_rekordbox_xml(path)
    elif options.format == Format.REKORDBOX_DB:
        from .rekordbox_db import read_rekordbox_db

        library = read_rekordbox_db(path, progress)
    elif options.format == Format.REKORDBOX_USB:
        from .pioneer.usb import read_rekordbox_usb

        library = read_rekordbox_usb(path, progress)
    elif options.format == Format.SERATO:
        library = read_serato(
            path,
            SeratoReadOptions(options.serato_root, options.read_file_tags),
            resolve,
            progress,
        )
    else:
        raise ValueError(f"unknown source format {options.format!r}")
    if options.music_roots:
        missing = [t.location for t in library.tracks.values() if resolve(t.location) is None]
        roots, options.music_roots = [Path(r) for r in options.music_roots], []
        if rules := infer_access_rules(missing, roots):
            progress("Found the music files: " + ", ".join(f"{a} => {b}" for a, b in rules))
            options.access_rules = [*options.access_rules, *rules]
            # Mixxx and Serato read from the audio files, so read again now they resolve.
            if options.format in (Format.MIXXX, Format.SERATO):
                return read_library(options, progress)
            resolve = make_resolver(options.access_rules)
    for track in library.tracks.values():
        track.grid = gridlib.simplify(track.grid)
        if found := resolve(track.location):
            track.extra["local"] = str(found)
    return library


def playlist_paths(library: Library) -> list[str]:
    return [" / ".join((*parents, p.name)) for parents, p in library.playlists.walk()]


def select(library: Library, paths: list[str]) -> Library:
    """Keep only the named playlists and the tracks in them."""
    if not paths:
        return library
    wanted = set(paths)

    def prune(node: Playlist, parents: tuple[str, ...]) -> Playlist | None:
        children = []
        for child in node.children:
            if child.is_folder:
                if kept := prune(child, (*parents, child.name)):
                    children.append(kept)
            elif " / ".join((*parents, child.name)) in wanted:
                children.append(child)
        return Playlist(node.name, None, children) if children else None

    root = prune(library.playlists, ()) or Playlist.folder("ROOT")
    ids = {tid for _, p in root.walk() for tid in p.track_ids or []}
    return Library(
        source=library.source,
        tracks={tid: t for tid, t in library.tracks.items() if tid in ids},
        playlists=root,
        warnings=list(library.warnings),
    )


@dataclass
class ConvertResult:
    files: list[str]
    summary: dict[str, int]
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.cratemover-{stamp}")
    if path.is_dir():
        shutil.copytree(path, backup)
    else:
        shutil.copy2(path, backup)
    return backup


def _place_on_drive(
    library: Library, root: Path, resolve: Resolver, result_warnings: list[str]
) -> int:
    """Copy tracks that aren't on the drive at ``root`` onto it (for Serato USB sticks)."""
    from .pioneer.usb import _copy_to_stick, _stick_path

    copied = 0
    for track in library.tracks.values():
        local = resolve(track.location)
        if local is None or _stick_path(local, root) is not None:
            continue
        stick_path, was_copied = _copy_to_stick(local, track, root)
        track.location = str(root) + stick_path
        track.extra["local"] = str(root) + stick_path
        copied += was_copied
    if copied:
        result_warnings.append(f"Copied {copied} track(s) onto {root}.")
    return copied


def _stick_xml(root: Path, options: WriteOptions, progress: Progress) -> Path:
    """``rekordbox.xml`` at the stick's root, describing the stick's own tracks.

    rekordbox on a laptop can't play a stick's device library directly (it only
    shows it in Export mode), and whether cues survive importing it is unclear.
    Importing this XML (Preferences > Advanced > rekordbox xml) is the documented
    way in, cues and grids included. XML needs absolute paths, so they are
    written as the rekordbox computer sees the drive (``usb_xml_root``).
    """
    from .pioneer.usb import find_stick_root, read_rekordbox_usb

    progress("Writing rekordbox.xml for importing into rekordbox")
    stick_root = find_stick_root(root)
    stick = read_rekordbox_usb(stick_root, progress)
    seen_as = options.usb_xml_root.strip() or str(stick_root)
    for track in stick.tracks.values():
        track.location = apply_rules(track.location, [(str(stick_root), seen_as)])
    target = stick_root / "rekordbox.xml"
    write_rekordbox_xml(
        stick,
        target,
        RekordboxWriteOptions(key_notation=options.key_notation or KeyNotation.CAMELOT),
    )
    return target


def _refuse_second_library(folder: Path, fmt: Format) -> None:
    """A drive holds one DJ library: writing a second one next to it can break the first
    program's reading of it (see docs/usb-compatibility.md). Convert the drive instead."""
    from .devices import _libraries

    if not folder.is_dir():
        return
    others = sorted({lib["format"] for lib in _libraries(folder)} - {fmt})
    if others:
        raise ValueError(
            f"{folder} already has a {', '.join(others)} library. cratemover won't put a second "
            f"library on a drive: convert the drive instead, which replaces one with the other."
        )


def write_library(
    library: Library,
    options: WriteOptions,
    access_rules: list[tuple[str, str]],
    progress: Progress = _noop,
) -> ConvertResult:
    if options.format in (Format.REKORDBOX_USB, Format.SERATO):
        _refuse_second_library(Path(options.output_dir), options.format)
    library = select(copy.deepcopy(library), options.playlists)
    # Find local files using source locations, then rewrite locations for the target.
    source_resolve = make_resolver(access_rules)
    local: dict[str, Path | None] = {}
    for track in library.tracks.values():
        known = Path(track.extra["local"]) if "local" in track.extra else None
        found = known if known is not None and known.is_file() else source_resolve(track.location)
        track.location = apply_rules(track.location, options.path_rules)
        local[track.location] = found
        if found is not None:
            track.extra["local"] = str(found)

    def resolve(location: str) -> Path | None:
        if location in local:
            return local[location]
        path = Path(location)
        return path if path.is_file() else None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(options.output_dir)
    warnings = list(library.warnings)
    files: list[Path] = []
    if options.format == Format.REKORDBOX_XML:
        target = out if out.suffix.lower() == ".xml" else out / "rekordbox.xml"
        if options.in_place:
            _backup(target, stamp)
        rb = write_rekordbox_xml(
            library,
            target,
            RekordboxWriteOptions(
                key_notation=options.key_notation or KeyNotation.CAMELOT,
                memory_cues=options.rekordbox_memory_cues,
                hot_cues_as_memory=options.rekordbox_hot_cues_as_memory,
            ),
        )
        files, warnings = rb.files, warnings + rb.warnings
    elif options.format == Format.REKORDBOX_USB:
        from .pioneer.usb import UsbWriteOptions, write_rekordbox_usb

        usb = write_rekordbox_usb(
            library,
            out,
            UsbWriteOptions(
                copy_missing=options.copy_missing,
                key_notation=options.key_notation or KeyNotation.MUSICAL,
                waveforms=options.waveforms,
                device_name=options.device_name,
                onelibrary=options.onelibrary,
            ),
            resolve,
            progress,
        )
        files, warnings = usb.files, warnings + usb.warnings
        warnings.insert(
            0,
            f"{usb.tracks} track(s) on the stick: {usb.copied} copied, {usb.transcoded} converted to MP3; "
            f"waveforms {usb.reused} reused, {usb.measured} measured, {usb.placeholders} placeholders.",
        )
        if options.usb_xml:
            files.append(_stick_xml(out, options, progress))
    elif options.format == Format.SERATO:
        serato_root = options.serato_root or "/"
        if options.in_place:
            try:
                serato_dir = find_serato_dir(out)
            except FileNotFoundError:
                serato_dir = out / "_Serato_"
            out = serato_dir.parent
            base = serato_dir / DATABASE_FILE
            _backup(base, stamp)
            _backup(serato_dir / CRATE_DIR, stamp)
            base_database = base if base.is_file() else None
        else:
            out.mkdir(parents=True, exist_ok=True)
            base_database = (
                Path(options.serato_base_database) if options.serato_base_database else None
            )
        if serato_root not in ("", "/") and options.copy_missing:
            _place_on_drive(library, Path(serato_root), resolve, warnings)
        se = write_serato(
            library,
            out,
            SeratoWriteOptions(
                volume_root=serato_root,
                key_notation=options.key_notation or KeyNotation.MUSICAL,
                write_file_tags=options.serato_write_tags,
                max_hot_cues=options.serato_max_hot_cues,
                memory_cues_to_hot_cues=options.memory_cues_to_hot_cues,
                base_database=base_database,
            ),
            lambda location: (
                resolve(location) or (Path(location) if Path(location).is_file() else None)
            ),
            progress,
        )
        files, warnings = se.files, warnings + se.warnings
        if not options.serato_write_tags:
            warnings.append(
                "Cues and beat grids were not written: Serato keeps them inside the audio files. "
                "Enable 'write Serato tags into audio files' to include them."
            )
        else:
            warnings.insert(0, f"Serato tags written to {se.tags_written} audio file(s).")
    elif options.format == Format.MIXXX:
        mixxx_options = MixxxWriteOptions(
            mp3_decoder=options.mp3_decoder,
            base_database=Path(options.mixxx_base_database)
            if options.mixxx_base_database
            else None,
            playlists_as_crates=options.mixxx_playlists_as_crates,
            memory_cues_to_hot_cues=options.memory_cues_to_hot_cues,
            key_notation=options.key_notation or KeyNotation.MUSICAL,
        )
        if options.in_place:
            db_path = find_database(out)
            _backup(db_path, stamp)
            mixxx_options.base_database = db_path
            mixxx_options.replace_playlists = True
            with tempfile.TemporaryDirectory(dir=db_path.parent) as tmp:
                mx = write_mixxx(library, Path(tmp), mixxx_options, resolve, progress)
                os.replace(Path(tmp) / "mixxxdb.sqlite", db_path)
            mx.files = [db_path]
        else:
            out.mkdir(parents=True, exist_ok=True)
            mx = write_mixxx(library, out, mixxx_options, resolve, progress)
        files, warnings = mx.files, warnings + mx.warnings
    else:
        raise ValueError(f"cannot write {options.format!r}")
    missing = sum(1 for p in local.values() if p is None)
    if missing and options.format != Format.REKORDBOX_XML:
        warnings.append(
            f"{missing} of {len(local)} track file(s) were not found locally (check the file access rules)."
        )
    if options.in_place:
        warnings.append(f"Backups of the previous files end in .cratemover-{stamp}.")
    return ConvertResult([str(f) for f in files], library.summary(), warnings)


# --- sync ----------------------------------------------------------------------------


@dataclass
class SyncSide:
    read: ReadOptions
    write: WriteOptions  # format/output_dir are filled in from ``read``


@dataclass
class SyncResult:
    a_to_b: dict[str, Any] | None = None
    b_to_a: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def invert_rules(rules: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(new, old) for old, new in rules]


def sync_libraries(
    a: SyncSide,
    b: SyncSide,
    direction: Direction,
    options: SyncOptions,
    dry_run: bool = False,
    progress: Progress = _noop,
    playlists: list[str] | None = None,
) -> SyncResult:
    """Merge A into B and/or B into A and write the results in place.

    ``options.path_rules`` map A's paths to B's; they are inverted for B -> A.
    ``playlists`` limits what A sends to B to those playlists and their tracks.
    """
    progress("Reading library A")
    lib_a = read_library(a.read, progress)
    progress("Reading library B")
    lib_b = read_library(b.read, progress)
    result = SyncResult()
    rules_ab = options.path_rules
    jobs = []
    if direction in (Direction.A_TO_B, Direction.BOTH):
        jobs.append((Direction.A_TO_B, lib_b, lib_a, b, rules_ab))
    if direction in (Direction.B_TO_A, Direction.BOTH):
        # A two-way sync keeps one preference: flip it so the same side wins both times.
        jobs.append((Direction.B_TO_A, lib_a, lib_b, a, invert_rules(rules_ab)))
    for name, base, incoming, side, rules in jobs:
        opts = copy.copy(options)
        opts.path_rules = rules
        if direction == Direction.BOTH and name == Direction.B_TO_A:
            opts.prefer = Prefer.BASE if options.prefer is Prefer.INCOMING else Prefer.INCOMING
        if name == Direction.A_TO_B and playlists:
            incoming = select(incoming, playlists)
        progress(f"Merging ({name.replace('_', ' ')})")
        merged, report = merge(base, incoming, opts)
        outcome: dict[str, Any] = {"report": report.as_dict(), "summary": merged.summary()}
        if not dry_run and (
            report.added or report.updated or report.playlists_added or report.playlists_updated
        ):
            write = copy.copy(side.write)
            write.format = side.read.format
            write.output_dir = side.read.path
            write.in_place = True
            write.path_rules = []
            write.playlists = []
            if side.read.format == Format.SERATO:
                write.serato_root = side.read.serato_root
            progress(f"Writing ({name.replace('_', ' ')})")
            outcome["written"] = write_library(
                merged, write, side.read.access_rules, progress
            ).as_dict()
        setattr(result, name, outcome)
    return result
