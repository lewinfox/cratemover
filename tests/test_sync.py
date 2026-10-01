from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import needs_ffmpeg

from cratemover.convert import (
    ReadOptions,
    SyncSide,
    WriteOptions,
    read_library,
    sync_libraries,
    write_library,
)
from cratemover.model import Cue, CueRole, Format, Library, Playlist, TempoMarker, Track
from cratemover.sync import (
    CuePolicy,
    Direction,
    PlaylistPolicy,
    Prefer,
    SyncOptions,
    match_tracks,
    merge,
)


def _lib(*tracks: Track, playlists: dict[str, list[str]] | None = None) -> Library:
    lib = Library("test")
    for t in tracks:
        lib.add_track(t)
    for name, ids in (playlists or {}).items():
        lib.playlists.children.append(Playlist(name, ids))
    return lib


def test_matching_tiers() -> None:
    base = _lib(
        Track("b1", "/usb/Contents/A/x.mp3", title="X", artist="A", file_size=100),
        Track("b2", "/usb/Contents/B/y.mp3", title="Y", artist="B", duration_s=200),
        Track("b3", "/usb/Contents/C/z.mp3", title="Zed", artist="C"),
        Track("b4", "/usb/Music/w.mp3", title="W"),
    )
    incoming = _lib(
        Track("i1", "/home/me/Music/x.mp3", title="Other title", file_size=100),
        Track("i2", "/home/me/Music/y2.mp3", title="y", artist="b", duration_s=201),
        Track("i3", "/home/me/Music/z.mp3", title="Zed (edit)", artist="C"),
        Track("i4", "/home/me/Music/w.mp3", title="W"),
        Track("i5", "/home/me/Music/new.mp3", title="New"),
    )
    matches, rules = match_tracks(
        base, incoming, SyncOptions(path_rules=[("/home/me/Music", "/usb/Music")])
    )
    assert matches == {"i1": "b1", "i2": "b2", "i3": "b3", "i4": "b4"}
    assert rules == {"i1": "name+size", "i2": "artist+title", "i3": "file name", "i4": "path"}


def test_merge_cues_playlists_and_new_tracks() -> None:
    base = _lib(
        Track(
            "b1",
            "/a.mp3",
            title="A",
            cues=[Cue(CueRole.CUE, 1000, slot=0, name="base")],
            grid=[TempoMarker(10, 120)],
        ),
        playlists={"Set": ["b1"]},
    )
    incoming = _lib(
        Track(
            "i1",
            "/a.mp3",
            title="A",
            genre="House",
            cues=[
                Cue(CueRole.CUE, 1100, slot=0, name="inc"),
                Cue(CueRole.CUE, 5000, slot=1),
                Cue(CueRole.CUE, 9000),
            ],
            grid=[TempoMarker(20, 121)],
        ),
        Track("i2", "/b.mp3", title="B"),
        playlists={"Set": ["i2", "i1"], "New list": ["i2"]},
    )
    merged, report = merge(base, incoming, SyncOptions())
    a = merged.tracks["b1"]
    assert [(c.slot, c.position_ms, c.name) for c in a.cues] == [
        (1, 5000, ""),
        (None, 9000, ""),
        (0, 1100, "inc"),
    ] or [(c.slot, c.position_ms, c.name) for c in sorted(a.cues, key=lambda c: c.position_ms)] == [
        (0, 1100, "inc"),
        (1, 5000, ""),
        (None, 9000, ""),
    ]
    assert a.genre == "House" and a.grid[0].position_ms == 10  # grids: fill only
    assert report.added == 1 and report.updated == 1 and report.playlists_added == 1
    set_list = next(p for _, p in merged.playlists.walk() if p.name == "Set")
    assert set_list.track_ids == ["b1", "sync:i2"]  # base order, then new tracks
    assert base.tracks["b1"].cues[0].name == "base"  # inputs untouched

    kept, _ = merge(
        base, incoming, SyncOptions(prefer=Prefer.BASE, playlists=PlaylistPolicy.REPLACE)
    )
    assert kept.tracks["b1"].hot_cues[0].name == "base"
    assert next(p for _, p in kept.playlists.walk() if p.name == "Set").track_ids == [
        "sync:i2",
        "b1",
    ]
    filled, _ = merge(base, incoming, SyncOptions(cues=CuePolicy.FILL))
    assert [c.name for c in filled.tracks["b1"].cues] == ["base"]


