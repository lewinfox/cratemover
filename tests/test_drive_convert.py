from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_ffmpeg

from cratemover import drive_convert, fingerprint
from cratemover.convert import ReadOptions, WriteOptions, read_library, write_library
from cratemover.devices import _libraries
from cratemover.drive_convert import (
    DriveConvertOptions,
    convert_drive,
    list_backups,
    restore_drive,
    verify_conversion,
)
from cratemover.model import Cue, CueRole, Format, Library, TempoMarker, Track
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
    assert not any("Backed up" in w or ".cratemover-" in w for w in result.warnings)
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
    before = {str(p.relative_to(stick)): p.read_bytes() for p in stick.rglob("*") if p.is_file()}
    result = convert_drive(
        stick, DriveConvertOptions(target=Format.SERATO, backup_dir=str(tmp_path / "backups"))
    )
    assert result.removed == ["PIONEER"]
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO}
    old = str(mp3.relative_to(stick))
    assert result.moved[old] == "Peak Time/Alpha - First Light.mp3"  # its first crate's folder
    moved = stick / result.moved[old]
    assert _stray(stick) == ["rekordbox.xml"]  # written with the stick, not by the conversion
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    assert _hot(serato)["First Light"] == [(0, 250), (1, 4250), (2, 2250)]
    assert read_tags(moved).found
    restore_drive(Path(result.backup), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {Format.REKORDBOX_USB}
    assert not read_tags(mp3).found  # the Serato tags written by the conversion are gone again
    after = {str(p.relative_to(stick)): p.read_bytes() for p in stick.rglob("*") if p.is_file()}
    assert after == before  # every file, audio included, byte for byte
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
@pytest.mark.parametrize(
    ("source", "target"),
    [(Format.SERATO, Format.REKORDBOX_USB), (Format.REKORDBOX_USB, Format.SERATO)],
)
def test_conversion_matches_its_source_exactly(
    library_copy: Path, tmp_path: Path, source: Format, target: Format
) -> None:
    stick = _make_stick(library_copy, tmp_path, source)
    before = read_library(ReadOptions(source, str(stick), serato_root=str(stick)))
    result = convert_drive(
        stick, DriveConvertOptions(target=target, backup_dir=str(tmp_path / "b"), waveforms=False)
    )
    after = read_library(ReadOptions(target, str(stick), serato_root=str(stick)))
    for track in before.tracks.values():  # follow the audio to where the conversion moved it
        rel = str(Path(track.extra.get("local", track.location)).relative_to(stick))
        if rel in result.moved:
            track.location = track.extra["local"] = str(stick / result.moved[rel])
    r = fingerprint.rules(source, target)
    assert fingerprint.check(before, after, stick, r) == []
    assert result.fingerprint == fingerprint.fingerprint(fingerprint.canonical(after, stick, r))
    manifest = json.loads((Path(result.backup) / "manifest.json").read_text())
    assert manifest["converted_to"] == {
        "format": target,
        "fingerprint": result.fingerprint,
        "fingerprint_schema": fingerprint.SCHEMA,
    }


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
    assert verify_conversion(stick, serato, Format.SERATO, Format.REKORDBOX_USB)[0] == []
    first = next(t for t in serato.tracks.values() if t.title == "First Light")
    first.hot_cues[0].position_ms += 1  # 1 ms off, on a file that wasn't converted to MP3
    [problem] = verify_conversion(stick, serato, Format.SERATO, Format.REKORDBOX_USB)[0]
    assert "first light/cues" in problem


def test_fingerprints_carry_the_schema_version() -> None:
    form = fingerprint.canonical(
        Library("empty"), Path("/"), fingerprint.rules(Format.SERATO, Format.MIXXX)
    )
    assert form["schema"] == fingerprint.SCHEMA
    assert fingerprint.fingerprint(form) != fingerprint.fingerprint(form | {"schema": 0})


def test_grids_compare_by_where_the_beats_fall() -> None:
    r = fingerprint.rules(Format.SERATO, Format.REKORDBOX_USB)

    def grid(position: float, beat: int) -> list[dict]:  # type: ignore[type-arg]
        track = Track("1", "/x.mp3", grid=[TempoMarker(position, 123.0, beat)])
        return fingerprint._track(track, r, [], 0.0)["grid"]

    # Serato starts this grid on a downbeat at 558.8 ms; Rekordbox one beat earlier, as beat 4.
    assert grid(558.8, 1) == grid(71.0, 4)
    assert grid(558.8, 1) != grid(71.0, 1)  # same beats, but the downbeats moved: a real change
    assert grid(558.8, 1) != grid(560.8, 1)  # 2 ms off


@needs_ffmpeg
def test_restore_notices_a_tree_that_changed(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    result = convert_drive(
        stick,
        DriveConvertOptions(
            target=Format.REKORDBOX_USB, backup_dir=str(tmp_path / "b"), waveforms=False
        ),
    )
    (stick / "stray.txt").write_text("added after the backup, outside the library")
    with pytest.raises(OSError, match="file tree doesn't"):
        restore_drive(Path(result.backup), stick)


@needs_ffmpeg
def test_a_track_in_several_crates_is_stored_once(library_copy: Path, tmp_path: Path) -> None:
    # Departure from Serato, which copies it into each crate's folder and lists every copy in
    # every crate (see cratemover.layout).
    stick = _make_stick(library_copy, tmp_path, Format.REKORDBOX_USB)
    convert_drive(stick, DriveConvertOptions(target=Format.SERATO, backup_dir=str(tmp_path / "b")))
    assert len(list(stick.rglob("*First Light.mp3"))) == 1
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    first = next(t.id for t in serato.tracks.values() if t.title == "First Light")
    crates = [p for _, p in serato.playlists.walk() if p.track_ids and first in p.track_ids]
    assert len(crates) >= 2
    assert all(p.track_ids.count(first) == 1 for p in crates)


def test_notes_say_memory_cues_become_hot_cues_in_serato() -> None:
    lib = Library("test")
    lib.add_track(
        Track("1", "/x.mp3", cues=[Cue(CueRole.CUE, 1000.0), Cue(CueRole.CUE, 5000.0, slot=0)])
    )
    [note] = drive_convert.cue_changes(lib, Format.SERATO)
    assert note.startswith("1 memory cue(s) on 1 track(s) became Serato hot cues")
    assert drive_convert.cue_changes(lib, Format.REKORDBOX_USB) == []


def test_refuses_a_drive_another_program_has_open(tmp_path: Path) -> None:
    import subprocess
    import sys
    import time

    stick = tmp_path / "STICK"
    (stick / "_Serato_").mkdir(parents=True)
    held = stick / "_Serato_" / "database V2"
    held.write_bytes(b"")
    holder = subprocess.Popen(
        [sys.executable, "-c", f"f = open({str(held)!r}); import time; time.sleep(30)"]
    )
    try:
        time.sleep(0.5)
        with pytest.raises(ValueError, match=r"Close these first(.|\n)*database V2 open"):
            convert_drive(stick, DriveConvertOptions(target=Format.REKORDBOX_USB))
    finally:
        holder.kill()
