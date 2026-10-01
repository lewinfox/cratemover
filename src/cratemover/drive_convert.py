"""Convert a USB drive from one DJ program's library to another's, with a backup to undo it.

``convert_drive`` replaces library X on the drive with library Y, and does nothing else:

* it reads X, writes Y pointing at the audio already on the drive, then deletes X's folder
  (``PIONEER`` or ``_Serato_``). Audio is only added where Y can't play a file as it is (an
  Ogg file becomes an MP3 for Rekordbox), and only changed where Y keeps its data inside the
  audio (Serato's cues and grids);
* nothing else is put on the drive: no ``rekordbox.xml``, no ``*.cratemover-*`` backups, no
  files moved aside. It refuses a drive that already has a Y library, so there's never an old
  Y to back up or merge with;
* the backup goes on the computer (``backup_dir``): the library folders and, for every audio
  file whose Serato tags will change, the original tag values, so a restore is exact without
  copying the audio; ``full`` copies the whole drive instead.

``restore_drive`` puts a backup back.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .convert import ReadOptions, WriteOptions, read_library, write_library
from .devices import _libraries
from .model import Format, Library, Track
from .offsets import Mp3Decoder
from .pioneer.usb import TRANSCODE_OFFSET_MS, OneLibraryMode

Progress = Callable[[str], None]
LIBRARY_DIRS = ("PIONEER", ".PIONEER", "_Serato_")
TARGETS = (Format.REKORDBOX_USB, Format.SERATO)
_SERATO_ID3 = ("GEOB:Serato Markers2", "GEOB:Serato Markers_", "GEOB:Serato BeatGrid")
_SERATO_VORBIS = ("SERATO_MARKERS_V2", "SERATO_BEATGRID", "SERATO_MARKERS2")
_SERATO_MP4 = (
    "----:com.serato.dj:markersv2",
    "----:com.serato.dj:markers",
    "----:com.serato.dj:beatgrid",
)


@dataclass
class DriveConvertOptions:
    target: Format
    source_format: Format | None = None  # None: the only library on the drive
    backup_dir: str = ""
    full_backup: bool = False
    waveforms: bool = True  # Rekordbox: measure waveforms with ffmpeg (else flat placeholders)
    onelibrary: bool = False  # Rekordbox: also write exportLibrary.db (experimental)
    mp3_decoder: Mp3Decoder = Mp3Decoder.MAD


@dataclass
class DriveConvertResult:
    backup: str
    source_format: Format
    written: dict[str, dict[str, Any]] = field(default_factory=dict)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _files(root: Path) -> set[str]:
    out = set()
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.add(str((Path(dirpath) / name).relative_to(root)))
    return out


def _drive_size(root: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(root):
        for name in files:
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / name).stat().st_size
    return total


# --- Serato tag snapshots ----------------------------------------------------------------


def _snapshot_tags(path: Path) -> dict[str, str | None]:
    """The current Serato tag values of one file (base64 of the raw value, or None)."""
    import mutagen
    from mutagen.mp4 import MP4FreeForm

    audio = mutagen.File(path)
    tags = audio.tags if audio is not None else None
    snap: dict[str, str | None] = {}
    suffix = path.suffix.lower()
    if suffix in (".mp3", ".aif", ".aiff", ".wav"):
        for key in _SERATO_ID3:
            frame = tags.get(key) if tags is not None else None
            snap[key] = base64.b64encode(frame.data).decode() if frame is not None else None
    elif suffix in (".flac", ".ogg"):
        for key in _SERATO_VORBIS:
            values = tags.get(key) if tags is not None else None
            snap[key] = base64.b64encode(values[0].encode()).decode() if values else None
    elif suffix in (".m4a", ".mp4"):
        for key in _SERATO_MP4:
            values = tags.get(key) if tags is not None else None
            snap[key] = base64.b64encode(bytes(MP4FreeForm(values[0]))).decode() if values else None
    return snap


def _restore_tags(path: Path, snap: dict[str, str | None]) -> None:
    import mutagen
    from mutagen.id3 import GEOB, ID3, ID3NoHeaderError
    from mutagen.mp4 import MP4FreeForm

    suffix = path.suffix.lower()
    if suffix == ".mp3":
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        for key, value in snap.items():
            tags.delall(key)
            if value is not None:
                desc = key.split(":", 1)[1]
                tags.add(GEOB(encoding=0, mime="application/octet-stream", filename="", desc=desc,
                              data=base64.b64decode(value)))  # fmt: skip
        tags.save(path, v2_version=4 if tags.version >= (2, 4, 0) else 3)
        return
    audio = mutagen.File(path)
    if audio is None:
        return
    if audio.tags is None:
        audio.add_tags()
    for key, value in snap.items():
        if key in audio.tags:
            del audio.tags[key]
        if value is None:
            continue
        raw = base64.b64decode(value)
        if suffix in (".aif", ".aiff", ".wav"):
            desc = key.split(":", 1)[1]
            audio.tags.add(
                GEOB(encoding=0, mime="application/octet-stream", filename="", desc=desc, data=raw)
            )
        elif suffix in (".m4a", ".mp4"):
            audio.tags[key] = [MP4FreeForm(raw)]
        else:
            audio.tags[key] = raw.decode()
    audio.save()


# --- backup and restore -------------------------------------------------------------------


def backup_drive(
    root: Path, backup_root: Path, full: bool, audio_to_snapshot: list[Path], progress: Progress
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_root / f"{root.name or 'drive'}-{stamp}"
    need = (
        _drive_size(root)
        if full
        else sum(_drive_size(root / d) for d in LIBRARY_DIRS if (root / d).is_dir())
    )
    backup_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(backup_root).free
    if need > free:
        raise OSError(
            f"the backup needs {need / 1e9:.1f} GB but {backup_root} has {free / 1e9:.1f} GB free"
        )
    dest.mkdir()
    progress(
        f"Backing up {'the whole drive' if full else 'the library folders'} ({need / 1e6:.0f} MB)"
    )
    if full:
        shutil.copytree(
            root,
            dest / "files",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".Trash*", "System Volume Information"),
        )
    else:
        for name in LIBRARY_DIRS:
            if (root / name).is_dir():
                shutil.copytree(root / name, dest / "files" / name)
    snapshots = {}
    for n, path in enumerate(audio_to_snapshot, 1):
        if n % 200 == 0:
            progress(f"Saving Serato tags {n}/{len(audio_to_snapshot)}")
        try:
            snapshots[str(path.relative_to(root))] = _snapshot_tags(path)
        except Exception:  # an unreadable file is left alone by the writer too
            continue
    manifest = {
        "drive": str(root),
        "label": root.name,
        "created": stamp,
        "full": full,
        "library_dirs": [d for d in LIBRARY_DIRS if (root / d).is_dir()],
        "serato_tags": snapshots,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return dest


def list_backups(backup_root: Path) -> list[dict[str, Any]]:
    out = []
    for manifest in sorted(backup_root.glob("*/manifest.json"), reverse=True):
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        out.append({
            "path": str(manifest.parent), "label": data.get("label", ""), "drive": data.get("drive", ""),
            "created": data.get("created", ""), "full": data.get("full", False),
            "size_bytes": _drive_size(manifest.parent),
        })  # fmt: skip
    return out


def restore_drive(backup: Path, root: Path, progress: Progress = print) -> list[str]:
    """Put a drive's library folders (or, for a full backup, every file) and Serato tags back."""
    manifest = json.loads((backup / "manifest.json").read_text())
    done = []
    files = backup / "files"
    for name in LIBRARY_DIRS:  # the conversion may have added library folders: remove them
        if (root / name).is_dir() and not (files / name).is_dir():
            shutil.rmtree(root / name)
            done.append(f"removed {name}")
    if manifest.get("full"):
        progress("Restoring every file")
        shutil.copytree(files, root, dirs_exist_ok=True)
        done.append("restored all files")
    else:
        for name in manifest.get("library_dirs", []):
            progress(f"Restoring {name}")
            if (root / name).is_dir():
                shutil.rmtree(root / name)
            shutil.copytree(files / name, root / name)
            done.append(f"restored {name}")
    removed = 0
    for rel in manifest.get("created_files", []):
        path = root / rel
        if path.is_file():
            path.unlink()
            removed += 1
            with contextlib.suppress(OSError):  # tidy up folders left empty
                for parent in path.parents:
                    if parent == root:
                        break
                    parent.rmdir()
    if removed:
        done.append(f"removed {removed} file(s) the conversion had added")
    restored = 0
    for rel, snap in manifest.get("serato_tags", {}).items():
        path = root / rel
        if path.is_file():
            try:
                _restore_tags(path, snap)
                restored += 1
            except Exception:
                continue
    if restored:
        done.append(f"restored Serato tags in {restored} file(s)")
    return done


