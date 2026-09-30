"""Read and write a Rekordbox USB stick (``PIONEER/rekordbox/export.pdb`` + analysis files).

Writing produces a fresh device library in place on a mounted stick:

* audio already on the stick is referenced where it is (so a Serato or Mixxx
  library on the same stick converts without copying); other tracks are copied
  to ``/Contents/<artist>/<album>/`` if allowed;
* ``export.pdb`` is rebuilt (the old one is kept as a backup);
* each track gets ``.DAT``/``.EXT``/``.2EX`` analysis files at the hashed path
  players compute. Waveforms are reused from existing analysis files when
  possible, otherwise measured with ffmpeg, otherwise flat placeholders.

Stale files that would contradict the new database (``exportLibrary.db`` and
``exportExt.pdb``) are moved aside. Never check a stick by plugging it into
Rekordbox: it rewrites sticks it mounts.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import unicodedata
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath

import mutagen

from ..colours import REKORDBOX_TRACK_COLOURS, rekordbox_track_colour
from ..keys import KeyNotation, format_key, parse_key
from ..model import Library, Playlist, Track
from . import anlz, waveform
from .pdb import FILE_TYPE_ALAC, FILE_TYPES, Pdb, PlaylistNode, TrackRow, read_pdb, write_pdb

Resolver = Callable[[str], Path | None]
Progress = Callable[[str], None]
_TRACK_COLOURS = list(REKORDBOX_TRACK_COLOURS)  # color ids 1-8 in this order


def find_stick_root(path: Path) -> Path:
    """The mount point, given it, its PIONEER folder or the export.pdb itself."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        for folder in ("PIONEER", ".PIONEER"):
            for name in ("export.pdb", "exportLibrary.db"):
                if (candidate / folder / "rekordbox" / name).is_file():
                    return candidate
    raise FileNotFoundError(f"no PIONEER/rekordbox/export.pdb at or above {path}")


def pioneer_dir(root: Path) -> Path:
    hidden = root / ".PIONEER"
    return hidden if hidden.is_dir() and not (root / "PIONEER").is_dir() else root / "PIONEER"


# --- reading --------------------------------------------------------------------------


def _date(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10]) if text else None
    except ValueError:
        return None


def _load_anlz(path: Path) -> anlz.AnlzFile | None:
    try:
        return anlz.AnlzFile.parse(path.read_bytes())
    except (OSError, ValueError):
        return None


