"""Find removable drives (USB sticks) and what DJ libraries are on them.

Mounts are read from ``/proc/self/mounts``: with the USB folder bind-mounted
``rslave`` into the container, sticks mounted on the host after startup show up
there, and disappear when unmounted. Where ``/proc`` doesn't list them (macOS
hosts, where Docker Desktop shares ``/Volumes`` as a folder), folders under the
USB roots that hold a Rekordbox or Serato library count as drives too.

Run natively on macOS or Windows (no ``/proc``), drives come from psutil:
everything mounted under ``/Volumes`` on a Mac, removable drive letters on Windows.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .detect import is_hidden
from .model import Format

DEFAULT_ROOTS = () if sys.platform == "win32" else ("/media", "/run/media", "/mnt", "/Volumes")
# Filesystems that aren't drives even when mounted under a USB root.
_VIRTUAL = {"proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "overlay", "autofs",
            "binfmt_misc", "debugfs", "tracefs", "securityfs", "pstore", "mqueue", "fusectl"}  # fmt: skip
# Players up to the CDJ-2000NXS2 only read FAT32 (and MBR partition tables).
_OLD_PLAYER_OK = {"vfat", "fat", "fat32", "msdos"}


@dataclass
class Drive:
    path: str
    label: str
    fstype: str = ""
    device: str = ""
    total_bytes: int = 0
    free_bytes: int = 0
    writable: bool = False
    libraries: list[dict[str, str]] = field(
        default_factory=list
    )  # {"format", "path", "serato_root"}
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def usb_roots() -> list[Path]:
    value = os.environ.get("USB_ROOTS")
    return [Path(p) for p in (value.split(os.pathsep) if value else DEFAULT_ROOTS) if p]


def _unescape(field_: str) -> str:
    """/proc/mounts escapes spaces and other characters as octal (``\\040``)."""
    out, i = [], 0
    while i < len(field_):
        if field_[i] == "\\" and i + 3 < len(field_) and field_[i + 1 : i + 4].isdigit():
            out.append(chr(int(field_[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field_[i])
            i += 1
    return "".join(out)


def _mounts(mounts_file: Path) -> list[tuple[str, Path, str]]:
    try:
        lines = mounts_file.read_text().splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 3:
            result.append((_unescape(parts[0]), Path(_unescape(parts[1])), parts[2]))
    return result


def _is_file(path: Path) -> bool:
    # is_file() raises on folders we may not enter (e.g. /media/<another user>).
    try:
        return path.is_file()
    except OSError:
        return False


def _libraries(path: Path) -> list[dict[str, str]]:
    found = []
    for folder in ("PIONEER", ".PIONEER"):
        rekordbox = path / folder / "rekordbox"
        if _is_file(rekordbox / "export.pdb") or _is_file(rekordbox / "exportLibrary.db"):
            found.append({"format": Format.REKORDBOX_USB, "path": str(path)})
            break
    if _is_file(path / "_Serato_" / "database V2"):
        found.append(
            {"format": Format.SERATO, "path": str(path / "_Serato_"), "serato_root": str(path)}
        )
    return found


def _drive(path: Path, fstype: str = "", device: str = "") -> Drive:
    fstype = fstype.lower()
    drive = Drive(path=str(path), label=path.name or path.anchor, fstype=fstype, device=device)
    try:
        usage = shutil.disk_usage(path)
        drive.total_bytes, drive.free_bytes = usage.total, usage.free
    except OSError:
        pass
    drive.writable = os.access(path, os.W_OK)
    drive.libraries = _libraries(path)
    if not drive.writable:
        drive.notes.append(
            "Not writable here: check USB_MOUNT_MODE and that the container runs as your user."
        )
    if fstype in ("ntfs", "ntfs3"):
        drive.notes.append(
            "NTFS: rekordbox won't write to it, macOS only reads it, and no player reads it. Use exFAT (laptop only) or FAT32."
        )
    elif fstype == "fuseblk":
        drive.notes.append(
            "A FUSE filesystem (often exFAT or NTFS): if it's NTFS, reformat it; exFAT is fine on a laptop, but CDJ-2000NXS2 and older players only read FAT32."
        )
    elif fstype == "exfat":
        drive.notes.append(
            "exFAT: fine on a laptop (DDJ-400/FLX4) and the CDJ-3000, but CDJ-2000NXS2 and older players only read FAT32."
        )
    elif fstype and fstype not in _OLD_PLAYER_OK and fstype not in ("tmpfs", ""):
        drive.notes.append(
            f"Formatted {fstype}: Windows and macOS laptops may not read it, and players only read FAT32 (the CDJ-3000 also reads exFAT)."
        )
    return drive


def _native_drives() -> list[Drive]:
    """Drives on a macOS or Windows host, where there's no /proc to read."""
    try:
        import psutil
    except ImportError:
        return []
    drives = []
    for part in psutil.disk_partitions(all=False):
        if sys.platform == "win32":
            if "removable" not in part.opts:
                continue
        elif not part.mountpoint.startswith("/Volumes/"):
            continue
        path = Path(part.mountpoint)
        if path.is_dir():
            drives.append(_drive(path, part.fstype, part.device))
    return drives


def list_drives(
    roots: list[Path] | None = None, mounts_file: Path = Path("/proc/self/mounts")
) -> list[Drive]:
    roots = [r.resolve() for r in (roots if roots is not None else usb_roots()) if r.exists()]
    drives: dict[str, Drive] = {}
    if sys.platform in ("darwin", "win32") and not mounts_file.exists():
        drives = {d.path: d for d in _native_drives()}
    for device, mount_point, fstype in _mounts(mounts_file):
        if fstype in _VIRTUAL:
            continue
        # A drive is a mount *below* a USB root, not the root's own bind mount.
        if any(root in mount_point.parents for root in roots) and mount_point.is_dir():
            drives[str(mount_point)] = _drive(mount_point, fstype, device)
    # Folders holding a library, for hosts where /proc doesn't list the mounts.
    for root in roots:
        for depth in ("*", "*/*"):
            try:
                candidates = sorted(root.glob(depth))
            except OSError:
                continue
            for candidate in candidates:
                if str(candidate) in drives or not candidate.is_dir() or is_hidden(candidate, root):
                    continue
                if any(str(candidate).startswith(d + "/") for d in drives):
                    continue
                if _libraries(candidate):
                    drives[str(candidate)] = _drive(candidate)
    return sorted(drives.values(), key=lambda d: d.path)
