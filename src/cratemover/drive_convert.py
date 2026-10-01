"""One-button conversion of a USB drive's library, with a backup to undo it.

``convert_drive`` backs the drive up, reads the library on it, and writes the
target format(s) onto the same drive, pointing at the audio already there:

* the backup always holds the library folders (``PIONEER``, ``_Serato_``) and,
  for every audio file whose Serato tags will change, the original tag values,
  so a restore is exact without copying the audio; ``full`` copies the whole
  drive instead;
* by default the original library is kept, so the drive works in both programs;
  with ``keep_source=False`` it is removed after a successful conversion.

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

Progress = Callable[[str], None]
LIBRARY_DIRS = ("PIONEER", ".PIONEER", "_Serato_")
TARGETS = ("rekordbox_usb", "serato")
_SERATO_ID3 = ("GEOB:Serato Markers2", "GEOB:Serato Markers_", "GEOB:Serato BeatGrid")
_SERATO_VORBIS = ("SERATO_MARKERS_V2", "SERATO_BEATGRID", "SERATO_MARKERS2")
_SERATO_MP4 = (
    "----:com.serato.dj:markersv2",
    "----:com.serato.dj:markers",
    "----:com.serato.dj:beatgrid",
)


@dataclass
class DriveConvertOptions:
    targets: list[str]
    source_format: str = ""  # "" = the only library on the drive
    backup_dir: str = ""
    full_backup: bool = False
    keep_source: bool = True
    serato_write_tags: bool = True
    waveforms: bool = True
    onelibrary: str = "auto"
    mp3_decoder: str = "MAD"
    xml_root: str = ""  # how the rekordbox computer sees this drive, for rekordbox.xml


@dataclass
class DriveConvertResult:
    backup: str
    source_format: str
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
    root = root.resolve()
    found = {lib["format"]: lib for lib in _libraries(root)}
    if not found:
        raise ValueError(f"no Rekordbox or Serato library on {root}")
    source_format = options.source_format or next(iter(found))
    if source_format not in found:
        raise ValueError(f"no {source_format} library on {root}")
    targets = [t for t in options.targets if t in TARGETS and t != source_format]
    if not targets:
        raise ValueError("choose a target format different from the drive's library")
    source = found[source_format]
    read = ReadOptions(
        format=source_format,  # type: ignore[arg-type]
        path=source["path"],
        serato_root=source.get("serato_root", str(root)),
        mp3_decoder=options.mp3_decoder,  # type: ignore[arg-type]
    )
    progress("Reading the drive's library")
    library = read_library(read, progress)

    # Files whose Serato tags will be rewritten, so the backup can undo it.
    snapshot: list[Path] = []
    if "serato" in targets and options.serato_write_tags:
        for track in library.tracks.values():
            local = Path(track.extra.get("local", track.location))
            if local.is_file() and root in local.resolve().parents and (track.cues or track.grid):
                snapshot.append(local)
    backup_root = Path(options.backup_dir or root.parent / "cratemover-backups")
    backup = backup_drive(root, backup_root, options.full_backup, snapshot, progress)
    result = DriveConvertResult(backup=str(backup), source_format=source_format)
    before = _files(root)

    for target in targets:
        progress(f"Writing {target.replace('_', ' ')}")
        write = WriteOptions(
            format=target,  # type: ignore[arg-type]
            output_dir=str(root),
            in_place=True,
            copy_missing=False,  # a drive conversion only uses the audio already on it
            serato_root=str(root),
            serato_write_tags=options.serato_write_tags,
            waveforms=options.waveforms,
            onelibrary=options.onelibrary,
            mp3_decoder=options.mp3_decoder,  # type: ignore[arg-type]
            usb_xml_root=options.xml_root,
        )
        written = write_library(library, write, read.access_rules, progress)
        result.written[target] = written.as_dict()
        result.warnings += written.warnings

    # Remember files the conversion added (e.g. MP3s made from Ogg), so a restore removes them.
    created = sorted(
        f
        for f in _files(root) - before
        if f.split("/", 1)[0] not in LIBRARY_DIRS and ".cratemover-" not in f
    )
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_files"] = created
    manifest_path.write_text(json.dumps(manifest, indent=1))

    if not options.keep_source:
        for name in ("PIONEER", ".PIONEER") if source_format == "rekordbox_usb" else ("_Serato_",):
            if (root / name).is_dir():
                shutil.rmtree(root / name)
                result.removed.append(name)
        if source_format == "rekordbox_usb" and (root / "rekordbox.xml").is_file():
            (root / "rekordbox.xml").unlink()
    return result
