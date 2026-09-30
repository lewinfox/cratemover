"""Read a library in one format, remap paths, write it in another."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import grid as gridlib
from .keys import KeyNotation
from .mixxx import MixxxReadOptions, MixxxWriteOptions, read_mixxx, write_mixxx
from .model import Library, Playlist, normalise_path
from .offsets import Mp3Decoder
from .rekordbox_xml import RekordboxWriteOptions, read_rekordbox_xml, write_rekordbox_xml
from .serato.library import SeratoReadOptions, SeratoWriteOptions, read_serato, write_serato

Format = Literal["mixxx", "rekordbox_xml", "serato"]
FORMATS: dict[str, str] = {
    "mixxx": "Mixxx (mixxxdb.sqlite)",
    "rekordbox_xml": "Rekordbox XML",
    "serato": "Serato (_Serato_ folder)",
}

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


# --- paths ---------------------------------------------------------------------------


def apply_rules(path: str, rules: list[tuple[str, str]]) -> str:
    """Replace the longest matching prefix. Matching ignores slash direction and case."""
    norm = normalise_path(path)
    best: tuple[str, str] | None = None
    for old, new in rules:
        old_n = normalise_path(old).rstrip("/")
        if not old_n:
            continue
        if (norm.lower() == old_n.lower() or norm.lower().startswith(old_n.lower() + "/")) and (
            best is None or len(old_n) > len(normalise_path(best[0]).rstrip("/"))
        ):
            best = (old, new)
    if best is None:
        return path
    old_n = normalise_path(best[0]).rstrip("/")
    new = best[1].rstrip("/\\")
    rest = norm[len(old_n) :]
    result = new + rest
    return result.replace("/", "\\") if "\\" in best[1] else result


def parse_rules(text: str | list[Any] | None) -> list[tuple[str, str]]:
    """Rules as ``[[from, to], ...]`` or lines of ``from => to``."""
    if not text:
        return []
    if isinstance(text, list):
        return [(str(a), str(b)) for a, b in text if str(a).strip()]
    rules = []
    for line in text.splitlines():
        if "=>" in line:
            old, new = line.split("=>", 1)
            if old.strip():
                rules.append((old.strip(), new.strip()))
    return rules


def make_resolver(access_rules: list[tuple[str, str]]) -> Callable[[str], Path | None]:
    """Library location -> an existing local file, applying the file-access rules."""

    def resolve(location: str) -> Path | None:
        candidate = apply_rules(location, access_rules)
        if len(candidate) >= 2 and candidate[1] == ":" and os.name != "nt":
            return None  # a Windows path we have no mapping for
        path = Path(candidate)
        return path if path.is_file() else None

    return resolve


# --- options -------------------------------------------------------------------------


@dataclass
class ReadOptions:
    format: Format
    path: str
    access_rules: list[tuple[str, str]] = field(default_factory=list)
    mp3_decoder: Mp3Decoder = "MAD"
    serato_root: str = "/"
    read_file_tags: bool = True


@dataclass
class WriteOptions:
    format: Format
    output_dir: str
    path_rules: list[tuple[str, str]] = field(default_factory=list)
    key_notation: KeyNotation | None = None  # None: the target's usual notation
    mp3_decoder: Mp3Decoder = "MAD"
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
    # Selection: playlist paths ("Folder / Name"); empty means everything
    playlists: list[str] = field(default_factory=list)


def read_library(options: ReadOptions, progress: Progress = _noop) -> Library:
    resolve = make_resolver(options.access_rules)
    path = Path(options.path)
    if options.format == "mixxx":
        library = read_mixxx(
            path, MixxxReadOptions(mp3_decoder=options.mp3_decoder), resolve, progress
        )
    elif options.format == "rekordbox_xml":
        library = read_rekordbox_xml(path)
    elif options.format == "serato":
        library = read_serato(
            path,
            SeratoReadOptions(options.serato_root, options.read_file_tags),
            resolve,
            progress,
        )
    else:
        raise ValueError(f"unknown source format {options.format!r}")
    for track in library.tracks.values():
        track.grid = gridlib.simplify(track.grid)
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


def write_library(
    library: Library,
    options: WriteOptions,
    access_rules: list[tuple[str, str]],
    progress: Progress = _noop,
) -> ConvertResult:
    library = select(copy.deepcopy(library), options.playlists)
    # Find local files using source locations, then rewrite locations for the target.
    source_resolve = make_resolver(access_rules)
    local: dict[str, Path | None] = {}
    for track in library.tracks.values():
        found = source_resolve(track.location)
        track.location = apply_rules(track.location, options.path_rules)
        local[track.location] = found

    def resolve(location: str) -> Path | None:
        return local.get(location)

    out = Path(options.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    warnings = list(library.warnings)
    if options.format == "rekordbox_xml":
        rb = write_rekordbox_xml(
            library,
            out / "rekordbox.xml",
            RekordboxWriteOptions(
                key_notation=options.key_notation or KeyNotation.CAMELOT,
                memory_cues=options.rekordbox_memory_cues,
                hot_cues_as_memory=options.rekordbox_hot_cues_as_memory,
            ),
        )
        files, warnings = rb.files, warnings + rb.warnings
    elif options.format == "serato":
        se = write_serato(
            library,
            out,
            SeratoWriteOptions(
                volume_root=options.serato_root,
                key_notation=options.key_notation or KeyNotation.MUSICAL,
                write_file_tags=options.serato_write_tags,
                max_hot_cues=options.serato_max_hot_cues,
                memory_cues_to_hot_cues=options.memory_cues_to_hot_cues,
                base_database=Path(options.serato_base_database)
                if options.serato_base_database
                else None,
            ),
            resolve,
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
    elif options.format == "mixxx":
        mx = write_mixxx(
            library,
            out,
            MixxxWriteOptions(
                mp3_decoder=options.mp3_decoder,
                base_database=Path(options.mixxx_base_database)
                if options.mixxx_base_database
                else None,
                playlists_as_crates=options.mixxx_playlists_as_crates,
                memory_cues_to_hot_cues=options.memory_cues_to_hot_cues,
                key_notation=options.key_notation or KeyNotation.MUSICAL,
            ),
            resolve,
            progress,
        )
        files, warnings = mx.files, warnings + mx.warnings
    else:
        raise ValueError(f"unknown target format {options.format!r}")
    missing = sum(1 for p in local.values() if p is None)
    if missing and options.format != "rekordbox_xml":
        warnings.append(
            f"{missing} of {len(local)} track file(s) were not found locally (check the file access rules)."
        )
    return ConvertResult([str(f) for f in files], library.summary(), warnings)