def read_rekordbox_usb(path: Path, progress: Progress = print) -> Library:
    root = find_stick_root(path)
    rekordbox_dir = pioneer_dir(root) / "rekordbox"
    if (rekordbox_dir / "export.pdb").is_file():
        pdb = read_pdb((rekordbox_dir / "export.pdb").read_bytes())
    else:
        from .onelibrary import read_onelibrary

        pdb = read_onelibrary(rekordbox_dir / "exportLibrary.db")
    library = Library(source=f"Rekordbox USB at {root}")
    missing_analysis = 0
    for n, row in enumerate(pdb.tracks, 1):
        if n % 500 == 0:
            progress(f"Reading tracks {n}/{len(pdb.tracks)}")
        album = pdb.albums.get(row.album_id, ("", 0))[0]
        track = Track(
            id=f"rbusb:{row.id}",
            location=str(root) + row.file_path if str(root) != "/" else row.file_path,
            title=row.title,
            artist=pdb.artists.get(row.artist_id, ""),
            album=album,
            genre=pdb.genres.get(row.genre_id, ""),
            composer=pdb.artists.get(row.composer_id, ""),
            label=pdb.labels.get(row.label_id, ""),
            remixer=pdb.artists.get(row.remixer_id, ""),
            comment=row.comment,
            year=str(row.year) if row.year else "",
            track_number=row.track_number or None,
            duration_s=float(row.duration),
            sample_rate=row.sample_rate,
            bitrate=row.bitrate,
            file_size=row.file_size,
            bpm=row.tempo / 100.0,
            key=parse_key(pdb.keys.get(row.key_id)),
            rating=min(5, row.rating),
            colour=_TRACK_COLOURS[row.color_id - 1] if 1 <= row.color_id <= 8 else None,
            play_count=row.play_count,
            date_added=_date(row.date_added),
        )
        dat_path = root / (
            row.analyze_path or anlz.anlz_dir(row.file_path) + "/ANLZ0000.DAT"
        ).lstrip("/")
        dat = _load_anlz(dat_path)
        if dat is None:
            fallback = root / (anlz.anlz_dir(row.file_path) + "/ANLZ0000.DAT").lstrip("/")
            dat, dat_path = _load_anlz(fallback), fallback
        ext = _load_anlz(dat_path.with_suffix(".EXT"))
        if dat is None:
            missing_analysis += 1
        else:
            track.grid = anlz.read_grid(dat)
            track.extra["anlz"] = str(dat_path)
        track.cues = anlz.read_cues(dat, ext)
        library.add_track(track)
    if missing_analysis:
        library.warnings.append(
            f"{missing_analysis} track(s) have no analysis file (no grid or cues)."
        )

    nodes = {n.id: n for n in pdb.playlists}
    built: dict[int, Playlist] = {}
    for node in sorted(pdb.playlists, key=lambda n: (n.parent, n.order)):
        built[node.id] = (
            Playlist.folder(node.name)
            if node.is_folder
            else Playlist(
                node.name, [f"rbusb:{t}" for t in node.track_ids if f"rbusb:{t}" in library.tracks]
            )
        )
    for node in sorted(pdb.playlists, key=lambda n: (n.parent, n.order)):
        parent = built.get(node.parent) if node.parent in nodes else None
        (parent or library.playlists).children.append(built[node.id])
    return library


# --- writing --------------------------------------------------------------------------


@dataclass
class UsbWriteOptions:
    copy_missing: bool = True  # copy tracks that aren't on the stick into /Contents
    key_notation: KeyNotation = KeyNotation.MUSICAL
    waveforms: bool = True  # measure waveforms with ffmpeg (else reuse or placeholders)
    device_name: str = ""
    workers: int = 0  # waveform processes; 0 = CPU count
    transcode: bool = True  # convert formats players can't read (Ogg, Opus, WMA...) to MP3
    # OneLibrary (exportLibrary.db) for CDJ-3000X/XDJ-AZ/OPUS-QUAD etc.: "auto" writes it when the
    # stick already has one. Experimental: no hardware test of a third-party one is published.
    onelibrary: str = "auto"


@dataclass
class UsbWriteResult:
    files: list[Path] = field(default_factory=list)
    tracks: int = 0
    copied: int = 0
    transcoded: int = 0
    measured: int = 0
    reused: int = 0
    placeholders: int = 0
    warnings: list[str] = field(default_factory=list)


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe(name: str, limit: int = 80) -> str:
    """A FAT32-safe path component in NFC (players look files up by the NFC string)."""
    name = unicodedata.normalize("NFC", _UNSAFE.sub("_", name)).strip().rstrip(".")
    return (name[:limit].rstrip(" .") or "Unknown").strip()


def _stick_path(local: Path, root: Path) -> str | None:
    try:
        return "/" + local.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _copy_to_stick(local: Path, track: Track, root: Path) -> tuple[str, bool]:
    """The file's path on the stick, and whether it had to be copied."""
    stem, suffix = os.path.splitext(local.name)
    rel = PurePosixPath(
        "Contents",
        _safe(track.artist or "Unknown Artist"),
        _safe(track.album or "Unknown Album"),
        _safe(stem, 100) + suffix.lower(),
    )
    dest = root / rel
    if dest.is_file() and dest.stat().st_size == local.stat().st_size:
        return "/" + rel.as_posix(), False
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    shutil.copyfile(local, partial)
    os.replace(partial, dest)
    return "/" + rel.as_posix(), True