# --- convert --------------------------------------------------------------------------------


def convert_drive(
    root: Path, options: DriveConvertOptions, progress: Progress = print
) -> DriveConvertResult:
    """Replace the drive's library with ``options.target``: back up, convert, verify, check.

    If the check or verification fails, the drive is restored from the backup and the error
    raised, so a drive never ends up with two libraries or a half-written one.
    """
    root = root.resolve()
    target = options.target
    if target not in TARGETS:
        raise ValueError(f"can't convert a drive to {target}")
    found = {Format(lib["format"]): lib for lib in _libraries(root)}
    if not found:
        raise ValueError(f"no Rekordbox or Serato library on {root}")
    if len(found) > 1:
        raise ValueError(
            f"{root} has both a Rekordbox and a Serato library. A drive should only have one: "
            "remove one of them first"
        )
    source_format = options.source_format or next(iter(found))
    if source_format not in found:
        raise ValueError(f"no {source_format} library on {root}")
    if source_format == target:
        raise ValueError("choose a target format different from the drive's library")
    source = found[source_format]
    read = ReadOptions(
        format=source_format,
        path=source["path"],
        serato_root=source.get("serato_root", str(root)),
        mp3_decoder=options.mp3_decoder,
    )
    progress("Reading the drive's library")
    library = read_library(read, progress)

    # 1. Back up to the computer. Files whose Serato tags will be rewritten are snapshotted.
    snapshot: list[Path] = []
    if target == Format.SERATO:
        for track in library.tracks.values():
            local = Path(track.extra.get("local", track.location))
            if local.is_file() and root in local.resolve().parents and (track.cues or track.grid):
                snapshot.append(local)
    backup_root = Path(options.backup_dir or root.parent / "cratemover-backups")
    backup = backup_drive(root, backup_root, options.full_backup, snapshot, progress)
    result = DriveConvertResult(backup=str(backup), source_format=source_format)
    before = _files(root)

    try:
        # 2. Convert: the old library goes first, so there's never a second one on the drive.
        for name in _library_dirs(source_format):
            if (root / name).is_dir():
                shutil.rmtree(root / name)
                result.removed.append(name)
        progress(f"Writing {target.replace('_', ' ')}")
        write = WriteOptions(
            format=target,
            output_dir=str(root),
            in_place=True,
            copy_missing=False,  # a drive conversion only uses the audio already on it
            serato_root=str(root),
            serato_write_tags=True,  # Serato keeps cues and grids in the audio files
            waveforms=options.waveforms,
            onelibrary=OneLibraryMode.ON if options.onelibrary else OneLibraryMode.OFF,
            mp3_decoder=options.mp3_decoder,
            usb_xml=False,
        )
        written = write_library(library, write, read.access_rules, progress)
        result.written[target] = written.as_dict()
        result.warnings += written.warnings
    finally:
        # Remember files the conversion added (e.g. MP3s made from Ogg), so a restore removes them.
        created = sorted(f for f in _files(root) - before if f.split("/", 1)[0] not in LIBRARY_DIRS)
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["created_files"] = created
        manifest_path.write_text(json.dumps(manifest, indent=1))

    # 3. Verify, and 4. check nothing else is on the drive.
    progress("Checking the drive")
    problems = verify_conversion(root, library, target, mp3_decoder=options.mp3_decoder)
    problems += check_drive(root, before, source_format, target, library)
    if problems:
        progress("The check failed: restoring the drive from the backup")
        restore_drive(backup, root, progress)
        raise ValueError(
            "The converted drive didn't pass its checks, so it was put back as it was:\n- "
            + "\n- ".join(problems)
        )
    return result


