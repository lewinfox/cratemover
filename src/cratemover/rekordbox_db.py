"""Read Rekordbox 6/7's own library: ``master.db`` plus its ANLZ analysis files.

``master.db`` is an SQLCipher-encrypted SQLite database with a publicly known
key (see :mod:`cratemover.pioneer.keys`); opening it needs the ``rekordbox``
extra (sqlcipher3). Tracks, cues and playlists are in the database; beat grids
are only in the analysis files (``share/PIONEER/USBANLZ/…/ANLZ0000.DAT``, the
``PQTZ`` tag), so point this at the whole Rekordbox folder:

* macOS: ``~/Library/Pioneer/rekordbox``
* Windows: ``%APPDATA%\\Pioneer\\rekordbox``

Table layouts: pyrekordbox ``masterdb/tables.py``.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from .colours import REKORDBOX_CUE_COLOURS, REKORDBOX_TRACK_COLOURS
from .keys import parse_key
from .model import Cue, CueRole, Library, Playlist, TempoMarker, Track
from .pioneer import anlz
from .pioneer.keys import MASTER_DB, open_encrypted

# djmdCue.Kind: 0 is a memory cue; hot cues A-H skip 4 (pyrekordbox docs / observed exports).
_HOT_CUE_KIND = {1: 0, 2: 1, 3: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7}
_TRACK_COLOURS = list(REKORDBOX_TRACK_COLOURS)  # djmdColor IDs 1-8 in this order
PLAYLIST, FOLDER, SMART = 0, 1, 4  # djmdPlaylist.Attribute


def find_master_db(path: Path) -> Path:
    if path.is_file():
        return path
    for candidate in (path / "master.db", path / "rekordbox" / "master.db"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no Rekordbox master.db at {path}")


def _connect(db_path: Path, workdir: Path) -> Any:
    """Open a private copy (Rekordbox may be running; the mount may be read-only)."""
    copy = workdir / "master.db"
    shutil.copyfile(db_path, copy)
    with open(copy, "rb") as f:
        plain = f.read(16) == b"SQLite format 3\x00"
    if plain:
        return sqlite3.connect(copy)
    return open_encrypted(copy, MASTER_DB)


def _columns(conn: Any, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def grid_from_anlz(path: Path) -> list[TempoMarker]:
    """Tempo sections from an analysis file's PQTZ beat grid."""
    return anlz.read_grid(anlz.AnlzFile.parse(path.read_bytes()))