# Formats Pioneer players cannot play.
UNPLAYABLE = {"ogg", "oga", "opus", "wma", "ape", "wv", "caf", "mka", "webm"}
# LAME's encoder delay, which Rekordbox plays and the source decoders skip
# (measured for FLAC -> MP3 in mixxx-to-rekordbox).
TRANSCODE_OFFSET_MS = 26.0


def _transcode(local: Path, track: Track, root: Path) -> tuple[str, bool]:
    rel = PurePosixPath(
        "Contents",
        _safe(track.artist or "Unknown Artist"),
        _safe(track.album or "Unknown Album"),
        _safe(local.stem, 100) + ".mp3",
    )
    dest = root / rel
    if dest.is_file():
        return "/" + rel.as_posix(), False
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.stem + ".part.mp3")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(local), "-map", "0:a:0",
         "-c:a", "libmp3lame", "-b:a", "320k", "-map_metadata", "0", str(partial)],
        check=True,
        capture_output=True,
    )  # fmt: skip
    os.replace(partial, dest)
    return "/" + rel.as_posix(), True


def _shifted(track: Track, ms: float) -> Track:
    moved = copy.deepcopy(track)
    for cue in moved.cues:
        cue.position_ms += ms
        if cue.end_ms is not None:
            cue.end_ms += ms
    for marker in moved.grid:
        marker.position_ms += ms
    return moved


@dataclass
class _Item:
    track: Track
    local: Path | None
    usb_path: str
    row: TrackRow


def _audio_facts(local: Path | None, track: Track) -> tuple[int, int, int, int, int, int]:
    """(sample rate, bits per sample, kbps, seconds, file type, mp3 frames)."""
    rate, depth, kbps, seconds = (
        track.sample_rate or 44100,
        16,
        track.bitrate,
        round(track.duration_s),
    )
    file_type = FILE_TYPES.get(track.extension, 1)
    frames = 0
    info = None
    if local is not None:
        try:
            audio = mutagen.File(local)
            info = audio.info if audio is not None else None
        except Exception:
            info = None
    if info is not None:
        rate = int(getattr(info, "sample_rate", 0) or rate)
        depth = int(getattr(info, "bits_per_sample", 0) or 16)
        kbps = int((getattr(info, "bitrate", 0) or 0) / 1000) or kbps
        seconds = round(getattr(info, "length", 0) or seconds)
        if getattr(info, "codec", "") == "alac":
            file_type = FILE_TYPE_ALAC
        if track.extension == "mp3":
            frames = round((getattr(info, "length", 0) or 0) * rate / 1152)
    return rate, depth, kbps, seconds, file_type, frames


def _measure(path: str) -> waveform.Waveforms | str:
    try:
        return waveform.analyze(waveform.measure(Path(path)))
    except Exception as exc:  # decode failures fall back to placeholders
        return f"{type(exc).__name__}: {exc}"


def _reusable(
    track: Track, root: Path, anlz_path: str, usb_path: str
) -> tuple[anlz.AnlzFile, ...] | None:
    """Existing analysis (DAT, EXT[, 2EX]) for this audio: on the stick, or from the source library."""
    candidates = [root / anlz_path.lstrip("/")]
    if "anlz" in track.extra:
        candidates.append(Path(track.extra["anlz"]))
    for dat_path in candidates:
        dat, ext = _load_anlz(dat_path), _load_anlz(dat_path.with_suffix(".EXT"))
        if dat is None or ext is None or not dat.get("PWAV") or not ext.get("PWV3"):
            continue
        # A file on the stick must describe this audio; a source library's may describe
        # the same audio at another path.
        if dat_path == candidates[0] and dat.path != usb_path:
            continue
        two = _load_anlz(dat_path.with_suffix(".2EX"))
        return (dat, ext, two) if two is not None else (dat, ext)
    return None


