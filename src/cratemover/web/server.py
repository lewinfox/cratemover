"""FastAPI app: pick a source library, look at it, convert it, download the result.

Environment:

* ``EXPORT_DIR`` (default ``./export``): conversions are written under here.
* ``BROWSE_ROOTS`` (default ``/sources:<EXPORT_DIR>:/media:/mnt:/Volumes:$HOME``): folders
  the file picker may show and libraries may be written in, separated by ``:``.
* ``UPLOAD_DIR`` (default a temp dir): where uploaded libraries are unpacked.
* ``BACKUP_DIR`` (default ``<EXPORT_DIR>/backups``): where drive conversions back drives up.
* ``USB_ROOTS`` (default ``/media:/run/media:/mnt:/Volumes``): where removable drives
  are mounted; drives below them are listed live by ``/api/drives``.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
import traceback
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..convert import (
    FORMATS,
    SOURCE_FORMATS,
    SYNC_FORMATS,
    TARGET_FORMATS,
    ReadOptions,
    SyncSide,
    WriteOptions,
    read_library,
    sync_libraries,
    write_library,
)
from ..detect import REMOVABLE, detect_libraries, is_rekordbox_xml
from ..devices import list_drives, usb_roots
from ..drive_convert import DriveConvertOptions, convert_drive, list_backups, restore_drive
from ..keys import KeyNotation, format_key
from ..model import Library, Playlist, Track
from ..paths import make_resolver, parse_rules
from ..sync import CuePolicy, PlaylistPolicy, Prefer, SyncOptions

EXPORT_DIR = Path(os.environ.get("EXPORT_DIR", "export")).resolve()
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or tempfile.mkdtemp(prefix="cratemover-uploads-"))
BROWSE_ROOTS = [
    *usb_roots(),
    *(
        Path(p)
        for p in os.environ.get(
            "BROWSE_ROOTS", f"/sources:{EXPORT_DIR}:/media:/mnt:/Volumes:/run/media:{Path.home()}"
        ).split(":")
        if p
    ),
]
BROWSE_ROOTS = list(dict.fromkeys(BROWSE_ROOTS))

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or EXPORT_DIR / "backups").resolve()

app = FastAPI(title="Cratemover", version=__version__)


# --- background jobs --------------------------------------------------------------------


@dataclass
class Job:
    id: str
    kind: str
    status: str = "running"  # running | done | error
    message: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)


_jobs: dict[str, Job] = {}
_libraries: dict[str, tuple[ReadOptions, Library]] = {}  # job id -> loaded library
_lock = threading.Lock()


def _start(kind: str, work: Any) -> Job:
    job = Job(uuid.uuid4().hex[:12], kind)
    with _lock:
        _jobs[job.id] = job

    def progress(message: str) -> None:
        job.message = message
        job.log.append(message)

    def run() -> None:
        try:
            job.result = work(job, progress)
            job.status = "done"
        except Exception as exc:
            job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
            job.log.append(traceback.format_exc())

    threading.Thread(target=run, daemon=True).start()
    return job


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return asdict(job) | {"log": job.log[-20:]}


# --- info and file browsing ----------------------------------------------------------------


@app.get("/api/info")
def info() -> dict[str, Any]:
    return {
        "version": __version__,
        "formats": FORMATS,
        "source_formats": SOURCE_FORMATS,
        "target_formats": TARGET_FORMATS,
        "sync_formats": SYNC_FORMATS,
        "key_notations": [k.value for k in KeyNotation],
        "export_dir": str(EXPORT_DIR),
        "browse_roots": [str(p) for p in BROWSE_ROOTS if p.exists()],
        "suggestions": _suggest_sources(),
    }


def _is_removable(path: Path) -> bool:
    return str(path).startswith(REMOVABLE)


def _suggest_sources() -> list[dict[str, str]]:
    """Libraries and sticks found in the usual mount points and home folder."""
    found: list[dict[str, str]] = []
    patterns = (
        ("mixxxdb.sqlite", "mixxx"),
        ("*/mixxxdb.sqlite", "mixxx"),
        (".mixxx/mixxxdb.sqlite", "mixxx"),
        ("PIONEER/rekordbox/export.pdb", "rekordbox_usb"),
        ("*/PIONEER/rekordbox/export.pdb", "rekordbox_usb"),
        ("*/*/PIONEER/rekordbox/export.pdb", "rekordbox_usb"),
        ("_Serato_/database V2", "serato"),
        ("*/_Serato_/database V2", "serato"),
        ("*/*/_Serato_/database V2", "serato"),
        ("database V2", "serato"),
        ("*/database V2", "serato"),
        ("*.xml", "rekordbox_xml"),
        ("*/*.xml", "rekordbox_xml"),
        ("master.db", "rekordbox_db"),
        ("*/master.db", "rekordbox_db"),
    )
    for root in BROWSE_ROOTS:
        if not root.is_dir():
            continue
        for pattern, fmt in patterns:
            try:
                for match in sorted(root.glob(pattern))[:10]:
                    entry = {"format": fmt, "path": str(match)}
                    if fmt == "rekordbox_usb":
                        entry["path"] = str(match.parents[2])
                    elif fmt == "serato":
                        folder = match.parent
                        entry["path"] = str(folder)
                        # A _Serato_ at a drive's root stores paths relative to that drive.
                        drive = folder.parent if folder.name == "_Serato_" else folder
                        entry["serato_root"] = str(drive) if _is_removable(drive) else "/"
                    elif fmt == "rekordbox_xml" and not is_rekordbox_xml(match):
                        continue
                    if entry not in found:
                        found.append(entry)
            except OSError:
                continue
    return found


@app.get("/api/sources")
def sources() -> dict[str, Any]:
    """Libraries found in the mounted folders and on drives. The UI lists these to pick from."""
    return {"sources": _suggest_sources()}


@app.get("/api/drives")
def drives() -> dict[str, Any]:
    """Removable drives mounted now, and the libraries on them. The UI polls this."""
    return {"drives": [d.as_dict() for d in list_drives()], "roots": [str(r) for r in usb_roots()]}


class DriveConvertRequest(BaseModel):
    path: str
    targets: list[str]
    source_format: str = ""
    full_backup: bool = False
    keep_source: bool = True
    serato_write_tags: bool = True
    waveforms: bool = True
    onelibrary: str = "auto"
    mp3_decoder: str = "MAD"
    xml_root: str = ""


def _drive_path(path: str) -> Path:
    drive = Path(path)
    if not drive.is_dir() or not any(r.resolve() in drive.resolve().parents for r in usb_roots()):
        raise HTTPException(403, "not a drive under the USB folder")
    return drive


@app.post("/api/drives/convert")
def drive_convert(request: DriveConvertRequest) -> dict[str, str]:
    """Back a drive up and convert its library to the target format(s), in place."""
    drive = _drive_path(request.path)
    options = DriveConvertOptions(
        targets=request.targets,
        source_format=request.source_format,
        backup_dir=str(BACKUP_DIR),
        full_backup=request.full_backup,
        keep_source=request.keep_source,
        serato_write_tags=request.serato_write_tags,
        waveforms=request.waveforms,
        onelibrary=request.onelibrary,
        mp3_decoder=request.mp3_decoder,
        xml_root=request.xml_root,
    )

    def work(job: Job, progress: Any) -> dict[str, Any]:
        return convert_drive(drive, options, progress).as_dict()

    return {"job_id": _start("drive-convert", work).id}


@app.get("/api/backups")
def backups() -> dict[str, Any]:
    return {"backups": list_backups(BACKUP_DIR), "dir": str(BACKUP_DIR)}


class RestoreRequest(BaseModel):
    backup: str
    path: str


@app.post("/api/backups/restore")
def restore(request: RestoreRequest) -> dict[str, str]:
    drive = _drive_path(request.path)
    backup = Path(request.backup).resolve()
    if BACKUP_DIR not in backup.parents or not (backup / "manifest.json").is_file():
        raise HTTPException(404, "no such backup")

    def work(job: Job, progress: Any) -> dict[str, Any]:
        return {"done": restore_drive(backup, drive, progress)}

    return {"job_id": _start("restore", work).id}


def _allowed(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        resolved == r.resolve() or r.resolve() in resolved.parents for r in BROWSE_ROOTS
    ) or (UPLOAD_DIR.resolve() in resolved.parents)


@app.get("/api/browse")
def browse(path: str = "") -> dict[str, Any]:
    if not path:
        return {
            "path": "",
            "parent": None,
            "entries": [
                {"name": str(r), "path": str(r), "dir": True} for r in BROWSE_ROOTS if r.exists()
            ],
        }
    target = Path(path)
    if not _allowed(target) or not target.is_dir():
        raise HTTPException(403, "folder not available")
    entries = []
    try:
        for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if child.name.startswith(".") and child.name not in (".mixxx",):
                continue
            entries.append({"name": child.name, "path": str(child), "dir": child.is_dir()})
    except PermissionError as exc:
        raise HTTPException(403, "permission denied") from exc
    parent = (
        str(target.parent)
        if any(r.resolve() in target.resolve().parents for r in BROWSE_ROOTS)
        else ""
    )
    return {"path": str(target), "parent": parent, "entries": entries[:2000]}


@app.post("/api/upload")
async def upload(file: UploadFile) -> dict[str, str]:
    """Upload a rekordbox.xml, a mixxxdb.sqlite, or a zip of a _Serato_ folder."""
    dest = UPLOAD_DIR / uuid.uuid4().hex[:12]
    dest.mkdir(parents=True)
    name = Path(file.filename or "upload").name
    data = await file.read()
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.namelist():
                target = (dest / member).resolve()
                if dest.resolve() not in target.parents:
                    raise HTTPException(400, "unsafe path in zip")
            archive.extractall(dest)
        serato = next(dest.rglob("database V2"), None)
        return {"path": str(serato.parent if serato else dest)}
    (dest / name).write_bytes(data)
    return {"path": str(dest / name)}


# --- inspect and convert ------------------------------------------------------------------


class ReadRequest(BaseModel):
    format: str = ""  # empty: work it out from what's at the path
    path: str
    access_rules: str = ""
    mp3_decoder: str = "MAD"
    serato_root: str = "/"
    read_file_tags: bool = True


class WriteRequest(BaseModel):
    library_id: str = ""
    format: str = ""
    output_name: str = "converted"
    path_rules: str = ""
    key_notation: str = ""
    mp3_decoder: str = "MAD"
    memory_cues_to_hot_cues: bool = True
    rekordbox_memory_cues: bool = True
    rekordbox_hot_cues_as_memory: bool = False
    serato_root: str = "/"
    serato_write_tags: bool = False
    serato_max_hot_cues: int = 8
    serato_base_database: str = ""
    mixxx_base_database: str = ""
    mixxx_playlists_as_crates: bool = False
    playlists: list[str] = []
    # Write into an existing library or onto a stick instead of the export folder.
    in_place: bool = False
    target_path: str = ""
    copy_missing: bool = True
    waveforms: bool = True
    device_name: str = ""
    onelibrary: str = "auto"
    usb_xml: bool = True
    usb_xml_root: str = ""


class SyncRequest(BaseModel):
    a: ReadRequest
    b: ReadRequest
    direction: str = "both"  # a_to_b | b_to_a | both
    prefer: str = "incoming"  # the side changes come *from* wins; for "both", A wins
    cues: str = "merge"
    grids: str = "fill"
    metadata: str = "fill"
    playlists: str = "merge"
    add_tracks: bool = True
    path_rules: str = ""  # A paths => B paths
    only_playlists: list[str] = []  # send only these of A's playlists to B; empty: all
    dry_run: bool = True
    write: WriteRequest | None = None  # format options for the written side(s)


def _tree(node: Playlist, parents: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    out = []
    for child in node.children:
        path = " / ".join((*parents, child.name))
        if child.is_folder:
            out.append(
                {"name": child.name, "path": path, "children": _tree(child, (*parents, child.name))}
            )
        else:
            out.append(
                {
                    "name": child.name,
                    "path": path,
                    "count": len(child.track_ids or []),
                    "crate": child.is_crate,
                }
            )
    return out


def _common_folders(tracks: list[Track], limit: int = 6) -> list[dict[str, Any]]:
    """The most common top folders, to suggest path rewrite rules."""
    counts: Counter[str] = Counter()
    for track in tracks:
        parts = track.location.replace("\\", "/").split("/")
        depth = 4 if parts and parts[0] == "" else 3
        counts["/".join(parts[:depth])] += 1
    return [{"folder": f, "tracks": n} for f, n in counts.most_common(limit)]


def _track_row(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "artist": track.artist,
        "title": track.title or track.filename,
        "bpm": round(track.bpm, 2),
        "location": track.location,
        "hot_cues": sum(1 for c in track.cues if c.slot is not None),
        "memory_cues": sum(1 for c in track.cues if c.slot is None),
        "grid": len(track.grid),
    }


@app.post("/api/inspect")
def inspect(request: ReadRequest) -> dict[str, str]:
    options = _read_options(request)

    def work(job: Job, progress: Any) -> dict[str, Any]:
        progress("Reading library")
        library = read_library(options, progress)
        _libraries[job.id] = (options, library)
        progress("Checking files")
        resolve = make_resolver(options.access_rules)
        tracks = list(library.tracks.values())
        missing = [t for t in tracks if resolve(t.location) is None]
        return {
            "library_id": job.id,
            "source": library.source,
            "summary": library.summary() | {"missing_files": len(missing)},
            "playlists": _tree(library.playlists),
            "folders": _common_folders(tracks),
            "missing_examples": [t.location for t in missing[:5]],
            "format": options.format,
            "path": options.path,
            "access_rules": [list(rule) for rule in options.access_rules],
            "warnings": library.warnings,
        }

    return {"job_id": _start("inspect", work).id}


@app.get("/api/libraries/{library_id}/tracks")
def tracks(library_id: str, playlist: str = "", q: str = "", limit: int = 200) -> dict[str, Any]:
    if library_id not in _libraries:
        raise HTTPException(404, "library not loaded; load it again")
    library = _libraries[library_id][1]
    if playlist:
        ids = next(
            (
                p.track_ids or []
                for parents, p in library.playlists.walk()
                if " / ".join((*parents, p.name)) == playlist
            ),
            [],
        )
        selected = [library.tracks[i] for i in ids if i in library.tracks]
    else:
        selected = list(library.tracks.values())
    if q:
        needle = q.lower()
        selected = [t for t in selected if needle in f"{t.artist} {t.title} {t.location}".lower()]
    return {"total": len(selected), "tracks": [_track_row(t) for t in selected[:limit]]}


@app.get("/api/libraries/{library_id}/tracks/{track_id}")
def track_detail(library_id: str, track_id: str) -> dict[str, Any]:
    if library_id not in _libraries or track_id not in _libraries[library_id][1].tracks:
        raise HTTPException(404, "not found")
    track = _libraries[library_id][1].tracks[track_id]
    data = asdict(track)
    data["date_added"] = track.date_added.isoformat() if track.date_added else None
    data["key"] = " / ".join(format_key(track.key, n) for n in KeyNotation) if track.key else ""
    return data


def _safe_output(name: str) -> Path:
    cleaned = "".join(c for c in name if c.isalnum() or c in " -_.").strip(" .") or "converted"
    return EXPORT_DIR / cleaned


def _read_options(request: ReadRequest) -> ReadOptions:
    fmt, path, serato_root = request.format, request.path, request.serato_root or "/"
    if not fmt:
        found = detect_libraries(Path(path))
        if not found:
            raise HTTPException(
                400, f"No Mixxx, Rekordbox or Serato library found at {path or '(no path)'}"
            )
        fmt, path = found[0]["format"], found[0]["path"]
        if serato_root == "/":
            serato_root = found[0].get("serato_root", "/")
    if fmt not in FORMATS:
        raise HTTPException(400, f"unknown format {fmt!r}")
    return ReadOptions(
        format=fmt,  # type: ignore[arg-type]
        path=path,
        access_rules=parse_rules(request.access_rules),
        mp3_decoder=request.mp3_decoder,  # type: ignore[arg-type]
        serato_root=serato_root,
        read_file_tags=request.read_file_tags,
        music_roots=_music_roots(),
    )


def _music_roots() -> list[str]:
    """Where the music might be: the mounted folders and any drives plugged in."""
    roots = [str(r) for r in BROWSE_ROOTS if r.is_dir()]
    roots += [d.path for d in list_drives() if d.path not in roots]
    return roots


def _write_options(request: WriteRequest, output: Path) -> WriteOptions:
    return WriteOptions(
        format=request.format,  # type: ignore[arg-type]
        output_dir=str(output),
        path_rules=parse_rules(request.path_rules),
        key_notation=KeyNotation(request.key_notation) if request.key_notation else None,
        mp3_decoder=request.mp3_decoder,  # type: ignore[arg-type]
        memory_cues_to_hot_cues=request.memory_cues_to_hot_cues,
        rekordbox_memory_cues=request.rekordbox_memory_cues,
        rekordbox_hot_cues_as_memory=request.rekordbox_hot_cues_as_memory,
        serato_root=request.serato_root or "/",
        serato_write_tags=request.serato_write_tags,
        serato_max_hot_cues=request.serato_max_hot_cues,
        serato_base_database=request.serato_base_database,
        mixxx_base_database=request.mixxx_base_database,
        mixxx_playlists_as_crates=request.mixxx_playlists_as_crates,
        copy_missing=request.copy_missing,
        waveforms=request.waveforms,
        device_name=request.device_name,
        onelibrary=request.onelibrary,
        usb_xml=request.usb_xml,
        usb_xml_root=request.usb_xml_root,
        in_place=request.in_place,
        playlists=request.playlists,
    )


def _relative_to_export(files: list[str]) -> list[dict[str, Any]]:
    out = []
    for f in files:
        path = Path(f).resolve()
        inside = EXPORT_DIR in path.parents
        out.append(
            {"path": str(path), "download": str(path.relative_to(EXPORT_DIR)) if inside else None}
        )
    return out


@app.post("/api/convert")
def convert(request: WriteRequest) -> dict[str, str]:
    if request.library_id not in _libraries:
        raise HTTPException(404, "library not loaded; load it again")
    if request.format not in TARGET_FORMATS:
        raise HTTPException(400, "unknown format")
    read_options, library = _libraries[request.library_id]
    direct = request.in_place or request.format == "rekordbox_usb"
    if direct:
        out = Path(request.target_path)
        if not request.target_path or not _allowed(out):
            raise HTTPException(403, "choose a library or drive inside the mounted folders")
        if request.format == "rekordbox_usb" and not out.is_dir():
            raise HTTPException(400, f"{out} is not a folder or mounted drive")
    else:
        out = _safe_output(request.output_name)
    options = _write_options(request, out)

    def work(job: Job, progress: Any) -> dict[str, Any]:
        progress("Converting")
        result = write_library(library, options, read_options.access_rules, progress)
        return result.as_dict() | {
            "files": _relative_to_export(result.files),
            "output_dir": str(out),
            "zip": None if direct else out.name,
            "format": request.format,
            "in_place": direct,
        }

    return {"job_id": _start("convert", work).id}


@app.post("/api/sync")
def sync(request: SyncRequest) -> dict[str, str]:
    a, b = _read_options(request.a), _read_options(request.b)
    writes_to = {"a_to_b": [b], "b_to_a": [a], "both": [a, b]}.get(request.direction)
    if writes_to is None:
        raise HTTPException(400, "direction must be a_to_b, b_to_a or both")
    for side in writes_to:
        if side.format not in SYNC_FORMATS:
            raise HTTPException(
                400, f"{FORMATS[side.format]} can't be written; sync it one way only"
            )
        if not _allowed(Path(side.path)):
            raise HTTPException(403, f"{side.path} is outside the mounted folders")
    try:
        options = SyncOptions(
            prefer=Prefer(request.prefer),
            cues=CuePolicy(request.cues),
            grids=CuePolicy(request.grids),
            metadata=CuePolicy(request.metadata),
            playlists=PlaylistPolicy(request.playlists),
            add_tracks=request.add_tracks,
            path_rules=parse_rules(request.path_rules),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    template = request.write or WriteRequest(library_id="", format="mixxx")

    def side(read: ReadOptions) -> SyncSide:
        write = _write_options(template, Path(read.path))
        write.serato_root = read.serato_root
        return SyncSide(read, write)

    def work(job: Job, progress: Any) -> dict[str, Any]:
        result = sync_libraries(
            side(a),
            side(b),
            request.direction,
            options,
            request.dry_run,
            progress,
            request.only_playlists,
        )  # type: ignore[arg-type]
        return result.as_dict() | {"dry_run": request.dry_run}

    return {"job_id": _start("sync", work).id}


@app.get("/api/download")
def download(path: str) -> Response:
    target = (EXPORT_DIR / path).resolve()
    if EXPORT_DIR not in target.parents and target != EXPORT_DIR:
        raise HTTPException(403, "outside the export folder")
    if target.is_file():
        return FileResponse(target, filename=target.name)
    if target.is_dir():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(target.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(target))
        return Response(
            buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{target.name}.zip"'},
        )
    raise HTTPException(404, "not found")


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