def read_rekordbox_db(
    path: Path, progress: Callable[[str], None] = print, read_grids: bool = True
) -> Library:
    db_path = find_master_db(path)
    share = db_path.parent / "share"
    library = Library(source=f"Rekordbox library at {db_path}")
    with tempfile.TemporaryDirectory() as tmp:
        conn = _connect(db_path, Path(tmp))
        deleted = (
            "AND c.rb_local_deleted = 0"
            if "rb_local_deleted" in _columns(conn, "djmdContent")
            else ""
        )
        rows = conn.execute(
            f"""SELECT c.ID, c.FolderPath, c.Title, a.Name, al.Name, g.Name, k.ScaleName, c.BPM,
                       c.Length, c.TrackNo, c.BitRate, c.Commnt, c.Rating, c.ReleaseYear, l.Name,
                       r.Name, cm.Name, c.ColorID, c.DJPlayCount, c.DateCreated, c.SampleRate,
                       c.FileSize, c.AnalysisDataPath
                FROM djmdContent c
                LEFT JOIN djmdArtist a ON a.ID = c.ArtistID
                LEFT JOIN djmdAlbum al ON al.ID = c.AlbumID
                LEFT JOIN djmdGenre g ON g.ID = c.GenreID
                LEFT JOIN djmdKey k ON k.ID = c.KeyID
                LEFT JOIN djmdLabel l ON l.ID = c.LabelID
                LEFT JOIN djmdArtist r ON r.ID = c.RemixerID
                LEFT JOIN djmdArtist cm ON cm.ID = c.ComposerID
                WHERE c.FolderPath IS NOT NULL {deleted}"""
        ).fetchall()
        anlz_paths: dict[str, Path] = {}
        for row in rows:
            (cid, folder_path, title, artist, album, genre, key, bpm, length, track_no, bitrate,
             comment, rating, year, label, remixer, composer, colour_id, plays, created,
             sample_rate, size, analysis) = row  # fmt: skip
            colour_index = int(colour_id or 0)
            track = library.add_track(
                Track(
                    id=f"rbdb:{cid}",
                    location=str(folder_path),
                    title=title or "",
                    artist=artist or "",
                    album=album or "",
                    genre=genre or "",
                    composer=composer or "",
                    comment=comment or "",
                    label=label or "",
                    remixer=remixer or "",
                    year=str(year) if year else "",
                    track_number=int(track_no) if track_no else None,
                    duration_s=float(length or 0),
                    sample_rate=int(sample_rate or 0),
                    bitrate=int(bitrate or 0),
                    file_size=int(size or 0),
                    bpm=(bpm or 0) / 100.0,
                    key=parse_key(key),
                    rating=max(0, min(5, int(rating or 0))),
                    colour=_TRACK_COLOURS[colour_index - 1] if 1 <= colour_index <= 8 else None,
                    play_count=int(plays or 0),
                    date_added=_date(created),
                )
            )
            if analysis:
                anlz_paths[track.id] = share / str(analysis).strip("/\\")

        cue_deleted = (
            "WHERE rb_local_deleted = 0" if "rb_local_deleted" in _columns(conn, "djmdCue") else ""
        )
        for cid, start, end, kind, colour_index, comment in conn.execute(
            f"SELECT ContentID, InMsec, OutMsec, Kind, ColorTableIndex, Comment FROM djmdCue {cue_deleted}"
        ):
            track = library.tracks.get(f"rbdb:{cid}")
            if track is None or start is None or start < 0:
                continue
            loop = end is not None and end > start
            index = int(colour_index or 0)
            slot = _HOT_CUE_KIND.get(int(kind or 0)) if kind else None
            track.cues.append(
                Cue(
                    CueRole.LOOP if loop else CueRole.CUE,
                    float(start),
                    float(end) if loop else None,
                    slot=slot,
                    name=comment or "",
                    colour=REKORDBOX_CUE_COLOURS[index - 1]
                    if slot is not None and 1 <= index <= 16
                    else None,
                )
            )

        playlists = conn.execute(
            "SELECT ID, Name, Attribute, ParentID, Seq FROM djmdPlaylist ORDER BY Seq"
        ).fetchall()
        entries: dict[str, list[str]] = {}
        for pid, cid in conn.execute(
            "SELECT PlaylistID, ContentID FROM djmdSongPlaylist ORDER BY PlaylistID, TrackNo"
        ):
            if f"rbdb:{cid}" in library.tracks:
                entries.setdefault(str(pid), []).append(f"rbdb:{cid}")
        conn.close()

    nodes: dict[str, Playlist] = {}
    smart = 0
    for pid, name, attribute, _parent, _seq in playlists:
        if attribute == SMART:
            smart += 1
            continue
        if attribute not in (PLAYLIST, FOLDER):
            continue  # internal lists, e.g. cloud sync (-128)
        nodes[str(pid)] = (
            Playlist.folder(name or "")
            if attribute == FOLDER
            else Playlist(name or "", entries.get(str(pid), []))
        )
    for pid, _name, _attribute, parent, _seq in playlists:
        node = nodes.get(str(pid))
        if node is None:
            continue
        nodes.get(str(parent), library.playlists).children.append(node)
    if smart:
        library.warnings.append(f"{smart} intelligent playlist(s) not converted.")

    if read_grids:
        missing = 0
        for n, (track_id, anlz_path) in enumerate(anlz_paths.items(), 1):
            if n % 500 == 0:
                progress(f"Reading beat grids {n}/{len(anlz_paths)}")
            try:
                library.tracks[track_id].grid = grid_from_anlz(anlz_path)
                library.tracks[track_id].extra["anlz"] = str(anlz_path)
            except (OSError, ValueError, IndexError):
                missing += 1
        if missing:
            library.warnings.append(
                f"{missing} track(s) have no readable analysis file under {share}, so no beat grid."
            )
    return library