def _build_files(
    item: _Item,
    duration_ms: float,
    mp3_frames: int,
    waves: waveform.Waveforms | None,
    reuse: tuple[anlz.AnlzFile, ...] | None,
) -> tuple[anlz.AnlzFile, anlz.AnlzFile, anlz.AnlzFile | None]:
    t = item.track
    hot, memory = anlz.cue_lists(t.cues)
    bpm = t.grid[0].bpm if t.grid else t.bpm
    grid = anlz.pqtz(t.grid, duration_ms)
    path = anlz.ppth(item.usb_path)
    cues_dat = [
        anlz.pcob(anlz.HOT, [c for c in hot if (c.slot or 0) < anlz.DAT_HOT_CUES]),
        anlz.pcob(anlz.MEMORY, memory),
    ]
    cues_ext = [
        anlz.pcob(anlz.HOT, [c for c in hot if (c.slot or 0) >= anlz.DAT_HOT_CUES]),
        anlz.pcob(anlz.MEMORY, []),
        anlz.pco2(anlz.HOT, hot, bpm),
        anlz.pco2(anlz.MEMORY, memory, bpm),
    ]
    if reuse is not None:
        old_dat, old_ext = reuse[0], reuse[1]

        def keep(f: anlz.AnlzFile, tag: str) -> list[anlz.Section]:
            return f.all(tag)

        dat = anlz.AnlzFile(
            [
                path,
                *keep(old_dat, "PVBR"),
                grid,
                *keep(old_dat, "PWAV"),
                *keep(old_dat, "PWV2"),
                *cues_dat,
            ]
        )
        ext = anlz.AnlzFile(
            [
                path,
                *keep(old_ext, "PWV3"),
                *cues_ext,
                anlz.pqt2_empty(),
                *keep(old_ext, "PWV5"),
                *keep(old_ext, "PWV4"),
            ]
        )
        two = None
        if len(reuse) > 2:
            two = anlz.AnlzFile([path, *(s for s in reuse[2].sections if s.tag != "PPTH")])
        return dat, ext, two
    assert waves is not None
    dat = anlz.AnlzFile([path, anlz.pvbr(mp3_frames), grid, *waves.dat_sections(), *cues_dat])
    ext = anlz.AnlzFile(
        [
            path,
            waves.pwv3_section(),
            *cues_ext,
            anlz.pqt2_empty(),
            waves.pwv5_section(),
            waves.pwv4_section(),
        ]
    )
    two = anlz.AnlzFile([path, *waves.two_ex_sections()])
    return dat, ext, two


def _move_aside(path: Path, stamp: str, result: UsbWriteResult, why: str) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.djconvert-{stamp}")
        os.replace(path, backup)
        result.warnings.append(f"Moved {path.name} aside to {backup.name}: {why}")


