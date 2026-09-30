from __future__ import annotations

from pathlib import Path

from djconvert.detect import detect_libraries
from djconvert.paths import infer_access_rules


def _touch(path: Path, data: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_detects_each_kind_of_library(tmp_path: Path) -> None:
    mixxx = _touch(tmp_path / "mixxx/mixxxdb.sqlite")
    rekordbox = _touch(tmp_path / "rb/master.db")
    _touch(tmp_path / "stick/PIONEER/rekordbox/export.pdb")
    _touch(tmp_path / "stick/_Serato_/database V2")
    xml = _touch(tmp_path / "xml/collection.xml", b"<?xml?><DJ_PLAYLISTS Version='1.0.0'>")
    _touch(tmp_path / "xml/other.xml", b"<nope/>")

    assert detect_libraries(tmp_path / "mixxx") == [{"format": "mixxx", "path": str(mixxx)}]
    assert detect_libraries(tmp_path / "rb") == [{"format": "rekordbox_db", "path": str(rekordbox)}]
    assert detect_libraries(rekordbox) == [{"format": "rekordbox_db", "path": str(rekordbox)}]
    assert [lib["format"] for lib in detect_libraries(tmp_path / "stick")] == [
        "rekordbox_usb",
        "serato",
    ]
    assert detect_libraries(tmp_path / "stick/PIONEER/rekordbox/export.pdb") == [
        {"format": "rekordbox_usb", "path": str(tmp_path / "stick")}
    ]
    assert detect_libraries(tmp_path / "xml") == [{"format": "rekordbox_xml", "path": str(xml)}]
    assert detect_libraries(tmp_path / "nothing") == []


def test_infers_where_the_music_is(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    _touch(music / "house/a.mp3")
    _touch(music / "house/b.mp3")
    locations = ["C:/users/dj/Music/house/a.mp3", "C:\\users\\dj\\Music\\house\\b.mp3", "gone.mp3"]
    assert infer_access_rules(locations, [tmp_path / "other", music]) == [
        ("C:/users/dj/Music", str(music))
    ]
