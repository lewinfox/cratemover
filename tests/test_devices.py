from __future__ import annotations

import shutil
from pathlib import Path

from conftest import FIXTURES

from cratemover.detect import is_hidden
from cratemover.devices import list_drives
from cratemover.model import Format


def _mounts(tmp_path: Path, lines: list[str]) -> Path:
    f = tmp_path / "mounts"
    f.write_text("\n".join(lines) + "\n")
    return f


def test_lists_mounted_sticks_and_their_libraries(tmp_path: Path) -> None:
    media = tmp_path / "media"
    cdj = media / "me" / "CDJ STICK"
    serato = media / "me" / "SERATO"
    blank = media / "me" / "BLANK"
    for d in (cdj, serato, blank):
        d.mkdir(parents=True)
    shutil.copytree(FIXTURES / "rekordbox/stick-6.8.6/PIONEER", cdj / "PIONEER")
    (serato / "_Serato_").mkdir()
    shutil.copy(FIXTURES / "serato-db/database_v2_test.bin", serato / "_Serato_" / "database V2")
    mounts = _mounts(
        tmp_path,
        [
            "/dev/root / ext4 rw 0 0",
            f"/dev/sda1 {media} ext4 rw 0 0",  # the USB root's own bind mount is not a drive
            f"/dev/sdb1 {str(cdj).replace(' ', chr(92) + '040')} vfat rw 0 0",
            f"/dev/sdc1 {serato} exfat rw 0 0",
            f"/dev/sdd1 {blank} vfat rw 0 0",
            f"proc {media}/proc proc rw 0 0",
        ],
    )
    drives = {d.label: d for d in list_drives([media], mounts)}
    assert set(drives) == {"CDJ STICK", "SERATO", "BLANK"}
    assert drives["CDJ STICK"].libraries == [{"format": Format.REKORDBOX_USB, "path": str(cdj)}]
    assert drives["CDJ STICK"].fstype == "vfat" and not drives["CDJ STICK"].notes
    assert drives["SERATO"].libraries == [
        {"format": Format.SERATO, "path": str(serato / "_Serato_"), "serato_root": str(serato)}
    ]
    assert any("FAT32" in n for n in drives["SERATO"].notes)
    assert drives["BLANK"].libraries == [] and drives["BLANK"].writable


def test_finds_library_folders_without_proc_mounts(tmp_path: Path) -> None:
    volumes = tmp_path / "Volumes"
    stick = volumes / "STICK"
    shutil.copytree(FIXTURES / "rekordbox/stick-6.8.6/PIONEER", stick / "PIONEER")
    (volumes / "Macintosh HD").mkdir()
    drives = list_drives([volumes], tmp_path / "no-such-file")
    assert [d.label for d in drives] == ["STICK"]


def test_ignores_libraries_in_the_bin(tmp_path: Path) -> None:
    stick = tmp_path / "STICK"
    for trash in (".Trash-1000/files", "$RECYCLE.BIN/S-1-5"):
        (stick / trash / "_Serato_").mkdir(parents=True)
        shutil.copy(
            FIXTURES / "serato-db/database_v2_test.bin", stick / trash / "_Serato_" / "database V2"
        )
    assert is_hidden(stick / ".Trash-1000/files/_Serato_", stick)
    assert is_hidden(stick / "$RECYCLE.BIN/S-1-5", stick)
    assert not is_hidden(stick / ".mixxx", stick)
    assert list_drives([tmp_path], tmp_path / "no-such-file") == []