def write_rekordbox_usb(
    library: Library,
    root: Path,
    options: UsbWriteOptions,
    resolve: Resolver,
    progress: Progress = print,
) -> UsbWriteResult:
    result = UsbWriteResult()
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a mounted drive or folder")
    pioneer = pioneer_dir(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    today = date.today().isoformat()

    items: list[_Item] = []
    skipped: list[str] = []
    unplayable: list[str] = []
    ids: dict[str, int] = {}
    names: dict[str, dict[str, int]] = {
        "artist": {},
        "album": {},
        "genre": {},
        "label": {},
        "key": {},
    }
    pdb = Pdb(device_name=options.device_name, export_date=today)

    def name_id(kind: str, name: str) -> int:
        if not name:
            return 0
        table = names[kind]
        if name not in table:
            table[name] = len(table) + 1
        return table[name]

    total = len(library.tracks)
    for n, track in enumerate(library.tracks.values(), 1):
        if n % 100 == 0:
            progress(f"Placing tracks {n}/{total}")
        local = resolve(track.location)
        usb_path: str | None = None
        if track.extension in UNPLAYABLE:
            if local is None or not options.transcode or not waveform.have_ffmpeg():
                unplayable.append(track.display_name)
                continue
            try:
                usb_path, converted = _transcode(local, track, root)
            except (subprocess.CalledProcessError, OSError) as exc:
                result.warnings.append(f"{track.display_name}: could not convert to MP3: {exc}")
                continue
            local = root / usb_path.lstrip("/")
            track = _shifted(track, TRANSCODE_OFFSET_MS)
            result.transcoded += converted
        elif local is not None:
            usb_path = _stick_path(local, root)
            if usb_path is None and options.copy_missing:
                usb_path, copied = _copy_to_stick(local, track, root)
                result.copied += copied
        if usb_path is None:
            skipped.append(track.display_name)
            continue
        track_id = len(items) + 1
        ids[track.id] = track_id
        key_name = format_key(track.key, options.key_notation)
        colour = rekordbox_track_colour(track.colour)
        row = TrackRow(
            id=track_id,
            title=track.title or PurePosixPath(usb_path).stem,
            file_path=usb_path,
            filename=PurePosixPath(usb_path).name,
            artist_id=name_id("artist", track.artist),
            album_id=name_id("album", track.album),
            genre_id=name_id("genre", track.genre),
            label_id=name_id("label", track.label),
            key_id=name_id("key", key_name),
            remixer_id=name_id("artist", track.remixer),
            composer_id=name_id("artist", track.composer),
            tempo=round(track.bpm * 100),
            track_number=track.track_number or 0,
            play_count=track.play_count,
            year=int(track.year[:4]) if track.year[:4].isdigit() else 0,
            color_id=_TRACK_COLOURS.index(colour) + 1 if colour is not None else 0,
            rating=max(0, min(5, track.rating)),
            date_added=(track.date_added or date.today()).isoformat(),
            analyze_date=today,
            comment=track.comment,
        )
        items.append(_Item(track, local, usb_path, row))

    # Analysis files: reuse, measure (in parallel), or placeholders.
    slots: dict[str, int] = {}
    facts: dict[int, tuple[int, int, int, int, int, int]] = {}
    reuse: dict[int, tuple[anlz.AnlzFile, ...]] = {}
    to_measure: dict[int, str] = {}
    for item in items:
        folder = anlz.anlz_dir(item.usb_path)
        number = slots.get(folder, 0)
        slots[folder] = number + 1
        item.row.analyze_path = f"{folder}/ANLZ{number:04d}.DAT"
        rate, depth, kbps, seconds, file_type, frames = facts[item.row.id] = _audio_facts(
            item.local, item.track
        )
        item.row.sample_rate, item.row.sample_depth, item.row.bitrate = rate, depth, kbps
        item.row.duration, item.row.file_type = seconds, file_type
        item.row.file_size = item.local.stat().st_size if item.local else item.track.file_size
        found = _reusable(item.track, root, item.row.analyze_path, item.usb_path)
        if found is not None:
            reuse[item.row.id] = found
        elif options.waveforms and item.local is not None and waveform.have_ffmpeg():
            to_measure[item.row.id] = str(item.local)

    measured: dict[int, waveform.Waveforms] = {}
    if to_measure:
        workers = options.workers or os.cpu_count() or 1
        progress(f"Measuring waveforms for {len(to_measure)} track(s)")
        with ProcessPoolExecutor(max_workers=min(workers, len(to_measure))) as pool:
            for n, (track_id, outcome) in enumerate(
                zip(to_measure, pool.map(_measure, to_measure.values()), strict=True), 1
            ):
                if n % 10 == 0:
                    progress(f"Measured waveforms {n}/{len(to_measure)}")
                if isinstance(outcome, str):
                    result.warnings.append(f"Waveform failed for track {track_id}: {outcome}")
                else:
                    measured[track_id] = outcome

    progress("Writing analysis files")
    for item in items:
        track_id = item.row.id
        seconds, frames = facts[track_id][3], facts[track_id][5]
        waves = measured.get(track_id)
        found = reuse.get(track_id)
        if found is not None:
            result.reused += 1
        elif waves is not None:
            result.measured += 1
        else:
            waves = waveform.placeholder(seconds or item.track.duration_s)
            result.placeholders += 1
        duration_ms = (seconds or item.track.duration_s) * 1000.0
        dat, ext, two = _build_files(item, duration_ms, frames, waves, found)
        base = root / item.row.analyze_path.lstrip("/")
        base.parent.mkdir(parents=True, exist_ok=True)
        for f, suffix in ((dat, ".DAT"), (ext, ".EXT"), (two, ".2EX")):
            if f is not None:
                target = base.with_suffix(suffix)
                partial = target.with_name(target.name + ".part")
                partial.write_bytes(f.to_bytes())
                os.replace(partial, target)
        pdb.tracks.append(item.row)

    pdb.artists = {v: k for k, v in names["artist"].items()}
    pdb.albums = {v: (k, 0) for k, v in names["album"].items()}
    pdb.genres = {v: k for k, v in names["genre"].items()}
    pdb.labels = {v: k for k, v in names["label"].items()}
    pdb.keys = {v: k for k, v in names["key"].items()}

    next_id = 1

    def add_nodes(node: Playlist, parent: int) -> None:
        nonlocal next_id
        for order, child in enumerate(node.children):
            node_id = next_id
            next_id += 1
            track_ids = [ids[t] for t in child.track_ids or [] if t in ids]
            pdb.playlists.append(
                PlaylistNode(node_id, parent, order, child.name, child.is_folder, track_ids)
            )
            if child.is_folder:
                add_nodes(child, node_id)

    add_nodes(library.playlists, 0)

    rekordbox_dir = pioneer / "rekordbox"
    rekordbox_dir.mkdir(parents=True, exist_ok=True)
    db_path = rekordbox_dir / "export.pdb"
    if db_path.exists():
        shutil.copyfile(db_path, db_path.with_name(f"export.pdb.djconvert-{stamp}"))
    partial = db_path.with_name("export.pdb.part")
    partial.write_bytes(write_pdb(pdb))
    os.replace(partial, db_path)
    result.files.append(db_path)
    _move_aside(
        rekordbox_dir / "exportExt.pdb", stamp, result, "its My Tags refer to the old track ids"
    )
    one_library = rekordbox_dir / "exportLibrary.db"
    if options.onelibrary == "on" or (options.onelibrary == "auto" and one_library.exists()):
        from .onelibrary import write_onelibrary

        if one_library.exists():
            shutil.copyfile(
                one_library, one_library.with_name(f"exportLibrary.db.djconvert-{stamp}")
            )
        try:
            write_onelibrary(pdb, one_library)
            result.files.append(one_library)
            result.warnings.append(
                "Wrote a OneLibrary database (exportLibrary.db) too. This is experimental: test it on "
                "your player before relying on it."
            )
        except RuntimeError as exc:
            result.warnings.append(f"OneLibrary not written: {exc}")
    else:
        for name in ("exportLibrary.db", "exportLibrary.db-wal", "exportLibrary.db-shm"):
            _move_aside(
                rekordbox_dir / name,
                stamp,
                result,
                "this OneLibrary database no longer matches export.pdb (CDJ-3000X, XDJ-AZ, OPUS-QUAD "
                "and other OneLibrary-only players need it; turn on OneLibrary to rewrite it)",
            )
    result.tracks = len(items)
    if skipped:
        result.warnings.append(
            f"{len(skipped)} track(s) not on the stick and not copied (file not found, or copying off), "
            f"e.g. {', '.join(skipped[:3])}."
        )
    if unplayable:
        result.warnings.append(
            f"{len(unplayable)} track(s) left out: Pioneer players can't play their format and "
            f"converting to MP3 needs ffmpeg and the audio file (e.g. {', '.join(unplayable[:3])})."
        )
    if result.transcoded:
        result.warnings.append(
            f"{result.transcoded} track(s) converted to MP3 on the stick; their cues and grids were "
            f"moved {TRANSCODE_OFFSET_MS:.0f} ms later for the MP3 encoder delay."
        )
    if result.placeholders:
        why = (
            "ffmpeg is not installed"
            if not waveform.have_ffmpeg()
            else "their audio could not be decoded or found"
        )
        result.warnings.append(
            f"{result.placeholders} track(s) got flat placeholder waveforms because {why}."
        )
    return result
