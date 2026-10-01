from __future__ import annotations

from pathlib import Path

from cratemover.layout import rekordbox_path, serato_path, target_paths
from cratemover.model import Format, Library, Playlist, Track


def test_rekordbox_layout_matches_rekordbox_7() -> None:
    # Seen on stick UFC8: an 81-character stem cut to 44, mid-word, trailing space kept.
    track = Track("1", "/x", artist="Jerome Robins")
    name = "[HouseU] Jerome Robins - Groovejet (If This Aint Love) (Earth n Days Remix).mp3"
    path = rekordbox_path(track, name)
    assert (
        str(path)
        == "Contents/Jerome Robins/UnknownAlbum/[HouseU] Jerome Robins - Groovejet (If This .mp3"
    )
    assert (
        str(rekordbox_path(Track("2", "/y"), "a.mp3"))
        == "Contents/UnknownArtist/UnknownAlbum/a.mp3"
    )
    assert str(rekordbox_path(Track("3", "/z", artist="AC/DC"), "b.mp3")).startswith(
        "Contents/AC_DC/"
    )


def test_serato_layout_stores_a_shared_track_once() -> None:
    # Serato copies a track in two crates into both folders and lists both copies in both
    # crates (a bug, stick FX5Z). We store it once, in its first crate's folder.
    lib = Library("test")
    for i in "abc":
        lib.add_track(Track(i, f"/stick/{i}.mp3"))
    lib.playlists.children = [Playlist("One", ["a", "b"]), Playlist("Two", ["b", "c"])]
    local = {i: Path(f"/stick/{i}.mp3") for i in "abc"}
    paths = target_paths(lib, Format.SERATO, local, set())
    assert {k: str(v) for k, v in paths.items()} == {
        "a": "One/a.mp3",
        "b": "One/b.mp3",  # its first crate only
        "c": "Two/c.mp3",
    }
    assert str(serato_path("Peak: Time", "x.mp3")) == "Peak_ Time/x.mp3"


def test_clashing_names_get_numbered() -> None:
    lib = Library("test")
    long = "x" * 50
    for i in "ab":  # both cut to the same 44-character name in the same folder
        lib.add_track(Track(i, f"/stick/{i}/{long}.mp3", artist="A", album="B"))
    local = {i: Path(f"/stick/{i}/{long}.mp3") for i in "ab"}
    paths = target_paths(
        lib, Format.REKORDBOX_USB, local, {"contents/a/b/" + "x" * 44 + " (2).mp3"}
    )
    assert sorted(p.name for p in paths.values()) == ["x" * 44 + " (3).mp3", "x" * 44 + ".mp3"]
