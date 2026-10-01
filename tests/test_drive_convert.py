from __future__ import annotations

from pathlib import Path

from conftest import needs_ffmpeg

from cratemover.convert import ReadOptions, WriteOptions, read_library, write_library
from cratemover.devices import _libraries
from cratemover.drive_convert import DriveConvertOptions, convert_drive, list_backups, restore_drive
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


@needs_ffmpeg
def test_serato_stick_to_rekordbox_keeps_both(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    serato = read_library(ReadOptions(Format.SERATO, str(stick), serato_root=str(stick)))
    result = convert_drive(
        stick,
        DriveConvertOptions(
            targets=[Format.REKORDBOX_USB], backup_dir=str(tmp_path / "backups"), waveforms=False
        ),
    )
    assert result.source_format == Format.SERATO
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO, Format.REKORDBOX_USB}
    rekordbox = read_library(ReadOptions(Format.REKORDBOX_USB, str(stick)))
    expected = {
        k: v for k, v in _hot(serato).items() if k not in ("Vorbis", "Gone")
    }  # Gone: no audio
    assert _hot(rekordbox) == expected | {
        "Vorbis": [(s, p + 26) for s, p in _hot(serato)["Vorbis"]]  # converted to MP3 on the stick
    }
    # Undo: the Rekordbox library disappears again.
    [backup] = list_backups(tmp_path / "backups")
    done = restore_drive(Path(backup["path"]), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO}
    # The MP3 made from the Ogg file, and rekordbox.xml.
    assert "removed 2 file(s) the conversion had added" in done
    assert not (stick / "rekordbox.xml").exists()
    assert not list(stick.rglob("*Vorbis.mp3"))


@needs_ffmpeg
def test_rekordbox_stick_to_serato_and_restore_tags(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.REKORDBOX_USB)
    mp3 = next((stick / "Contents").rglob("*First Light.mp3"))
    assert not read_tags(mp3).found
    result = convert_drive(
        stick,
        DriveConvertOptions(
            targets=[Format.SERATO], backup_dir=str(tmp_path / "backups"), keep_source=False
        ),
    )
    assert result.removed == ["PIONEER"]
    assert {lib["format"] for lib in _libraries(stick)} == {Format.SERATO}
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
def test_stick_gets_a_rekordbox_xml(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, Format.SERATO)
    convert_drive(
        stick,
        DriveConvertOptions(
            targets=[Format.REKORDBOX_USB],
            backup_dir=str(tmp_path / "b"),
            waveforms=False,
            xml_root="E:/",
        ),
    )
    xml = read_library(ReadOptions(Format.REKORDBOX_XML, str(stick / "rekordbox.xml")))
    locations = sorted(t.location for t in xml.tracks.values())
    assert locations and all(loc.startswith("E:/") for loc in locations)
    assert "E:/Contents/Alpha/Fixtures/Alpha - First Light.mp3" in locations or any(
        loc.endswith("First Light.mp3") for loc in locations
    )
    first = next(t for t in xml.tracks.values() if t.title == "First Light")
    assert [(c.slot, round(c.position_ms)) for c in first.hot_cues] == [
        (0, 250),
        (1, 4250),
        (2, 2250),
    ]
