from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES, needs_ffmpeg

from cratemover.serato import markers as m
from cratemover.serato.binfile import Field, dump, parse
from cratemover.serato.tags import read_tags, write_tags

TAGS = FIXTURES / "serato-tags"


@pytest.mark.parametrize(
    "name",
    [
        "database_v2_test.bin",
        "database_v2_duplicates.bin",
        "TestCrate.crate",
        "TestSmartCrate.scrate",
    ],
)
def test_binfile_round_trips_real_files(name: str) -> None:
    data = (FIXTURES / "serato-db" / name).read_bytes()
    assert dump(parse(data)) == data


def test_database_fields() -> None:
    fields = parse((FIXTURES / "serato-db" / "database_v2_test.bin").read_bytes())
    assert fields[0] == Field("vrsn", "2.0/Serato Scratch LIVE Database")
    track = fields[1]
    assert track.get("pfil") == "Users/bvand/Music/DJ Tracks/Zeds Dead - In The Beginning.mp3"
    assert track.get("tbpm") == "70.00"
    assert track.get("uadd") == 1747147273
    assert track.get("bmis") is False


@pytest.mark.parametrize(
    "path", sorted((TAGS / "mp3" / "markers2").glob("*")), ids=lambda p: p.name
)
def test_markers2_id3_decodes_and_reencodes(path: Path) -> None:
    data = path.read_bytes()
    markers = m.decode_markers2_id3(data)
    again = m.decode_markers2_id3(m.encode_markers2_id3(markers))
    assert again == markers
    if len(data) == m.MARKERS2_MIN_SIZE:  # not padded to a larger allocation
        assert m.encode_markers2_id3(markers) == data


def test_markers2_contents() -> None:
    markers = m.decode_markers2_id3(
        (TAGS / "mp3/markers2/hotcues-with-names.octet-stream").read_bytes()
    )
    assert [(c.index, c.position_ms, c.name) for c in markers.cues][:2] == [
        (0, 0, "Hello, World!"),
        (1, 218456, "äöüß"),
    ]
    loops = m.decode_markers2_id3((TAGS / "mp3/markers2/saved-loops.octet-stream").read_bytes())
    assert (loops.loops[0].start_ms, loops.loops[0].end_ms) == (0, 2086)
    locked = m.decode_markers2_id3(
        (TAGS / "mp3/markers2/bpmlock-enabled.octet-stream").read_bytes()
    )
    assert locked.bpm_locked is True


@pytest.mark.parametrize(
    "path", sorted((TAGS / "mp3" / "markers_").glob("*")), ids=lambda p: p.name
)
def test_markers1_round_trip(path: Path) -> None:
    data = path.read_bytes()
    cues, loops = m.decode_markers1_id3(data)
    colour = m._from_serato32(int.from_bytes(data[-4:], "big"))
    assert m.encode_markers1_id3(m.Markers2(cues, loops, colour)) == data


@pytest.mark.parametrize(
    "path",
    sorted((TAGS / "flac" / "markers2").glob("*")) + sorted((TAGS / "mp4" / "markers2").glob("*")),
    ids=lambda p: f"{p.parent.parent.name}-{p.name}",
)
def test_markers2_base64_variants(path: Path) -> None:
    markers = m.decode_markers2_b64(path.read_bytes())
    assert m.decode_markers2_b64(m.encode_markers2_b64(markers)) == markers


def test_beatgrid_round_trip() -> None:
    data = (TAGS / "mp3/beatgrid/manual-beatgrid-changing-tempo.octet-stream").read_bytes()
    grid = m.decode_beatgrid(data)
    assert (
        len(grid) == 8 and grid[0].beats_to_next == 32 and grid[-1].bpm == pytest.approx(117, 0.01)
    )
    assert m.encode_beatgrid(grid, data[-1]) == data
    b64 = (TAGS / "flac/beatgrid/analyzed.octet-stream").read_bytes()
    assert m.decode_beatgrid_b64(
        m.encode_beatgrid_b64(m.decode_beatgrid_b64(b64))
    ) == m.decode_beatgrid_b64(b64)


def test_real_track_tags() -> None:
    markers = m.decode_markers2_id3((TAGS / "real-markers2.bin").read_bytes())
    assert [c.position_ms for c in markers.cues] == [45104, 67474, 112125, 134451, 156776, 201428]
    grid = m.decode_beatgrid((TAGS / "real-beatgrid.bin").read_bytes())
    assert grid[0].beats_to_next == 32 and grid[1].bpm == pytest.approx(86, abs=0.01)


@needs_ffmpeg
@pytest.mark.parametrize("suffix", [".mp3", ".flac", ".m4a", ".ogg", ".aiff", ".wav"])
def test_write_then_read_tags(tmp_path: Path, suffix: str) -> None:
    from fakelib import make_audio

    path = make_audio(tmp_path / f"t{suffix}", seconds=2)
    markers = m.Markers2(
        cues=[m.SeratoCue(0, 250, 0xCC0000, "Drop"), m.SeratoCue(3, 1500, 0x0000CC, "")],
        loops=[m.SeratoLoop(0, 500, 1000, name="L")],
        track_colour=0xFFFFFF,
        bpm_locked=True,
    )
    grid = [m.GridMarker(0.25, beats_to_next=2), m.GridMarker(1.25, bpm=121.5)]
    write_tags(path, markers, grid)
    tags = read_tags(path)
    assert [(c.index, c.position_ms, c.colour, c.name) for c in tags.markers.cues] == [
        (0, 250, 0xCC0000, "Drop"),
        (3, 1500, 0x0000CC, ""),
    ]
    assert [(lp.start_ms, lp.end_ms, lp.name) for lp in tags.markers.loops] == [(500, 1000, "L")]
    if suffix != ".ogg":  # no known Ogg beat grid tag
        assert [g.position_s for g in tags.grid] == [0.25, 1.25]
        assert tags.grid[-1].bpm == pytest.approx(121.5)