def test_transcoded_pair_shifts_cues() -> None:
    base = _lib(Track("b", "/usb/x.mp3", title="X", artist="A"))
    incoming = _lib(
        Track("i", "/lib/x.ogg", title="X", artist="A", cues=[Cue(CueRole.CUE, 1000, slot=0)])
    )
    merged, _ = merge(base, incoming, SyncOptions())
    assert merged.tracks["b"].cues[0].position_ms == 1026


@needs_ffmpeg
def test_two_way_sync_mixxx_and_stick(library_copy: Path, tmp_path: Path) -> None:
    stick = tmp_path / "stick"
    stick.mkdir()
    mixxx = ReadOptions(Format.MIXXX, str(library_copy / "mixxx"))
    usb = ReadOptions(Format.REKORDBOX_USB, str(stick))
    write_library(
        read_library(mixxx),
        WriteOptions(Format.REKORDBOX_USB, str(stick), playlists=["Warm Up"]),
        [],
    )
    # A playlist made "on the stick", and a new hot cue in Mixxx.
    lib = read_library(usb)
    lib.playlists.children.append(Playlist("From CDJ", [next(iter(lib.tracks))]))
    write_library(lib, WriteOptions(Format.REKORDBOX_USB, str(stick), in_place=True), [])
    db = sqlite3.connect(library_copy / "mixxx" / "mixxxdb.sqlite")
    track_id = db.execute("SELECT id FROM library WHERE title = 'Drift'").fetchone()[0]
    db.execute(
        "INSERT INTO cues (track_id, type, position, length, hotcue, label, color) VALUES (?, 1, ?, 0, 6, 'New', 255)",
        (track_id, 5.0 * 88200),
    )
    db.commit()
    db.close()

    result = sync_libraries(
        SyncSide(mixxx, WriteOptions(Format.MIXXX, "")),
        SyncSide(usb, WriteOptions(Format.REKORDBOX_USB, "")),
        Direction.BOTH,
        SyncOptions(),
    )
    assert result.a_to_b and result.b_to_a
    stick_lib = read_library(usb)
    mixxx_lib = read_library(mixxx)
    stick_names = {p.name for _, p in stick_lib.playlists.walk()}
    mixxx_names = {p.name for _, p in mixxx_lib.playlists.walk()}
    assert {"Warm Up", "Peak Time", "Techno", "From CDJ"} <= stick_names
    assert "From CDJ" in mixxx_names
    drift = next(t for t in stick_lib.tracks.values() if t.title == "Drift")
    assert (6, 5000) in [(c.slot, round(c.position_ms)) for c in drift.hot_cues]
    # Mixxx's playlists weren't duplicated, and a backup was made.
    assert sorted(p.name for _, p in mixxx_lib.playlists.walk()).count("Warm Up") == 1
    assert list((library_copy / "mixxx").glob("mixxxdb.sqlite.cratemover-*"))
    # Syncing again changes nothing.
    again = sync_libraries(
        SyncSide(mixxx, WriteOptions(Format.MIXXX, "")),
        SyncSide(usb, WriteOptions(Format.REKORDBOX_USB, "")),
        Direction.BOTH,
        SyncOptions(),
        dry_run=True,
    )
    for side in (again.a_to_b, again.b_to_a):
        assert side is not None
        report = side["report"]
        # Zeta's file is missing, so it can't reach the stick and is offered again each time.
        assert report["updated"] == 0 and report["playlists_added"] == 0, report["details"]
