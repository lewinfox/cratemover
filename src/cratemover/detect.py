"""Work out which kind of DJ library a file or folder holds."""

from __future__ import annotations

import sys
from pathlib import Path

REMOVABLE = ("/media/", "/mnt/", "/Volumes/", "/run/media/")


def serato_root(drive: Path) -> str:
    """What a Serato library in ``drive`` stores its paths relative to: the drive it's on."""
    if sys.platform == "win32":
        return drive.anchor  # C:\ or E:\
    return str(drive) if str(drive).startswith(REMOVABLE) else "/"


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def is_rekordbox_xml(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"DJ_PLAYLISTS" in f.read(2048)
    except OSError:
        return False


def _serato(folder: Path) -> dict[str, str]:
    # A _Serato_ at a drive's root stores paths relative to that drive.
    drive = folder.parent if folder.name == "_Serato_" else folder
    return {"format": "serato", "path": str(folder), "serato_root": serato_root(drive)}


def _stick(root: Path) -> dict[str, str]:
    return {"format": "rekordbox_usb", "path": str(root)}


def detect_libraries(path: Path) -> list[dict[str, str]]:
    """Every library at ``path`` (a library file, or the folder holding one).

    A USB stick can hold both a Rekordbox and a Serato library.
    """
    if _is_file(path):
        name = path.name
        if name == "mixxxdb.sqlite":
            return [{"format": "mixxx", "path": str(path)}]
        if name == "master.db":
            return [{"format": "rekordbox_db", "path": str(path)}]
        if name in ("export.pdb", "exportLibrary.db") and len(path.parents) >= 3:
            return [_stick(path.parents[2])]
        if name == "database V2":
            return [_serato(path.parent)]
        if name.lower().endswith(".xml") and is_rekordbox_xml(path):
            return [{"format": "rekordbox_xml", "path": str(path)}]
        return []
    found: list[dict[str, str]] = []
    for folder in ("PIONEER", ".PIONEER"):
        rekordbox = path / folder / "rekordbox"
        if _is_file(rekordbox / "export.pdb") or _is_file(rekordbox / "exportLibrary.db"):
            found.append(_stick(path))
            break
    for serato in (path / "_Serato_", path):
        if _is_file(serato / "database V2"):
            found.append(_serato(serato))
            break
    for db in (path / "mixxxdb.sqlite", path / ".mixxx" / "mixxxdb.sqlite"):
        if _is_file(db):
            found.append({"format": "mixxx", "path": str(db)})
            break
    for db in (path / "master.db", path / "rekordbox" / "master.db"):
        if _is_file(db):
            found.append({"format": "rekordbox_db", "path": str(db)})
            break
    if not found:
        try:
            xmls = [p for p in sorted(path.glob("*.xml")) if is_rekordbox_xml(p)]
        except OSError:
            xmls = []
        found.extend({"format": "rekordbox_xml", "path": str(p)} for p in xmls[:10])
    return found
