from __future__ import annotations

import gzip
import shutil
import struct
from pathlib import Path

import pytest
from conftest import FIXTURES, needs_ffmpeg
from oracles.rekordbox_pdb import Database

from cratemover.convert import ReadOptions, WriteOptions, read_library, write_library
from cratemover.model import CueRole, Format
from cratemover.pioneer import anlz
from cratemover.pioneer.pdb import decode_string, encode_string, read_pdb, write_pdb
from cratemover.pioneer.usb import OneLibraryMode

RB = FIXTURES / "rekordbox"
STICK = RB / "stick-6.8.6"
BIG = gzip.decompress((RB / "export-3886-tracks.pdb.gz").read_bytes())


def _pages(data: bytes, table: int) -> tuple[int, int]:
    for i in range(struct.unpack_from("<I", data, 8)[0]):
        type_, _, first, last = struct.unpack_from("<IIII", data, 0x1C + 16 * i)
        if type_ == table:
            return first, last
    raise KeyError(table)


def _masked(page: bytes) -> bytes:
    """A page without its index/next/sequence words and transaction bookkeeping."""
    v = bytearray(page)
    v[4:0x14] = bytes(16)
    v[0x20:0x24] = bytes(4)
    slots = page[0x18] | (page[0x19] & 0x1F) << 8
    for g in range((slots + 15) // 16):
        base = 4096 - g * 0x24
        v[base - 2 : base] = b"\0\0"
    return bytes(v)


@pytest.mark.parametrize(
    "data",
    [
        BIG,
        (RB / "one-song-export.pdb").read_bytes(),
        (STICK / "PIONEER/rekordbox/export.pdb").read_bytes(),
    ],
    ids=["3886-tracks", "one-song", "rb-6.8.6"],
)
def test_pdb_reader_agrees_with_independent_reader(data: bytes) -> None:
    ours, theirs = read_pdb(data), Database(data)
    assert [t.file_path for t in ours.tracks] == [t.file_path for t in theirs.tracks]
    assert [t.title for t in ours.tracks] == [t.title for t in theirs.tracks]
    assert sum(len(n.track_ids) for n in ours.playlists) == len(theirs.playlist_entries)
    assert len(ours.artists) == len(theirs.artists) and len(ours.albums) == len(theirs.albums)


def test_pdb_rewrite_round_trips_a_big_library() -> None:
    pdb = read_pdb(BIG)
    out = write_pdb(pdb)
    again, oracle = read_pdb(out), Database(out)
    assert len(pdb.tracks) == 3886
    assert [(t.id, t.title, t.file_path, t.tempo, t.artist_id) for t in again.tracks] == [
        (t.id, t.title, t.file_path, t.tempo, t.artist_id) for t in pdb.tracks
    ]
    assert [(n.id, n.name, n.track_ids) for n in again.playlists] == [
        (n.id, n.name, n.track_ids) for n in pdb.playlists
    ]
    assert len(oracle.tracks) == 3886 and len(oracle.playlist_entries) == 7440
    # Header invariants players check.
    header_seq = struct.unpack_from("<I", out, 0x14)[0]
    for index in range(1, len(out) // 4096):
        page = out[index * 4096 : (index + 1) * 4096]
        if page[0x1B] == 0x24:
            assert struct.unpack_from("<I", page, 0x10)[0] < header_seq
            assert struct.unpack_from("<H", page, 0x1E)[0] % 4 == 0  # rows 4-byte aligned


@pytest.mark.parametrize(
    ("table", "name"),
    [(6, "colors"), (16, "columns"), (17, "categories"), (18, "sorts"), (1, "genres"), (2, "artists"),
     (3, "albums"), (4, "labels"), (7, "playlist tree"), (8, "playlist entries")],
)  # fmt: skip
def test_written_pages_match_rekordbox_byte_for_byte(table: int, name: str) -> None:
    theirs = (STICK / "PIONEER/rekordbox/export.pdb").read_bytes()
    ours = write_pdb(read_pdb(theirs))
    a, b = _pages(theirs, table)[1], _pages(ours, table)[1]
    assert _masked(theirs[a * 4096 : (a + 1) * 4096]) == _masked(ours[b * 4096 : (b + 1) * 4096]), (
        name
    )


def test_device_sql_strings() -> None:
    assert encode_string("") == b"\x03"
    assert encode_string("1A") == b"\x07" + b"1A"
    for text in ["", "Techno", "Rødhåd", "22. 盾", "x" * 200]:
        assert decode_string(encode_string(text), 0) == text


def test_anlz_folder_hash_matches_rekordbox() -> None:
    for track in read_pdb((STICK / "PIONEER/rekordbox/export.pdb").read_bytes()).tracks:
        assert track.analyze_path.startswith(anlz.anlz_dir(track.file_path) + "/")
    assert (
        anlz.anlz_dir("/Contents/XamarA/UnknownAlbum/Unreal.mp3")
        == "/PIONEER/USBANLZ/P060/00012938"
    )


def test_anlz_cue_sections_match_rekordbox_byte_for_byte() -> None:
    for dat_path in sorted((STICK / "PIONEER/USBANLZ").rglob("ANLZ0000.DAT")):
        dat = anlz.AnlzFile.parse(dat_path.read_bytes())
        ext = anlz.AnlzFile.parse(dat_path.with_suffix(".EXT").read_bytes())
        assert anlz.AnlzFile.parse(dat.to_bytes()).to_bytes() == dat_path.read_bytes()
        cues = anlz.read_cues(dat, ext)
        assert len(cues) == 2 and all(c.slot is not None for c in cues)
        hot, memory = anlz.cue_lists(cues)
        bpm = anlz.read_grid(dat)[0].bpm
        assert (
            anlz.pcob(anlz.HOT, [c for c in hot if (c.slot or 0) < 3]).data
            == dat.all("PCOB")[0].data
        )
        assert anlz.pcob(anlz.MEMORY, memory).data == dat.all("PCOB")[1].data
        assert anlz.pco2(anlz.HOT, hot, bpm).data == ext.all("PCO2")[0].data
        assert anlz.pco2(anlz.MEMORY, memory, bpm).data == ext.all("PCO2")[1].data


def test_grid_expansion() -> None:
    from cratemover.model import TempoMarker

    beats = anlz.beats([TempoMarker(1250, 120, 3)], 2250)
    assert beats == [(1, 12000, 250), (2, 12000, 750), (3, 12000, 1250), (4, 12000, 1750)]
    grid = anlz.read_grid(
        anlz.AnlzFile([anlz.pqtz([TempoMarker(25, 128, 1), TempoMarker(60025, 130, 1)], 90000)])
    )
    assert [(round(m.position_ms), m.bpm) for m in grid] == [(25, 128), (60025, 130)]


def test_read_real_stick() -> None:
    lib = read_library(ReadOptions(Format.REKORDBOX_USB, str(STICK)))
    tracks = {t.title: t for t in lib.tracks.values()}
    source = tracks["Assign The Source (Remaster)"]
    assert source.artist == "Reboot" and source.bpm == 126 and source.genre == "Minimal / Deep Tech"
    assert [(c.slot, round(c.position_ms)) for c in source.hot_cues] == [(0, 286), (1, 248379)]
    assert source.grid[0].position_ms == 286 and source.grid[0].beat == 4
    assert [(p, n.name, len(n.track_ids)) for p, n in lib.playlists.walk()] == [((), "aaaaa", 2)]


def test_onelibrary_reads_like_export_pdb() -> None:
    pytest.importorskip("sqlcipher3")
    from cratemover.pioneer.onelibrary import read_onelibrary

    ol = read_onelibrary(STICK / "PIONEER/rekordbox/exportLibrary.db")
    pdb = read_pdb((STICK / "PIONEER/rekordbox/export.pdb").read_bytes())
    assert [(t.title, t.file_path, t.tempo) for t in ol.tracks] == [
        (t.title, t.file_path, t.tempo) for t in pdb.tracks
    ]
    assert [(n.name, n.track_ids) for n in ol.playlists] == [
        (n.name, n.track_ids) for n in pdb.playlists
    ]


@needs_ffmpeg
def test_write_stick_from_mixxx(library_copy: Path, tmp_path: Path) -> None:
    pyrekordbox_anlz = pytest.importorskip("pyrekordbox.anlz")
    stick = tmp_path / "stick"
    stick.mkdir()
    source = read_library(ReadOptions(Format.MIXXX, str(library_copy / "mixxx")))
    result = write_library(
        source, WriteOptions(Format.REKORDBOX_USB, str(stick), onelibrary=OneLibraryMode.OFF), []
    )
    assert "5 track(s) on the stick: 4 copied, 1 converted to MP3" in result.warnings[0]
    back = {
        t.title: t
        for t in read_library(ReadOptions(Format.REKORDBOX_USB, str(stick))).tracks.values()
    }
    before = {t.title: t for t in source.tracks.values()}
    assert set(back) == {"First Light", "Second Wind", "Drift", "Vorbis", "Apple"}
    for title in ("First Light", "Second Wind", "Drift", "Apple"):
        assert [(c.slot, round(c.position_ms)) for c in back[title].hot_cues] == [
            (c.slot, round(c.position_ms)) for c in before[title].hot_cues
        ]
    # Converted to MP3: moved by the encoder delay.
    assert back["Vorbis"].hot_cues[0].position_ms == before["Vorbis"].hot_cues[0].position_ms + 26
    assert back["Drift"].grid[1].bpm == pytest.approx(104, abs=0.01)
    memory = [c for c in back["First Light"].cues if c.slot is None]
    assert [(c.role, round(c.position_ms), c.end_ms) for c in memory] == [
        (CueRole.CUE, 250, None),
        (CueRole.LOOP, 6250, 7250),
    ]
    oracle = Database((stick / "PIONEER/rekordbox/export.pdb").read_bytes())
    assert sorted(t.title for t in oracle.tracks) == sorted(back)
    for f in (stick / "PIONEER/USBANLZ").rglob("ANLZ0000.*"):
        parsed = pyrekordbox_anlz.AnlzFile.parse_file(f)
        assert parsed.get("path").startswith("/Contents/")
    # Run again: waveforms are reused, nothing is copied twice.
    again = write_library(
        source, WriteOptions(Format.REKORDBOX_USB, str(stick), onelibrary=OneLibraryMode.OFF), []
    )
    assert "0 copied" in again.warnings[0] and "5 reused" in again.warnings[0]


@needs_ffmpeg
def test_onelibrary_written_alongside(library_copy: Path, tmp_path: Path) -> None:
    pytest.importorskip("sqlcipher3")
    from cratemover.pioneer.onelibrary import read_onelibrary

    stick = tmp_path / "stick"
    stick.mkdir()
    source = read_library(ReadOptions(Format.MIXXX, str(library_copy / "mixxx")))
    write_library(
        source,
        WriteOptions(
            Format.REKORDBOX_USB, str(stick), onelibrary=OneLibraryMode.ON, waveforms=False
        ),
        [],
    )
    db = stick / "PIONEER/rekordbox/exportLibrary.db"
    assert not db.read_bytes().startswith(b"SQLite format 3")  # encrypted
    ol = read_onelibrary(db)
    pdb = read_pdb((stick / "PIONEER/rekordbox/export.pdb").read_bytes())
    assert [t.file_path for t in ol.tracks] == [t.file_path for t in pdb.tracks]
    shutil.rmtree(stick / "PIONEER/USBANLZ")