def _library_dirs(fmt: Format) -> tuple[str, ...]:
    return ("PIONEER", ".PIONEER") if fmt == Format.REKORDBOX_USB else ("_Serato_",)


def _key(track: Track, root: Path) -> str:
    """A track's audio file on the drive, without its extension (a transcode keeps its name)."""
    location = Path(track.extra.get("local") or track.location)
    if not location.is_absolute() or root not in location.parents:
        location = root / str(track.location).lstrip("/\\")
    with contextlib.suppress(ValueError):
        location = location.relative_to(root)
    return str(location.with_suffix("")).lower()


# Cue positions are whole milliseconds in every format, so a converted cue may move by rounding
# and nothing more, unless the audio itself was converted (see TRANSCODE_OFFSET_MS).
_CUE_TOLERANCE_MS = 1.0


def verify_conversion(
    root: Path, source: Library, target: Format, mp3_decoder: Mp3Decoder = Mp3Decoder.MAD
) -> list[str]:
    """Read the new library back and compare it with the one it was made from."""
    read = ReadOptions(
        format=target, path=str(root), serato_root=str(root), mp3_decoder=mp3_decoder
    )
    try:
        new = read_library(read)
    except Exception as exc:
        return [f"the new {target} library can't be read back: {exc}"]
    expected = {
        _key(t, root): t for t in source.tracks.values() if Path(t.extra.get("local", "")).is_file()
    }
    got = {_key(t, root): t for t in new.tracks.values()}
    problems = [f"track missing: {key}" for key in sorted(expected.keys() - got.keys())]
    max_slots = 8 if target == Format.REKORDBOX_USB else 16
    for key in sorted(expected.keys() & got.keys()):
        old, now = expected[key], got[key]
        # Audio converted to MP3 starts later by the encoder's delay; the writer moves cues to match.
        shift = TRANSCODE_OFFSET_MS if now.extension != old.extension else 0.0
        cues = {c.slot: c.position_ms - shift for c in now.hot_cues}
        for cue in old.hot_cues:
            if cue.slot is None or cue.slot >= max_slots:
                continue
            if cue.slot not in cues:
                problems.append(f"{key}: hot cue {cue.slot + 1} missing")
            elif abs(cues[cue.slot] - cue.position_ms) > _CUE_TOLERANCE_MS:
                problems.append(
                    f"{key}: hot cue {cue.slot + 1} moved from {cue.position_ms:.1f} ms "
                    f"to {cues[cue.slot] + shift:.1f} ms"
                    + (f" (expected +{shift:.0f} ms for the MP3 conversion)" if shift else "")
                )
        if old.grid and not now.grid:
            problems.append(f"{key}: beat grid missing")

    def lists(lib: Library, tracks: dict[str, Track]) -> dict[str, list[set[str]]]:
        by_id = {t.id: k for k, t in tracks.items()}
        out: dict[str, list[set[str]]] = {}
        for _, playlist in lib.playlists.walk():
            if playlist.track_ids is not None:
                members = {by_id[i] for i in playlist.track_ids if i in by_id}
                out.setdefault(playlist.name, []).append(members)
        return out

    old_lists, new_lists = lists(source, expected), lists(new, got)
    for name, versions in sorted(old_lists.items()):
        for members in versions:
            if members not in new_lists.get(name, []):
                problems.append(f"playlist {name!r} missing or has different tracks")
    return problems


