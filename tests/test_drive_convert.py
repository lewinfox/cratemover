from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_ffmpeg

from cratemover import drive_convert
from cratemover.convert import ReadOptions, WriteOptions, read_library, write_library
from cratemover.devices import _libraries
from cratemover.drive_convert import (
    DriveConvertOptions,
    convert_drive,
    list_backups,
    restore_drive,
    verify_conversion,
)
from cratemover.model import Format
from cratemover.serato.tags import read_tags


def _hot(lib) -> dict[str, list[tuple[int | None, int]]]:  # type: ignore[no-untyped-def]
    return {
        t.title: [(c.slot, round(c.position_ms)) for c in t.hot_cues] for t in lib.tracks.values()
    }


def _make_stick(library_copy: Path, tmp_path: Path, fmt: str) -> Path:
    stick = tmp_path / "media" / "STICK"
    stick.mkdir(parents=True)
    source = read_library(ReadOptions(Format.MIXXX, str(library_copy / "mixxx")))
    write_library(
        source,
        WriteOptions(fmt, str(stick), in_place=True, serato_root=str(stick), serato_write_tags=True,
                     waveforms=False),
        [],
    )  # fmt: skip
    return stick


def _stray(stick: Path) -> list[str]:
    """Files a conversion must never leave on a stick."""
    return sorted(
        str(p.relative_to(stick))
        for p in stick.rglob("*")
        if ".cratemover-" in p.name or p.name == "rekordbox.xml"
    )


@needs_ffmpeg
def test_serato_stick_to_rekordbox_replaces_the_library(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    result = convert_drive(
        stick,
        DriveConvertOptions(
            target=Format.REKORDBOX_USB, backup_dir=str(tmp_path / "backups"), waveforms=False
        ),
    )
    assert result.source_format == Format.SERATO
    assert result.removed == ["_Serato_"]
    assert {lib["format"] for lib in _libraries(stick)} == {Format.REKORDBOX_USB}
    assert _stray(stick) == []
    assert not (stick / "PIONEER/rekordbox/exportLibrary.db").exists()  # OneLibrary is opt-in
    rekordbox = read_library(ReadOptions(Format.REKORDBOX_USB, str(stick)))
    expected = {
        k: v for k, v in _hot(serato).items() if k not in ("Vorbis", "Gone")
    }  # Gone: no audio
    assert _hot(rekordbox) == expected | {
        "Vorbis": [(s, p + 26) for s, p in _hot(serato)["Vorbis"]]  # converted to MP3 on the stick
    }
    # Undo: the Serato library comes back and the Rekordbox one, and the MP3 made from the
    # Ogg file, go.
    [backup] = list_backups(tmp_path / "backups")
    done = restore_drive(Path(backup["path"]), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO}
    assert "removed 1 file(s) the conversion had added" in done
    assert not list(stick.rglob("*Vorbis.mp3"))


@needs_ffmpeg
def test_rekordbox_stick_to_serato_and_restore_tags(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.REKORDBOX_USB)
    mp3 = next((stick / "Contents").rglob("*First Light.mp3"))
    assert not read_tags(mp3).found
    result = convert_drive(
        stick, DriveConvertOptions(target=Format.SERATO, backup_dir=str(tmp_path / "backups"))
    )
    assert result.removed == ["PIONEER"]
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO}
    assert _stray(stick) == ["rekordbox.xml"]  # written with the stick, not by the conversion
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    assert _hot(serato)["First Light"] == [(0, 250), (1, 4250), (2, 2250)]
    assert read_tags(mp3).found
    restore_drive(Path(result.backup), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {Format.REKORDBOX_USB}
    assert not read_tags(mp3).found  # the Serato tags written by the conversion are gone again
    assert _hot(read_library(ReadOptions(Format.REKORDBOX_USB, str(stick))))["First Light"] == [
        (0, 250),
        (1, 4250),
        (2, 2250),
    ]


@needs_ffmpeg
def test_refuses_a_stick_with_two_libraries(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    shutil.copytree(FIXTURES / "rekordbox/stick-6.8.6/PIONEER", stick / "PIONEER")
    before = sorted(str(p) for p in stick.rglob("*"))
    with pytest.raises(ValueError, match="both a Rekordbox and a Serato library"):
        convert_drive(
            stick,
            DriveConvertOptions(
                target=Format.REKORDBOX_USB,
                source_format=Format.SERATO,
                backup_dir=str(tmp_path / "backups"),
            ),
        )
    assert sorted(str(p) for p in stick.rglob("*")) == before


@needs_ffmpeg
def test_never_writes_a_second_library_onto_a_stick(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    source = read_library(ReadOptions(Format.MIXXX, str(library_copy / "mixxx")))
    with pytest.raises(ValueError, match="already has a serato library"):
        write_library(source, WriteOptions(Format.REKORDBOX_USB, str(stick), waveforms=False), [])
    assert not (stick / "PIONEER").exists()


@needs_ffmpeg
def test_a_failed_check_puts_the_stick_back(
    library_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    before = {str(p.relative_to(stick)): p.read_bytes() for p in stick.rglob("*") if p.is_file()}
    real_write = drive_convert.write_library

    def write_with_junk(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = real_write(*args, **kwargs)
        (stick / "junk.txt").write_text("not part of any library")
        return result

    monkeypatch.setattr(drive_convert, "write_library", write_with_junk)
    with pytest.raises(ValueError, match=r"unexpected file added: junk\.txt"):
        convert_drive(
            stick,
            DriveConvertOptions(
                target=Format.REKORDBOX_USB, backup_dir=str(tmp_path / "b"), waveforms=False
            ),
        )
    after = {str(p.relative_to(stick)): p.read_bytes() for p in stick.rglob("*") if p.is_file()}
    assert after == before  # Serato library back, Rekordbox library, MP3 and junk gone


@needs_ffmpeg
def test_verify_catches_a_cue_that_drifted(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    convert_drive(
        stick,
        DriveConvertOptions(
            target=Format.REKORDBOX_USB, backup_dir=str(tmp_path / "b"), waveforms=False
        ),
    )
    assert verify_conversion(stick, serato, Format.REKORDBOX_USB) == []
    first = next(t for t in serato.tracks.values() if t.title == "First Light")
    first.hot_cues[0].position_ms += 5  # 5 ms off, on a file that wasn't converted to MP3
    [problem] = verify_conversion(stick, serato, Format.REKORDBOX_USB)
    assert "hot cue 1 moved" in problem
