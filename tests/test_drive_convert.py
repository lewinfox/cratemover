from __future__ import annotations

from pathlib import Path

from conftest import needs_ffmpeg

from djconvert.convert import ReadOptions, WriteOptions, read_library, write_library
from djconvert.devices import _libraries
from djconvert.drive_convert import DriveConvertOptions, convert_drive, list_backups, restore_drive
from djconvert.serato.tags import read_tags


def _hot(lib) -> dict[str, list[tuple[int | None, int]]]:  # type: ignore[no-untyped-def]
    return {
        t.title: [(c.slot, round(c.position_ms)) for c in t.hot_cues] for t in lib.tracks.values()
    }


def _make_stick(library_copy: Path, tmp_path: Path, fmt: str) -> Path:
    stick = tmp_path / "media" / "STICK"
    stick.mkdir(parents=True)
    source = read_library(ReadOptions("mixxx", str(library_copy / "mixxx")))
    write_library(
        source,
        WriteOptions(fmt, str(stick), in_place=True, serato_root=str(stick), serato_write_tags=True,
                     waveforms=False),
        [],
    )  # fmt: skip
    return stick


@needs_ffmpeg
def test_serato_stick_to_rekordbox_keeps_both(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, "serato")
    serato = read_library(ReadOptions("serato", str(stick), serato_root=str(stick)))
    result = convert_drive(
        stick,
        DriveConvertOptions(
            targets=["rekordbox_usb"], backup_dir=str(tmp_path / "backups"), waveforms=False
        ),
    )
    assert result.source_format == "serato"
    assert {lib["format"] for lib in _libraries(stick)} == {"serato", "rekordbox_usb"}
    rekordbox = read_library(ReadOptions("rekordbox_usb", str(stick)))
    expected = {
        k: v for k, v in _hot(serato).items() if k not in ("Vorbis", "Gone")
    }  # Gone: no audio
    assert _hot(rekordbox) == expected | {
        "Vorbis": [(s, p + 26) for s, p in _hot(serato)["Vorbis"]]  # converted to MP3 on the stick
    }
    # Undo: the Rekordbox library disappears again.
    [backup] = list_backups(tmp_path / "backups")
    done = restore_drive(Path(backup["path"]), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {"serato"}
    # The MP3 made from the Ogg file, and rekordbox.xml.
    assert "removed 2 file(s) the conversion had added" in done
    assert not (stick / "rekordbox.xml").exists()
    assert not list(stick.rglob("*Vorbis.mp3"))


@needs_ffmpeg
def test_rekordbox_stick_to_serato_and_restore_tags(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, "rekordbox_usb")
    mp3 = next((stick / "Contents").rglob("*First Light.mp3"))
    assert not read_tags(mp3).found
    result = convert_drive(
        stick,
        DriveConvertOptions(
            targets=["serato"], backup_dir=str(tmp_path / "backups"), keep_source=False
        ),
    )
    assert result.removed == ["PIONEER"]
    assert {lib["format"] for lib in _libraries(stick)} == {"serato"}
    serato = read_library(ReadOptions("serato", str(stick), serato_root=str(stick)))
    assert _hot(serato)["First Light"] == [(0, 250), (1, 4250), (2, 2250)]
    assert read_tags(mp3).found
    restore_drive(Path(result.backup), stick)
    assert {lib["format"] for lib in _libraries(stick)} == {"rekordbox_usb"}
    assert not read_tags(mp3).found  # the Serato tags written by the conversion are gone again
    assert _hot(read_library(ReadOptions("rekordbox_usb", str(stick))))["First Light"] == [
        (0, 250),
        (1, 4250),
        (2, 2250),
    ]


@needs_ffmpeg
def test_stick_gets_a_rekordbox_xml(library_copy: Path, tmp_path: Path) -> None:
    stick = _make_stick(library_copy, tmp_path, "serato")
    convert_drive(
        stick,
        DriveConvertOptions(
            targets=["rekordbox_usb"],
            backup_dir=str(tmp_path / "b"),
            waveforms=False,
            xml_root="E:/",
        ),
    )
    xml = read_library(ReadOptions("rekordbox_xml", str(stick / "rekordbox.xml")))
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
