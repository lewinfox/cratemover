from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import FIXTURES

from cratemover.convert import (
    ReadOptions,
    WriteOptions,
    read_library,
    write_library,
)
from cratemover.model import CueRole, Library, Track
from cratemover.paths import apply_rules, parse_rules
from cratemover.serato.library import from_serato_path, to_serato_path


def _by_title(library: Library) -> dict[str, Track]:
    return {t.title: t for t in library.tracks.values()}


def _hot(track: Track) -> list[tuple[int | None, int]]:
    return [(c.slot, round(c.position_ms)) for c in track.hot_cues]


def _grid(track: Track) -> list[tuple[int, float, int]]:
    return [(round(m.position_ms), round(m.bpm, 2), m.beat) for m in track.grid]


def test_read_mixxx(mixxx_root: Path) -> None:
    lib = read_library(ReadOptions("mixxx", str(mixxx_root / "mixxx")))
    tracks = _by_title(lib)
    assert len(tracks) == 6
    alpha = tracks["First Light"]
    assert _hot(alpha) == [(0, 250), (1, 4250), (2, 2250)]
    assert {c.role for c in alpha.cues} >= {CueRole.MAIN, CueRole.INTRO, CueRole.LOOP}
    assert _grid(tracks["Drift"]) == [(500, 100.0, 1), (4100, 104.0, 3)]
    # AAC priming (1024 samples from ffmpeg's encoder) moves Mixxx positions later.
    assert _hot(tracks["Apple"]) == [(0, 323), (1, 2223)]
    names = {(parents, p.name) for parents, p in lib.playlists.walk()}
    assert names == {((), "Warm Up"), ((), "Peak Time"), (("Crates",), "Techno")}


@pytest.mark.parametrize("target", ["serato", "rekordbox_xml", "mixxx"])
def test_round_trip_from_mixxx(library_copy: Path, tmp_path: Path, target: str) -> None:
    source = read_library(ReadOptions("mixxx", str(library_copy / "mixxx")))
    out = tmp_path / "out"
    result = write_library(source, WriteOptions(target, str(out), serato_write_tags=True), [])
    assert result.summary["tracks"] == 6
    path = {"serato": out, "rekordbox_xml": out / "rekordbox.xml", "mixxx": out}[target]
    back = _by_title(read_library(ReadOptions(target, str(path))))
    before = _by_title(source)
    for title in ("First Light", "Second Wind", "Drift", "Apple"):
        # Serato stores whole milliseconds.
        assert _hot(back[title])[:3] == _hot(before[title])[:3], title
        assert [(p, round(b)) for p, b, _ in _grid(back[title])][:1] == [
            (p, round(b)) for p, b, _ in _grid(before[title])
        ][:1], title
    assert back["First Light"].key == before["First Light"].key
    playlists = {
        p.name: len(p.track_ids or [])
        for _, p in read_library(ReadOptions(target, str(path))).playlists.walk()
    }
    assert playlists["Peak Time"] == 4 and playlists["Techno"] == 3


def test_mixxx_output_is_a_valid_mixxx_database(library_copy: Path, tmp_path: Path) -> None:
    source = read_library(ReadOptions("mixxx", str(library_copy / "mixxx")))
    write_library(source, WriteOptions("mixxx", str(tmp_path)), [])
    db = sqlite3.connect(tmp_path / "mixxxdb.sqlite")
    settings = dict(db.execute("SELECT name, value FROM settings"))
    assert settings["mixxx.schema.version"] == "40"
    assert db.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 6
    assert db.execute("SELECT COUNT(*) FROM crates").fetchone()[0] == 1
    # Cue positions are in stereo samples: 0.25 s at 44.1 kHz.
    assert db.execute("SELECT position FROM cues WHERE hotcue = 0 AND type = 1").fetchone()[0] in (
        22050,
        22050 + 26 * 88.2,
    )


def test_mixxx_merge_into_base_database(library_copy: Path, tmp_path: Path) -> None:
    source = read_library(ReadOptions("mixxx", str(library_copy / "mixxx")))
    result = write_library(
        source,
        WriteOptions("mixxx", str(tmp_path), mixxx_base_database=str(library_copy / "mixxx")),
        [],
    )
    db = sqlite3.connect(tmp_path / "mixxxdb.sqlite")
    assert db.execute("SELECT COUNT(*) FROM library").fetchone()[0] == 6  # updated, not duplicated
    assert result.files


