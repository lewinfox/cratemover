from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from conftest import FIXTURES

pytest.importorskip("sqlcipher3")

from djconvert.rekordbox_db import grid_from_anlz, read_rekordbox_db

ANLZ = FIXTURES / "rekordbox/anlz/demo-track-1.DAT"


def test_grid_from_anlz() -> None:
    grid = grid_from_anlz(ANLZ)
    assert len(grid) == 1
    assert grid[0].bpm == 128 and grid[0].position_ms == pytest.approx(25) and grid[0].beat == 1


def _make_master_db(root: Path) -> Path:
    """A plain (unencrypted) master.db with just the tables and columns we read."""
    db_path = root / "master.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        """
        CREATE TABLE djmdContent (ID, FolderPath, Title, ArtistID, AlbumID, GenreID, BPM, Length,
            TrackNo, BitRate, Commnt, Rating, ReleaseYear, RemixerID, LabelID, KeyID, ColorID,
            DJPlayCount, AnalysisDataPath, FileSize, ComposerID, SampleRate, DateCreated,
            rb_local_deleted);
        CREATE TABLE djmdArtist (ID, Name);
        CREATE TABLE djmdAlbum (ID, Name);
        CREATE TABLE djmdGenre (ID, Name);
        CREATE TABLE djmdKey (ID, ScaleName);
        CREATE TABLE djmdLabel (ID, Name);
        CREATE TABLE djmdCue (ID, ContentID, InMsec, OutMsec, Kind, ColorTableIndex, Comment,
            rb_local_deleted);
        CREATE TABLE djmdPlaylist (ID, Seq, Name, Attribute, ParentID);
        CREATE TABLE djmdSongPlaylist (ID, PlaylistID, ContentID, TrackNo);
        INSERT INTO djmdArtist VALUES ('a1', 'Loopmasters');
        INSERT INTO djmdKey VALUES ('k1', 'Fm');
        INSERT INTO djmdContent VALUES ('1', 'C:/Music/Demo Track 1.mp3', 'Demo Track 1', 'a1',
            NULL, NULL, 12800, 172, 3, 320, 'hi', 4, 2020, NULL, NULL, 'k1', 5, 7,
            '/PIONEER/USBANLZ/P016/0000875E/ANLZ0000.DAT', 6899624, NULL, 44100,
            '2022-04-09', 0);
        INSERT INTO djmdCue VALUES (1, '1', 25, -1, 0, 0, '', 0);
        INSERT INTO djmdCue VALUES (2, '1', 15025, -1, 1, 3, 'Drop', 0);
        INSERT INTO djmdCue VALUES (3, '1', 30025, -1, 5, 0, '', 0);
        INSERT INTO djmdCue VALUES (4, '1', 45025, 46900, 2, 0, 'Loop', 0);
        INSERT INTO djmdCue VALUES (5, '1', 50000, -1, 3, 0, 'gone', 1);
        INSERT INTO djmdPlaylist VALUES ('f1', 1, 'Sets', 1, 'root');
        INSERT INTO djmdPlaylist VALUES ('p1', 1, 'Friday', 0, 'f1');
        INSERT INTO djmdPlaylist VALUES ('p2', 2, 'Smart', 4, 'root');
        INSERT INTO djmdPlaylist VALUES ('p3', 3, 'Cloud', -128, 'root');
        INSERT INTO djmdSongPlaylist VALUES (1, 'p1', '1', 1);
        """
    )
    db.commit()
    db.close()
    anlz_dir = root / "share/PIONEER/USBANLZ/P016/0000875E"
    anlz_dir.mkdir(parents=True)
    shutil.copy(ANLZ, anlz_dir / "ANLZ0000.DAT")
    return root


def test_read_master_db(tmp_path: Path) -> None:
    library = read_rekordbox_db(_make_master_db(tmp_path))
    [track] = library.tracks.values()
    assert (track.title, track.artist, track.bpm, track.rating, track.play_count) == (
        "Demo Track 1",
        "Loopmasters",
        128.0,
        4,
        7,
    )
    assert track.colour == 0x00FF00 and track.key is not None and track.key.minor
    cues = sorted((c.position_ms, c.slot, c.end_ms, c.name) for c in track.cues)
    assert cues == [
        (25, None, None, ""),
        (15025, 0, None, "Drop"),
        (30025, 3, None, ""),
        (45025, 1, 46900, "Loop"),
    ]
    assert [(p, n.name, n.track_ids) for p, n in library.playlists.walk()] == [
        (("Sets",), "Friday", ["rbdb:1"])
    ]
    assert len(track.grid) == 1 and track.grid[0].bpm == 128