def check_drive(
    root: Path, before: set[str], source: Format, target: Format, library: Library
) -> list[str]:
    """Only the new library and any audio converted for it may have been added; only the old
    library may have gone."""
    formats = {Format(lib["format"]) for lib in _libraries(root)}
    problems = [] if formats == {target} else [f"the drive has these libraries: {sorted(formats)}"]
    after = _files(root)
    old_dirs, new_dirs = _library_dirs(source), _library_dirs(target)
    for f in sorted(before - after):
        if f.split("/", 1)[0] not in old_dirs:
            problems.append(f"file removed outside the old library: {f}")
    for f in sorted(after & before):
        if f.split("/", 1)[0] in old_dirs:
            problems.append(f"old library file still there: {f}")
    audio = {_key(t, root) for t in library.tracks.values()}
    for f in sorted(after - before):
        if f.split("/", 1)[0] in new_dirs:
            continue
        if Path(f).suffix.lower() == ".mp3" and str(Path(f).with_suffix("")).lower() in audio:
            continue  # audio converted to MP3 so the target can play it
        problems.append(f"unexpected file added: {f}")
    for f in sorted(after):
        if ".cratemover-" in f or f.endswith(".part") or f.endswith(".part.mp3"):
            problems.append(f"leftover file: {f}")
    return problems
