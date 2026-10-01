"""OneLibrary (Device Library Plus): ``PIONEER/rekordbox/exportLibrary.db``.

Newer players (CDJ-3000X, CDJ-1500X, OPUS-QUAD, OMNIS-DUO, XDJ-AZ, XDJ-AN)
only browse sticks that carry it. It is SQLCipher 4 with a fixed public key
(:mod:`cratemover.pioneer.keys`). The schema and fixed rows below were read from
a real Rekordbox 6.8.6 export (acrilique/rekordlib test data). Cue points,
beat grids and waveforms still come from the ANLZ files, which Rekordbox's own
export relies on too (its ``cue`` table was empty).

**Experimental:** nobody has published a hardware test of a third-party
OneLibrary database. Keep a copy of a Rekordbox-made stick to compare against.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .keys import ONE_LIBRARY, open_encrypted
from .pdb import Pdb

SCHEMA = """
CREATE TABLE content(content_id integer primary key, title varchar, titleForSearch varchar, subtitle varchar, bpmx100 integer, length integer, trackNo integer, discNo integer, artist_id_artist integer, artist_id_remixer integer, artist_id_originalArtist integer, artist_id_composer integer, artist_id_lyricist integer, album_id integer, genre_id integer, label_id integer, key_id integer, color_id integer, image_id integer, djComment varchar, rating integer, releaseYear integer, releaseDate varchar, dateCreated varchar, dateAdded varchar, path varchar, fileName varchar, fileSize integer, fileType integer, bitrate integer, bitDepth integer, samplingRate integer, isrc varchar, djPlayCount integer, isHotCueAutoLoadOn integer, isKuvoDeliverStatusOn integer, kuvoDeliveryComment varchar, masterDbId integer, masterContentId integer, analysisDataFilePath varchar, analysedBits integer, contentLink integer, hasModified integer, cueUpdateCount integer, analysisDataUpdateCount integer, informationUpdateCount integer);
CREATE TABLE genre(genre_id integer primary key, name varchar);
CREATE TABLE artist(artist_id integer primary key, name varchar, nameForSearch varchar);
CREATE TABLE album(album_id integer primary key, name varchar, artist_id integer, image_id integer, isComplation integer, nameForSearch varchar);
CREATE TABLE label(label_id integer primary key, name varchar);
CREATE TABLE key(key_id integer primary key, name varchar);
CREATE TABLE color(color_id integer primary key, name varchar);
CREATE TABLE playlist(playlist_id integer primary key, sequenceNo integer, name varchar, image_id integer, attribute integer, playlist_id_parent integer);
CREATE TABLE playlist_content(playlist_id integer, content_id integer, sequenceNo integer);
CREATE TABLE hotCueBankList(hotCueBankList_id integer primary key, sequenceNo integer, name varchar, image_id integer, attribute integer, hotCueBankList_id_parent integer);
CREATE TABLE hotCueBankList_cue(hotCueBankList_id integer, cue_id integer, sequenceNo integer);
CREATE TABLE history(history_id integer primary key, sequenceNo integer, name varchar, attribute integer, history_id_parent integer);
CREATE TABLE history_content(history_id integer, content_id integer, sequenceNo integer);
CREATE TABLE image(image_id integer primary key, path varchar);
CREATE TABLE cue(cue_id integer primary key, content_id integer, kind integer, colorTableIndex integer, cueComment varchar, isActiveLoop integer, beatLoopNumerator integer, beatLoopDenominator integer, inUsec integer, outUsec integer, in150FramePerSec integer, out150FramePerSec integer, inMpegFrameNumber integer, outMpegFrameNumber integer, inMpegAbs integer, outMpegAbs integer, inDecodingStartFramePosition integer, outDecodingStartFramePosition integer, inFileOffsetInBlock integer, OutFileOffsetInBlock integer, inNumberOfSampleInBlock integer, outNumberOfSampleInBlock integer);
CREATE TABLE menuItem(menuItem_id integer primary key, kind integer, name varchar);
CREATE TABLE category(category_id integer primary key, menuItem_id integer, sequenceNo integer, isVisible integer);
CREATE TABLE sort(sort_id integer primary key, menuItem_id integer, sequenceNo integer, isVisible integer, isSelectedAsSubColumn integer);
CREATE TABLE property(deviceName varchar, dbVersion varchar, numberOfContents integer, createdDate varchar, backGroundColorType integer, myTagMasterDBID integer);
CREATE TABLE recommendedLike(content_id_1 integer, content_id_2 integer, rating integer, createdDate integer);
CREATE TABLE myTag(myTag_id integer primary key, sequenceNo integer, name varchar, attribute integer, myTag_id_parent integer);
CREATE TABLE myTag_content(myTag_id integer, content_id integer);
CREATE INDEX index_playlist_content_playlist_id on playlist_content(playlist_id);
CREATE INDEX index_myTag_content_myTag_id on myTag_content(myTag_id);
CREATE INDEX index_myTag_content_content_id on myTag_content(content_id);
CREATE INDEX index_hotCueBankList_cue_hotCueBankList_id on hotCueBankList_cue(hotCueBankList_id);
"""

MENU_ITEMS = [
    (1, 128, "GENRE"), (2, 129, "ARTIST"), (3, 130, "ALBUM"), (4, 131, "TRACK"), (5, 133, "BPM"),
    (6, 134, "RATING"), (7, 135, "YEAR"), (8, 136, "REMIXER"), (9, 137, "LABEL"),
    (10, 138, "ORIGINAL ARTIST"), (11, 139, "KEY"), (12, 141, "CUE"), (13, 142, "COLOR"),
    (14, 146, "TIME"), (15, 147, "BITRATE"), (16, 148, "FILE NAME"), (17, 132, "PLAYLIST"),
    (18, 152, "HOT CUE BANK"), (19, 149, "HISTORY"), (20, 145, "SEARCH"), (21, 150, "COMMENTS"),
    (22, 140, "DATE ADDED"), (23, 151, "DJ PLAY COUNT"), (24, 144, "FOLDER"), (25, 161, "DEFAULT"),
    (26, 162, "ALPHABET"), (27, 170, "MATCHING"),
]  # fmt: skip
CATEGORIES = [
    (1, 1, 0, 0), (2, 2, 1, 1), (3, 3, 2, 1), (4, 4, 3, 1), (5, 17, 5, 1), (6, 5, 0, 0),
    (7, 6, 0, 0), (8, 7, 0, 0), (9, 8, 0, 0), (10, 9, 0, 0), (11, 10, 0, 0), (12, 11, 4, 1),
    (15, 13, 0, 0), (17, 24, 9, 1), (18, 20, 7, 1), (19, 14, 0, 0), (20, 15, 0, 0), (21, 16, 0, 0),
    (22, 19, 6, 1), (23, 18, 0, 0), (26, 27, 8, 1), (27, 22, 10, 1),
]  # fmt: skip
SORTS = [
    (0, 25, 1, 1, 0), (1, 26, 2, 1, 0), (2, 2, 3, 1, 0), (3, 3, 4, 1, 0), (4, 5, 5, 1, 0),
    (5, 6, 6, 1, 0), (6, 1, 0, 0, 0), (7, 21, 0, 0, 0), (8, 14, 0, 0, 0), (9, 8, 0, 0, 0),
    (10, 9, 0, 0, 0), (11, 10, 0, 0, 0), (12, 11, 7, 1, 0), (13, 15, 0, 0, 0), (15, 13, 0, 0, 0),
    (16, 23, 0, 0, 0), (17, 22, 0, 0, 0),
]  # fmt: skip
MY_TAG_CATEGORIES = [(1, 0, "Genre", 1, 0), (2, 1, "Components", 1, 0), (3, 2, "Situation", 1, 0),
                     (4, 3, "Untitled Column", 1, 0)]  # fmt: skip
COLORS = [(1, "Pink"), (2, "Red"), (3, "Orange"), (4, "Yellow"), (5, "Green"), (6, "Aqua"), (7, "Blue"),
          (8, "Purple")]  # fmt: skip


# property.dbVersion, as rekordbox 7.2.19 writes it (see write_onelibrary).
DB_VERSION = "1000"


def write_onelibrary(pdb: Pdb, path: Path) -> None:
    """Write ``exportLibrary.db`` describing the same library as ``pdb``."""
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        db_path = Path(tmp) / "exportLibrary.db"
        conn = open_encrypted(db_path, ONE_LIBRARY)
        conn.executescript(SCHEMA)
        conn.executemany("INSERT INTO color VALUES (?, ?)", COLORS)
        conn.executemany(
            "INSERT INTO menuItem VALUES (?, ?, ?)", [(i, k, f"￺{n}￻") for i, k, n in MENU_ITEMS]
        )
        conn.executemany("INSERT INTO category VALUES (?, ?, ?, ?)", CATEGORIES)
        conn.executemany("INSERT INTO sort VALUES (?, ?, ?, ?, ?)", SORTS)
        conn.executemany("INSERT INTO myTag VALUES (?, ?, ?, ?, ?)", MY_TAG_CATEGORIES)
        # dbVersion: rekordbox 7.2.19 writes '1000' (its own export, 2026-10-01); a rekordbox 6.8.6
        # export (acrilique/rekordlib test data) had '10000'. So it depends on the rekordbox
        # version: we match the newest seen. myTagMasterDBID identifies the rekordbox collection
        # the My Tags came from; we have none, so it's random.
        conn.execute(
            "INSERT INTO property VALUES (?, ?, ?, ?, 0, ?)",
            (
                pdb.device_name,
                DB_VERSION,
                len(pdb.tracks),
                pdb.export_date or date.today().isoformat(),
                random.randint(1, 0xFFFFFFFF),
            ),
        )
        conn.executemany("INSERT INTO artist VALUES (?, ?, NULL)", list(pdb.artists.items()))
        conn.executemany(
            "INSERT INTO album VALUES (?, ?, ?, NULL, 0, NULL)",
            [(i, n, artist or None) for i, (n, artist) in pdb.albums.items()],
        )
        conn.executemany("INSERT INTO genre VALUES (?, ?)", list(pdb.genres.items()))
        conn.executemany("INSERT INTO label VALUES (?, ?)", list(pdb.labels.items()))
        conn.executemany("INSERT INTO key VALUES (?, ?)", list(pdb.keys.items()))
        conn.executemany(
            "INSERT INTO content VALUES (?, ?, NULL, '', ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?, ?, ?, 0, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, 0, 1, '', ?, ?, ?, 41, 788224, 0, NULL, NULL, NULL)",
            [
                (
                    t.id, t.title, t.tempo, t.duration, t.track_number, t.disc_number, t.artist_id or None,
                    t.remixer_id or None, t.composer_id or None, t.album_id or None, t.genre_id, t.label_id or None, t.key_id,
                    t.color_id, t.comment, t.rating, t.year, t.release_date, t.date_added, t.date_added,
                    t.file_path, t.filename, t.file_size, t.file_type, t.bitrate, t.sample_depth, t.sample_rate,
                    t.play_count, t.master_db_id, t.master_content_id or t.id + 20, t.analyze_path,
                )
                for t in pdb.tracks
            ],
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO playlist VALUES (?, ?, ?, NULL, ?, ?)",
            [(n.id, n.order, n.name, int(n.is_folder), n.parent) for n in pdb.playlists],
        )
        conn.executemany(
            "INSERT INTO playlist_content VALUES (?, ?, ?)",
            [
                (n.id, t, i)
                for n in pdb.playlists
                if not n.is_folder
                for i, t in enumerate(n.track_ids, 1)
            ],
        )
        conn.commit()
        conn.execute("PRAGMA journal_mode = WAL")
        conn.close()
        for suffix in ("", "-wal", "-shm"):
            old = path.with_name(path.name + suffix)
            if old.exists():
                old.unlink()
        shutil.move(db_path, path)
        for suffix in ("-wal", "-shm"):
            leftover = db_path.with_name(db_path.name + suffix)
            if leftover.exists():
                os.replace(leftover, path.with_name(path.name + suffix))


def read_onelibrary(path: Path) -> Pdb:
    """The same shape :func:`cratemover.pioneer.pdb.read_pdb` returns, from a OneLibrary database."""
    from .pdb import PlaylistNode, TrackRow

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "exportLibrary.db"
        shutil.copyfile(path, copy)
        for suffix in ("-wal", "-shm"):
            if path.with_name(path.name + suffix).exists():
                shutil.copyfile(
                    path.with_name(path.name + suffix), copy.with_name(copy.name + suffix)
                )
        conn = open_encrypted(copy, ONE_LIBRARY)
        pdb = Pdb()
        pdb.artists = dict(conn.execute("SELECT artist_id, name FROM artist"))
        pdb.albums = {
            i: (n, a or 0)
            for i, n, a in conn.execute("SELECT album_id, name, artist_id FROM album")
        }
        pdb.genres = dict(conn.execute("SELECT genre_id, name FROM genre"))
        pdb.labels = dict(conn.execute("SELECT label_id, name FROM label"))
        pdb.keys = dict(conn.execute("SELECT key_id, name FROM key"))
        for row in conn.execute(
            """SELECT content_id, title, path, fileName, artist_id_artist, album_id, genre_id, label_id,
                      key_id, artist_id_remixer, artist_id_composer, samplingRate, bitDepth, bitrate,
                      fileSize, bpmx100, length, trackNo, djPlayCount, releaseYear, color_id, rating,
                      fileType, dateAdded, djComment, analysisDataFilePath FROM content"""
        ):
            (cid, title, file_path, filename, artist, album, genre, label, key, remixer, composer, rate, depth,
             kbps, size, tempo, length, track_no, plays, year, colour, rating, file_type, added, comment,
             analysis) = row  # fmt: skip
            pdb.tracks.append(
                TrackRow(
                    id=cid, title=title or "", file_path=file_path or "", filename=filename or "",
                    artist_id=artist or 0, album_id=album or 0, genre_id=genre or 0, label_id=label or 0,
                    key_id=key or 0, remixer_id=remixer or 0, composer_id=composer or 0,
                    sample_rate=rate or 0, sample_depth=depth or 0, bitrate=kbps or 0, file_size=size or 0,
                    tempo=tempo or 0, duration=length or 0, track_number=track_no or 0,
                    play_count=plays or 0, year=year or 0, color_id=colour or 0, rating=rating or 0,
                    file_type=file_type or 0, date_added=added or "", comment=comment or "",
                    analyze_path=analysis or "",
                )
            )  # fmt: skip
        nodes = {
            pid: PlaylistNode(pid, parent or 0, seq or 0, name or "", bool(attr))
            for pid, seq, name, attr, parent in conn.execute(
                "SELECT playlist_id, sequenceNo, name, attribute, playlist_id_parent FROM playlist"
            )
        }
        for pid, cid in conn.execute(
            "SELECT playlist_id, content_id FROM playlist_content ORDER BY playlist_id, sequenceNo"
        ):
            if pid in nodes:
                nodes[pid].track_ids.append(cid)
        pdb.playlists = sorted(nodes.values(), key=lambda n: (n.parent, n.order))
        conn.close()
    return pdb