def test_serato_read_real_database(tmp_path: Path) -> None:
    serato = tmp_path / "_Serato_"
    (serato / "Subcrates").mkdir(parents=True)
    (serato / "database V2").write_bytes((FIXTURES / "serato-db/database_v2_test.bin").read_bytes())
    (serato / "Subcrates" / "Test%%Nested.crate").write_bytes(
        (FIXTURES / "serato-db/TestCrate.crate").read_bytes()
    )
    lib = read_library(ReadOptions("serato", str(tmp_path)))
    tracks = _by_title(lib)
    zeds = tracks["In The Beginning"]
    assert zeds.location == "/Users/bvand/Music/DJ Tracks/Zeds Dead - In The Beginning.mp3"
    assert zeds.bpm == 70 and zeds.label == "SOUL_C" and zeds.year == "2012"
    assert zeds.key is not None and zeds.date_added is not None
    [(parents, crate)] = list(lib.playlists.walk())
    assert parents == ("Test",) and crate.name == "Nested" and len(crate.track_ids or []) == 3


def test_rekordbox_xml_read_real_file() -> None:
    lib = read_library(
        ReadOptions("rekordbox_xml", str(FIXTURES / "rekordbox/rekordbox5-database.xml"))
    )
    track = next(t for t in lib.tracks.values() if t.title == "Demo Track 1")
    assert track.location.startswith("C:/Music/PioneerDJ/Demo Tracks/")
    assert [round(c.position_ms) for c in track.cues if c.slot is None][:2] == [25, 15025]
    lib6 = read_library(
        ReadOptions("rekordbox_xml", str(FIXTURES / "rekordbox/rekordbox6-database.xml"))
    )
    demo2 = next(t for t in lib6.tracks.values() if t.title == "Demo Track 2")
    assert len(demo2.grid) == 1  # the per-beat TEMPO entries collapse into one section


def test_path_rules() -> None:
    rules = parse_rules("/home/me/Music => /Users/me/Music\nD:\\DJ => /Volumes/USB/DJ")
    assert apply_rules("/home/me/Music/a/b.mp3", rules) == "/Users/me/Music/a/b.mp3"
    assert apply_rules("D:\\DJ\\x.mp3", rules) == "/Volumes/USB/DJ/x.mp3"
    assert apply_rules("/other/x.mp3", rules) == "/other/x.mp3"
    assert (
        apply_rules("/Users/me/Music/x.mp3", [("/Users/me", "C:\\Users\\me")])
        == "C:\\Users\\me\\Music\\x.mp3"
    )


def test_serato_paths() -> None:
    assert to_serato_path("/Users/me/Music/a.mp3", "/") == "Users/me/Music/a.mp3"
    assert to_serato_path("C:\\Users\\me\\a.mp3", "/") == "Users/me/a.mp3"
    assert to_serato_path("/Volumes/USB/Music/a.mp3", "/Volumes/USB") == "Music/a.mp3"
    assert from_serato_path("Music/a.mp3", "/Volumes/USB") == "/Volumes/USB/Music/a.mp3"
    assert from_serato_path("Users/me/a.mp3", "C:/") == "C:/Users/me/a.mp3"


def test_selection(mixxx_root: Path, tmp_path: Path) -> None:
    source = read_library(ReadOptions("mixxx", str(mixxx_root / "mixxx")))
    result = write_library(
        source, WriteOptions("rekordbox_xml", str(tmp_path), playlists=["Warm Up"]), []
    )
    assert result.summary == {
        "tracks": 3,
        "playlists": 1,
        "hot_cues": 6,
        "memory_cues": 3,
        "gridded": 3,
    }
    assert len(source.tracks) == 6  # the source library is not modified


def test_serato_sample_rate_text() -> None:
    from cratemover.serato.library import _sample_rate

    assert [_sample_rate(t) for t in ("44100", "44.1", "44.1k", "48.0 kHz", "")] == [
        44100,
        44100,
        44100,
        48000,
        0,
    ]


def test_serato_mp3_frame_offset(library_copy: Path, tmp_path: Path) -> None:
    """ffmpeg's MP3s have an Info header without a LAME tag: Serato counts that frame."""
    from cratemover.offsets import mp3_header_case, serato_offset_ms
    from cratemover.serato.tags import read_tags

    mp3 = library_copy / "music" / "Alpha - First Light.mp3"
    assert mp3_header_case(mp3) == "B"
    assert serato_offset_ms(mp3, "mp3") == pytest.approx(1152000 / 44100)
    source = read_library(ReadOptions("mixxx", str(library_copy / "mixxx")))
    write_library(source, WriteOptions("serato", str(tmp_path), serato_write_tags=True), [])
    raw = {c.index: c.position_ms for c in read_tags(mp3).markers.cues}
    assert raw[0] == 250 + 26  # hot cue A at 0.25 s in the reference timeline
    back = _by_title(read_library(ReadOptions("serato", str(tmp_path))))["First Light"]
    assert round(back.hot_cues[0].position_ms) == 250  # and back again
