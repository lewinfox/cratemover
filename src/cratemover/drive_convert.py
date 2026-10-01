"""Convert a USB drive from one DJ program's library to another's, with a backup to undo it.

``convert_drive`` replaces library X on the drive with library Y, and does nothing else:

* it reads X, writes Y pointing at the audio already on the drive, then deletes X's folder
  (``PIONEER`` or ``_Serato_``). Audio is only added where Y can't play a file as it is (an
  Ogg file becomes an MP3 for Rekordbox), and only changed where Y keeps its data inside the
  audio (Serato's cues and grids);
* nothing else is put on the drive: no ``rekordbox.xml``, no ``*.cratemover-*`` backups, no
  files moved aside. It refuses a drive that already has a Y library, so there's never an old
  Y to back up or merge with;
* the backup goes on the computer (``backup_dir``): a byte-for-byte copy of the library folder
  and every audio file the library uses (``full``: every file on the drive), with each file's
  checksum. A restore copies them back and checks every checksum.

``restore_drive`` puts a backup back.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import fingerprint, layout
from .convert import ReadOptions, WriteOptions, read_library, write_library
from .devices import _libraries, drive_users
from .fingerprint import SCHEMA as FINGERPRINT_SCHEMA
from .model import Format, Library
from .offsets import Mp3Decoder
from .pioneer.usb import OneLibraryMode

Progress = Callable[[str], None]


def _steps(progress: Progress, total: int) -> None:
    """A per-file step of ``total`` files starts. A progress callback with ``steps`` and
    ``tick`` methods (the CLI's) draws a progress bar; others get messages instead."""
    steps = getattr(progress, "steps", None)
    if steps is not None:
        steps(total)


def _tick(progress: Progress) -> None:
    """One file of the current step done."""
    tick = getattr(progress, "tick", None)
    if tick is not None:
        tick()


LIBRARY_DIRS = ("PIONEER", ".PIONEER", "_Serato_")
TARGETS = (Format.REKORDBOX_USB, Format.SERATO)


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
    moved: dict[str, str] = field(default_factory=dict)  # audio, old -> new, relative to the drive
    notes: list[str] = field(default_factory=list)  # changes worth knowing about (cue_changes)
    warnings: list[str] = field(default_factory=list)
    # The new library's fingerprint (cratemover.fingerprint, schema fingerprint_schema)
    fingerprint: str = ""
    fingerprint_schema: int = FINGERPRINT_SCHEMA

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


# --- backup and restore -------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_hashed(source: Path, target: Path) -> str:
    """Copy a file and return its SHA-256, reading the source only once (sticks are slow)."""
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(target, "wb") as dst:
        for block in iter(lambda: src.read(1 << 20), b""):
            digest.update(block)
            dst.write(block)
    shutil.copystat(source, target)
    return digest.hexdigest()


def _ignored(rel: str) -> bool:
    return rel.startswith((".Trash", "System Volume Information"))


def tree_hash(root: Path, checksums: dict[str, str]) -> str:
    """One hash for the drive's whole file tree, like a Git tree: every folder and file name,
    plus the content checksum of every file in ``checksums``. Files outside ``checksums``
    count by name and size only, so a big drive isn't read in full."""
    folders, files = [], {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).relative_to(root)
        for name in dirnames:
            rel = str(here / name)
            if not _ignored(rel):
                folders.append(rel)
        for name in filenames:
            rel = str(here / name)
            if not _ignored(rel):
                files[rel] = checksums.get(rel) or f"size:{(root / rel).stat().st_size}"
    listing = {"folders": sorted(folders), "files": dict(sorted(files.items()))}
    return hashlib.sha256(json.dumps(listing).encode()).hexdigest()


def backup_drive(
    root: Path, backup_root: Path, full: bool, audio: list[Path], progress: Progress
) -> Path:
    """Copy, byte for byte, everything a conversion could touch: the library folders and the
    library's audio files on the drive (``full``: every file on the drive). Each file's
    checksum is recorded, and checked once copied, so a restore can prove it's exact."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_root / f"{root.name or 'drive'}-{stamp}"
    if full:
        files = sorted(f for f in _files(root) if not _ignored(f))
    else:
        files = sorted(
            {f for f in _files(root) if f.split("/", 1)[0] in LIBRARY_DIRS}
            | {str(p.relative_to(root)) for p in audio}
        )
    need = sum((root / f).stat().st_size for f in files)
    backup_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(backup_root).free
    if need > free:
        raise OSError(
            f"the backup needs {need / 1e9:.1f} GB but {backup_root} has {free / 1e9:.1f} GB free"
        )
    dest.mkdir()
    progress(f"Backing up {len(files)} file(s) ({need / 1e6:.0f} MB)")
    _steps(progress, len(files))
    checksums = {}
    for n, rel in enumerate(files, 1):
        _tick(progress)
        if n % 100 == 0 and not hasattr(progress, "tick"):
            progress(f"Backing up {n}/{len(files)}")
        target = dest / "files" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        checksums[rel] = _copy_hashed(root / rel, target)
        if _sha256(target) != checksums[rel]:  # re-read from the computer's disk: quick
            raise OSError(f"the backup copy of {rel} doesn't match the original")
    manifest = {
        "drive": str(root),
        "label": root.name,
        "created": stamp,
        "full": full,
        "library_dirs": [d for d in LIBRARY_DIRS if (root / d).is_dir()],
        "files": checksums,
        "tree": tree_hash(root, checksums),
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


def restore_drive(
    backup: Path, root: Path, progress: Progress = print, check_idle: bool = True
) -> list[str]:
    """Put back every file in the backup byte for byte, remove what the conversion added, and
    check every restored file against its recorded checksum."""
    if check_idle:  # a rollback mid-conversion goes ahead regardless: a half-done drive is worse
        ensure_idle(root)
    manifest = json.loads((backup / "manifest.json").read_text())
    done = []
    files = backup / "files"
    checksums: dict[str, str] = manifest.get("files", {})
    for name in LIBRARY_DIRS:  # library folders: exactly as backed up, or gone if they weren't
        if (root / name).is_dir():
            shutil.rmtree(root / name)
            if not (files / name).is_dir():
                done.append(f"removed {name}")
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
    progress(f"Restoring {len(checksums)} file(s)")
    _steps(progress, len(checksums))
    copied: list[str] = []
    for rel, digest in checksums.items():
        _tick(progress)
        target = root / rel
        same_size = target.is_file() and target.stat().st_size == (files / rel).stat().st_size
        if same_size and _sha256(target) == digest:
            continue  # already as backed up: leave it (fewer writes to the stick)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files / rel, target)
        copied.append(rel)
    progress(f"Copied back {len(copied)} changed file(s); checking them")
    wrong = [rel for rel in copied if _sha256(root / rel) != checksums[rel]]
    if wrong:
        raise OSError(
            f"{len(wrong)} restored file(s) don't match the backup: {', '.join(wrong[:5])}"
        )
    if "tree" in manifest and tree_hash(root, checksums) != manifest["tree"]:
        raise OSError(
            "the restored files match, but the drive's file tree doesn't: a file or folder "
            "was added or removed since the backup"
        )
    done.append(f"restored {len(checksums)} file(s); the drive's file tree matches the backup")
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
    ensure_idle(root)
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

    notes = cue_changes(library, target)

    # 1. Back up to the computer: the library folder and all its audio, byte for byte.
    audio = sorted(
        {
            local.resolve()
            for track in library.tracks.values()
            if (local := Path(track.extra.get("local", track.location))).is_file()
            and root in local.resolve().parents
        }
    )
    backup_root = Path(options.backup_dir or root.parent / "cratemover-backups")
    backup = backup_drive(root, backup_root, options.full_backup, audio, progress)
    result = DriveConvertResult(backup=str(backup), source_format=source_format, notes=notes)
    before = _files(root)

    ensure_idle(root)  # again: the backup can take a while
    try:
        # 2. Convert: the old library goes first, so there's never a second one on the drive.
        for name in _library_dirs(source_format):
            if (root / name).is_dir():
                shutil.rmtree(root / name)
                result.removed.append(name)
        moves = result.moved = relocate(root, library, target, progress)
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
    except Exception as exc:
        _record_created(root, before, backup)
        progress(f"Converting failed ({exc}): restoring the drive from the backup")
        restore_drive(backup, root, progress, check_idle=False)
        raise RolledBack(f"Converting failed: {exc}", []) from exc
    manifest_path = _record_created(root, before, backup)

    # 3. Verify, and 4. check nothing else is on the drive.
    progress("Checking the drive")
    problems, result.fingerprint = verify_conversion(
        root, library, source_format, target, mp3_decoder=options.mp3_decoder
    )
    checksums = json.loads(manifest_path.read_text())["files"]
    problems += check_drive(root, before, source_format, target, library, moves, checksums)
    if problems:
        progress("The check failed: restoring the drive from the backup")
        restore_drive(backup, root, progress, check_idle=False)
        raise RolledBack("The converted drive didn't pass its checks", problems)
    manifest = json.loads(manifest_path.read_text())
    manifest["converted_to"] = {
        "format": target,
        "fingerprint": result.fingerprint,
        "fingerprint_schema": result.fingerprint_schema,
    }
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return result


STAGING = ".cratemover-moving"  # on the stick, only while files are being moved


def relocate(root: Path, library: Library, target: Format, progress: Progress) -> dict[str, str]:
    """Move the library's audio to where ``target`` would have put it (:mod:`cratemover.layout`)
    and point the library's tracks at the new paths. Returns the moves, old -> new, relative
    to ``root``. Moves go through a staging folder, so files swapping places can't collide."""
    local: dict[str, Path] = {}
    for track in library.tracks.values():
        file = Path(track.extra.get("local") or track.location)
        if file.is_file() and root in file.resolve().parents:
            local[track.id] = file.resolve()
    ours = {str(f.relative_to(root)).lower() for f in local.values()}
    occupied = {f.lower() for f in _files(root) if f.lower() not in ours}
    wanted = layout.target_paths(library, target, local, occupied)
    moves = {
        local[tid]: root / rel for tid, rel in wanted.items() if root / rel != local[tid]
    }  # one entry per file, even when several tracks share it
    if moves:
        name = {Format.REKORDBOX_USB: "Rekordbox", Format.SERATO: "Serato"}[target]
        progress(f"Moving {len(moves)} audio file(s) into {name}'s layout")
        _steps(progress, len(moves))
        staging = root / STAGING
        staging.mkdir()
        staged = []
        for n, (old, new) in enumerate(sorted(moves.items())):
            os.rename(old, staging / str(n))
            staged.append((staging / str(n), new))
        for temp, new in staged:
            _tick(progress)
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(temp, new)
        staging.rmdir()
        for old in moves:  # folders the moves emptied
            with contextlib.suppress(OSError):
                for parent in old.parents:
                    if parent == root:
                        break
                    parent.rmdir()
    for tid, rel in wanted.items():
        track = library.tracks[tid]
        track.location = track.extra["local"] = str(root / rel)
    return {str(o.relative_to(root)): str(n.relative_to(root)) for o, n in moves.items()}


class RolledBack(ValueError):
    """A conversion failed, and the drive was restored from the backup and checked."""

    def __init__(self, reason: str, problems: list[str]) -> None:
        super().__init__(reason + ("\n- " + "\n- ".join(problems) if problems else ""))
        self.reason, self.problems = reason, problems


def ensure_idle(root: Path) -> None:
    """Refuse to change a drive while another program could be writing to it: Rekordbox keeps
    a stick's database open after exporting, and wrote brokendb on a stick whose files were
    replaced (byte-identically) underneath it."""
    users = drive_users(root)
    if users:
        raise ValueError(
            "Close these first, so nothing else writes to the drive while it's changed:\n- "
            + "\n- ".join(users)
        )


def cue_changes(library: Library, target: Format) -> list[str]:
    """Warnings for what a conversion changes about cues and grids: not errors (the timings
    survive, or the target can't hold them), but worth knowing before converting back."""
    notes = []
    if target == Format.SERATO:
        from .serato.library import DUPLICATE_MS

        moved = dropped = 0
        moved_tracks: set[str] = set()
        midbar = 0
        lite_hidden: dict[str, int] = {}  # track id -> hot cues in slots 5-8
        for t in library.tracks.values():
            hot = {c.slot for c in t.cues if c.slot is not None and c.slot < 8}
            spots = [c.position_ms for c in t.cues if c.slot is not None and c.slot < 8]
            memory = sorted(
                (c for c in t.cues if c.slot is None and not c.is_loop), key=lambda c: c.position_ms
            )
            for c in memory:  # as the Serato writer places them: lowest free slot, in time order
                free = [n for n in range(8) if n not in hot]
                if any(abs(p - c.position_ms) < DUPLICATE_MS for p in spots) or not free:
                    dropped += 1
                    continue
                hot.add(free[0])
                spots.append(c.position_ms)
                moved += 1
                moved_tracks.add(t.id)
            if high := sum(1 for n in hot if n >= 4):
                lite_hidden[t.id] = high
            midbar += any(m.beat != 1 for m in t.grid[1:])
        if moved:
            notes.append(
                f"{moved} memory cue(s) on {len(moved_tracks)} track(s) became Serato hot cues "
                "(Serato has no memory cues). Same positions, but converting back gives hot "
                "cues, not memory cues."
            )
        if dropped:
            notes.append(
                f"{dropped} memory cue(s) left out: a hot cue already marks the spot, or all 8 "
                "Serato hot cue slots are used."
            )
        if lite_hidden:
            notes.append(
                f"{sum(lite_hidden.values())} hot cue(s) on {len(lite_hidden)} track(s) are in "
                "slots 5-8. Serato DJ Lite shows only slots 1-4: they're kept in the files, and "
                "Serato DJ Pro shows them."
            )
        slips = [t for t in library.tracks.values() if fingerprint.grid_slip_ms(t.grid) > 1.0]
        if slips:
            worst = max(fingerprint.grid_slip_ms(t.grid) for t in slips)
            notes.append(
                f"{len(slips)} track(s) have uneven beats between grid markers. Serato spaces "
                f"beats evenly between markers, so some move (by up to {worst:.1f} ms)."
            )
        if midbar:
            notes.append(
                f"{midbar} track(s) change tempo mid-bar. Serato's grid can't store where the "
                "bars start after the change, so downbeats there may move."
            )
    elif target == Format.REKORDBOX_USB:
        high = [t for t in library.tracks.values() if any((c.slot or 0) >= 8 for c in t.cues)]
        if high:
            notes.append(
                f"{len(high)} track(s) have hot cues beyond the 8th; Rekordbox has 8 (A-H), "
                "so those are left out."
            )
    return notes


def _record_created(root: Path, before: set[str], backup: Path) -> Path:
    """Note the files the conversion added (moved audio, MP3s made from Ogg) in the backup's
    manifest, so a restore removes them."""
    created = sorted(f for f in _files(root) - before if f.split("/", 1)[0] not in LIBRARY_DIRS)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_files"] = created
    manifest_path.write_text(json.dumps(manifest, indent=1))
    return manifest_path


def _library_dirs(fmt: Format) -> tuple[str, ...]:
    return ("PIONEER", ".PIONEER") if fmt == Format.REKORDBOX_USB else ("_Serato_",)


def verify_conversion(
    root: Path,
    source: Library,
    source_format: Format,
    target: Format,
    mp3_decoder: Mp3Decoder = Mp3Decoder.MAD,
) -> tuple[list[str], str]:
    """Read the new library back and check it against the source, exactly (see
    :mod:`cratemover.fingerprint`). Returns the differences and the new library's fingerprint."""
    read = ReadOptions(
        format=target, path=str(root), serato_root=str(root), mp3_decoder=mp3_decoder
    )
    try:
        new = read_library(read)
    except Exception as exc:
        return [f"the new {target} library can't be read back: {exc}"], ""
    r = fingerprint.rules(source_format, target)
    return fingerprint.check(source, new, root, r), fingerprint.fingerprint(
        fingerprint.canonical(new, root, r)
    )


def check_drive(
    root: Path,
    before: set[str],
    source: Format,
    target: Format,
    library: Library,
    moves: dict[str, str],
    checksums: dict[str, str],
) -> list[str]:
    """Only the old library and audio that was moved may have gone; only the new library, the
    moved audio and any audio converted for the target may have been added. Moved audio must
    be byte for byte what it was, unless the target keeps its data in the audio (Serato)."""
    formats = {Format(lib["format"]) for lib in _libraries(root)}
    problems = [] if formats == {target} else [f"the drive has these libraries: {sorted(formats)}"]
    after = _files(root)
    old_dirs, new_dirs = _library_dirs(source), _library_dirs(target)
    moved_to = set(moves.values())
    for f in sorted(before - after):
        if f.split("/", 1)[0] not in old_dirs and f not in moves:
            problems.append(f"file removed outside the old library: {f}")
    for f in sorted(after & before):
        if f.split("/", 1)[0] in old_dirs:
            problems.append(f"old library file still there: {f}")
    for old, new in sorted(moves.items()):
        if new not in after:
            problems.append(f"moved file missing: {old} -> {new}")
        elif target != Format.SERATO and _sha256(root / new) != checksums.get(old):
            problems.append(f"moved file changed: {old} -> {new}")
    audio = {fingerprint.track_key(t, root) for t in library.tracks.values()}
    for f in sorted(after - before):
        if f.split("/", 1)[0] in new_dirs or f in moved_to:
            continue
        if Path(f).suffix.lower() == ".mp3" and str(Path(f).with_suffix("")).lower() in audio:
            continue  # audio converted to MP3 so the target can play it
        problems.append(f"unexpected file added: {f}")
    for f in sorted(after):
        if ".cratemover-" in f or f.endswith(".part") or f.endswith(".part.mp3"):
            problems.append(f"leftover file: {f}")
    return problems
