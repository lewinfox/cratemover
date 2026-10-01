"""Read and write a Mixxx library (``mixxxdb.sqlite``).

Reading takes a snapshot of the database, so Mixxx can stay open. Writing never
touches the source: it builds a new database from Mixxx's own schema
(``res/schema.xml``, vendored as ``mixxx_schema.xml``) or copies an existing
one and merges into the copy.

Positions: Mixxx stores cues in samples of its stereo engine output (frames *
2) and beats in frames, both on its own decoding timeline, which differs from
the reference timeline by :func:`cratemover.offsets.mixxx_offset_ms`.
"""

from __future__ import annotations

import shutil
import sqlite3
import struct
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path

from . import grid as gridlib
from .audiofile import probe
from .colours import MIXXX_CUE_COLOURS
from .keys import KeyNotation, format_key, key_from_mixxx_id, mixxx_key_id, parse_key
from .model import Cue, CueRole, Library, Playlist, TempoMarker, Track, path_name
from .offsets import Mp3Decoder, mixxx_offset_ms

Resolver = Callable[[str], Path | None]

# mixxx::CueType
HOT_CUE, MAIN_CUE, LOOP, INTRO, OUTRO = 1, 2, 4, 6, 7
ENGINE_CHANNELS = 2  # cue positions count samples of stereo output
MAX_HOT_CUES = 36
DEFAULT_RATE = 44100


class MixxxError(RuntimeError):
    pass


def find_database(path: Path) -> Path:
    if path.is_dir():
        path = path / "mixxxdb.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"no Mixxx database at {path}")
    return path


def _snapshot(db_path: Path) -> sqlite3.Connection:
    memory = sqlite3.connect(":memory:")
    errors = []
    for params in ("mode=ro", "mode=ro&immutable=1"):
        try:
            source = sqlite3.connect(f"{db_path.resolve().as_uri()}?{params}", uri=True)
            try:
                source.backup(memory)
                return memory
            finally:
                source.close()
        except sqlite3.Error as exc:
            errors.append(str(exc))
    raise MixxxError(f"could not read {db_path}: {'; '.join(errors)}")


# --- protobuf beats (src/proto/beats.proto) -----------------------------------------


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _fields(data: bytes) -> dict[int, list[int | bytes]]:
    fields: dict[int, list[int | bytes]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        value: int | bytes
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        elif wire == 2:
            length, pos = _varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _beat_frame(data: bytes) -> tuple[int, bool]:
    fields = _fields(data)
    frame = fields.get(1, [0])[-1]
    enabled = fields.get(2, [1])[-1]
    assert isinstance(frame, int) and isinstance(enabled, int)
    return (frame - (1 << 64) if frame >= 1 << 63 else frame), bool(enabled)


def decode_beats(blob: bytes, version: str) -> tuple[float, int] | list[int] | None:
    """(bpm, first beat frame) for a grid, a list of frames for a beat map, or None."""
    if version.startswith("BeatGrid"):
        try:
            fields = _fields(blob)
            bpm_field = fields.get(1)
            bpm = None
            if bpm_field and isinstance(bpm_field[-1], bytes):
                raw = _fields(bpm_field[-1]).get(1)
                if raw and isinstance(raw[-1], bytes):
                    (bpm,) = struct.unpack("<d", raw[-1])
            first = fields.get(2)
            frame = _beat_frame(first[-1])[0] if first and isinstance(first[-1], bytes) else 0
        except (ValueError, IndexError, struct.error):
            if len(blob) != 16:
                return None
            bpm, first_frame = struct.unpack("<dd", blob)  # BeatGrid-1.0 raw struct
            frame = int(first_frame)
        return (bpm, frame) if bpm and bpm > 0 else None
    if version.startswith("BeatMap"):
        frames = []
        for raw in _fields(blob).get(1, []):
            if isinstance(raw, bytes):
                frame, enabled = _beat_frame(raw)
                if enabled and frame >= 0:
                    frames.append(frame)
        return sorted(frames) or None
    return None


def _pb_varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _pb_bytes(number: int, payload: bytes) -> bytes:
    return _pb_varint(number << 3 | 2) + _pb_varint(len(payload)) + payload


def encode_beatgrid(bpm: float, first_frame: int) -> bytes:
    bpm_msg = _pb_varint(1 << 3 | 1) + struct.pack("<d", bpm)
    beat_msg = _pb_varint(1 << 3) + _pb_varint(first_frame)
    return _pb_bytes(1, bpm_msg) + _pb_bytes(2, beat_msg)


def encode_beatmap(frames: list[int]) -> bytes:
    return b"".join(_pb_bytes(1, _pb_varint(1 << 3) + _pb_varint(f)) for f in frames)


# --- reading -------------------------------------------------------------------------


@dataclass
class MixxxReadOptions:
    mp3_decoder: Mp3Decoder = "MAD"
    include_hidden_playlists: bool = False


def _date(value: object) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _cues(rows: list[tuple], rate: int, offset_ms: float) -> list[Cue]:  # type: ignore[type-arg]
    per_ms = rate * ENGINE_CHANNELS / 1000.0
    cues: list[Cue] = []
    for cue_type, hotcue, position, length, colour, label in rows:
        start = position / per_ms + offset_ms if position is not None and position >= 0 else None
        length_ms = max(0.0, (length or 0) / per_ms)
        label = (label or "").rstrip("\x00")
        colour = int(colour) & 0xFFFFFF if colour is not None else None
        slot = int(hotcue) if hotcue is not None and hotcue >= 0 else None
        if cue_type == HOT_CUE and start is not None:
            cues.append(Cue(CueRole.CUE, start, slot=slot, name=label, colour=colour))
        elif cue_type == LOOP and start is not None and length_ms > 0:
            cues.append(Cue(CueRole.LOOP, start, start + length_ms, slot, label, colour))
        elif cue_type == MAIN_CUE and start is not None:
            cues.append(Cue(CueRole.MAIN, start, name=label))
        elif cue_type in (INTRO, OUTRO) and (start is not None or length_ms > 0):
            role = CueRole.INTRO if cue_type == INTRO else CueRole.OUTRO
            if start is None:  # only the end is set: "length" is then the end position
                cues.append(
                    Cue(role, length_ms + offset_ms, name=label or f"{role.value.title()} end")
                )
            else:
                cues.append(Cue(role, start, start + length_ms if length_ms else None, name=label))
    return cues


def read_mixxx(
    path: Path,
    options: MixxxReadOptions,
    resolve: Resolver,
    progress: Callable[[str], None] = print,
) -> Library:
    db_path = find_database(path)
    conn = _snapshot(db_path)
    library = Library(source=f"Mixxx library at {db_path}")
    rows = conn.execute(
        """SELECT l.id, tl.location, l.title, l.artist, l.album, l.album_artist, l.genre,
                  l.composer, l.grouping, l.comment, l.year, l.tracknumber, l.duration,
                  l.samplerate, l.bitrate, tl.filesize, l.bpm, l.bpm_lock, l.key_id, l.key,
                  l.rating, l.color, l.timesplayed, l.datetime_added, l.beats, l.beats_version
           FROM library l JOIN track_locations tl ON tl.id = l.location
           WHERE l.mixxx_deleted = 0"""
    ).fetchall()
    cue_rows: dict[int, list[tuple]] = {}  # type: ignore[type-arg]
    for row in conn.execute(
        "SELECT track_id, type, hotcue, position, length, color, label FROM cues ORDER BY position"
    ):
        cue_rows.setdefault(row[0], []).append(row[1:])

    offset_warnings = 0
    for n, row in enumerate(rows, 1):
        if n % 500 == 0:
            progress(f"Reading Mixxx tracks {n}/{len(rows)}")
        (tid, location, title, artist, album, album_artist, genre, composer, grouping, comment,
         year, tracknumber, duration, rate, bitrate, size, bpm, bpm_lock, key_id, key_text,
         rating, colour, played, added, beats_blob, beats_version) = row  # fmt: skip
        rate = int(rate or 0) or DEFAULT_RATE
        track = Track(
            id=f"mixxx:{tid}",
            location=str(location),
            title=title or "",
            artist=artist or "",
            album=album or "",
            album_artist=album_artist or "",
            genre=genre or "",
            composer=composer or "",
            grouping=grouping or "",
            comment=comment or "",
            year=year or "",
            track_number=int(str(tracknumber)) if str(tracknumber or "").isdigit() else None,
            duration_s=float(duration or 0),
            sample_rate=rate,
            bitrate=int(bitrate or 0),
            file_size=int(size or 0),
            bpm=float(bpm or 0),
            bpm_locked=bool(bpm_lock),
            key=key_from_mixxx_id(key_id) or parse_key(key_text),
            rating=int(rating or 0),
            colour=int(colour) & 0xFFFFFF if colour is not None else None,
            play_count=int(played or 0),
            date_added=_date(added),
        )
        offset = mixxx_offset_ms(resolve(track.location), track.extension, options.mp3_decoder)
        if offset.warning:
            offset_warnings += 1
        track.cues = _cues(cue_rows.get(tid, []), rate, offset.ms)
        if beats_blob and beats_version:
            try:
                beats = decode_beats(bytes(beats_blob), str(beats_version))
            except (ValueError, IndexError):
                beats = None
            if isinstance(beats, tuple):
                track.grid = [TempoMarker(beats[1] * 1000.0 / rate + offset.ms, beats[0], 1)]
            elif isinstance(beats, list):
                positions = [f * 1000.0 / rate + offset.ms for f in beats]
                track.grid = gridlib.sections_from_beats(positions)
        library.add_track(track)
    if offset_warnings:
        library.warnings.append(
            f"{offset_warnings} MP3/AAC file(s) could not be inspected for decoder offsets; "
            "their cues may be ~26-50 ms off."
        )

    hidden = "" if options.include_hidden_playlists else "WHERE p.hidden = 0"
    for pid, name in conn.execute(
        f"SELECT p.id, p.name FROM Playlists p {hidden} ORDER BY p.position, p.name"
    ).fetchall():
        ids = [
            f"mixxx:{r[0]}"
            for r in conn.execute(
                "SELECT track_id FROM PlaylistTracks WHERE playlist_id = ? ORDER BY position",
                (pid,),
            )
        ]
        library.playlists.children.append(Playlist(name, [i for i in ids if i in library.tracks]))
    crates = Playlist.folder("Crates")
    for cid, name in conn.execute("SELECT id, name FROM crates ORDER BY name COLLATE NOCASE"):
        ids = [
            f"mixxx:{r[0]}"
            for r in conn.execute(
                """SELECT ct.track_id FROM crate_tracks ct JOIN library l ON l.id = ct.track_id
                   WHERE ct.crate_id = ? ORDER BY l.artist COLLATE NOCASE, l.title COLLATE NOCASE""",
                (cid,),
            )
        ]
        crates.children.append(
            Playlist(name, [i for i in ids if i in library.tracks], is_crate=True)
        )
    if crates.children:
        library.playlists.children.append(crates)
    conn.close()
    return library


# --- writing -------------------------------------------------------------------------


@dataclass
class MixxxWriteOptions:
    mp3_decoder: Mp3Decoder = "MAD"
    base_database: Path | None = None  # merge into a copy of this mixxxdb.sqlite
    playlists_as_crates: bool = False  # write every playlist as a crate
    overwrite_existing: bool = True  # replace cues/grid of tracks already in the base database
    memory_cues_to_hot_cues: bool = True
    key_notation: KeyNotation = KeyNotation.MUSICAL
    replace_playlists: bool = False  # reuse same-named playlists/crates instead of adding "(2)"


@dataclass
class MixxxWriteResult:
    files: list[Path] = field(default_factory=list)
    added: int = 0
    updated: int = 0
    warnings: list[str] = field(default_factory=list)


def create_database(path: Path) -> sqlite3.Connection:
    """A new, empty Mixxx database at the current schema version."""
    schema = resources.files("cratemover").joinpath("mixxx_schema.xml").read_text(encoding="utf-8")
    root = ET.fromstring(schema)
    db = sqlite3.connect(path)
    latest, min_compatible = 0, 0
    for revision in root.iter("revision"):
        sql = revision.find("sql")
        if sql is not None and sql.text:
            db.executescript(sql.text)
        latest = int(revision.get("version", "0"))
        min_compatible = int(revision.get("min_compatible", "0"))
    db.executemany(
        "INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)",
        [
            ("mixxx.schema.version", str(latest)),
            ("mixxx.schema.last_used_version", str(latest)),
            ("mixxx.schema.min_compatible_version", str(min_compatible)),
        ],
    )
    db.commit()
    return db


def _mixxx_cues(
    track: Track, options: MixxxWriteOptions
) -> list[tuple[int, int, float, float, int, str]]:
    """(type, hotcue, start ms, length ms, colour, label) on the reference timeline."""
    out: list[tuple[int, int, float, float, int, str]] = []
    used = {c.slot for c in track.cues if c.slot is not None and c.slot < MAX_HOT_CUES}
    free = iter(s for s in range(8, MAX_HOT_CUES) if s not in used)
    has_main = False

    def colour(cue: Cue, slot: int) -> int:
        return cue.colour if cue.colour is not None else MIXXX_CUE_COLOURS[slot % 8]

    placed: set[int] = set()
    for cue in sorted(track.cues, key=lambda c: (c.slot is None, c.slot or 0, c.position_ms)):
        length = (cue.end_ms - cue.position_ms) if cue.end_ms is not None else 0.0
        slot = cue.slot if cue.slot is not None and cue.slot < MAX_HOT_CUES else None
        if slot is not None and slot in placed:
            slot = None
        if cue.role is CueRole.MAIN and not has_main:
            has_main = True
            out.append((MAIN_CUE, -1, cue.position_ms, 0.0, 0, cue.name))
            continue
        if cue.role in (CueRole.INTRO, CueRole.OUTRO) and slot is None:
            kind = INTRO if cue.role is CueRole.INTRO else OUTRO
            out.append((kind, -1, cue.position_ms, max(0.0, length), 0, cue.name))
            continue
        if (
            slot is None
            and not cue.is_loop
            and any(
                c.slot is not None and not c.is_loop and abs(c.position_ms - cue.position_ms) < 5
                for c in track.cues
            )
        ):
            continue  # a memory cue that duplicates a hot cue
        if slot is None and (options.memory_cues_to_hot_cues or cue.is_loop):
            slot = next(free, None)
        if slot is None:
            continue
        placed.add(slot)
        if cue.is_loop and length > 0:
            out.append((LOOP, slot, cue.position_ms, length, colour(cue, slot), cue.name))
        else:
            out.append((HOT_CUE, slot, cue.position_ms, 0.0, colour(cue, slot), cue.name))
    return out


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate.lower() in taken:
        candidate = f"{name} ({n})"
        n += 1
    taken.add(candidate.lower())
    return candidate


def write_mixxx(
    library: Library,
    out_dir: Path,
    options: MixxxWriteOptions,
    resolve: Resolver,
    progress: Callable[[str], None] = print,
) -> MixxxWriteResult:
    result = MixxxWriteResult()
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "mixxxdb.sqlite"
    db_path.unlink(missing_ok=True)
    if options.base_database:
        shutil.copyfile(find_database(options.base_database), db_path)
        db = sqlite3.connect(db_path)
    else:
        db = create_database(db_path)

    track_ids: dict[str, int] = {}
    unknown_rate = 0
    total = len(library.tracks)
    for n, track in enumerate(library.tracks.values(), 1):
        if n % 200 == 0:
            progress(f"Writing Mixxx tracks {n}/{total}")
        local = resolve(track.location)
        info = probe(local) if local else None
        rate = (info.sample_rate if info else 0) or track.sample_rate
        if not rate:
            rate, unknown_rate = DEFAULT_RATE, unknown_rate + 1
        duration = (info.duration_s if info else 0) or track.duration_s
        offset = mixxx_offset_ms(local, track.extension, options.mp3_decoder).ms
        location = track.location
        existing = db.execute(
            """SELECT l.id FROM library l JOIN track_locations tl ON tl.id = l.location
               WHERE tl.location = ?""",
            (location,),
        ).fetchone()
        if existing and not options.overwrite_existing:
            track_ids[track.id] = existing[0]
            continue

        grid = gridlib.shift(gridlib.simplify(track.grid), -offset)
        beats, beats_version = None, None
        if len(grid) == 1:
            first_ms = gridlib.first_downbeat_ms(grid[0])
            beats = encode_beatgrid(grid[0].bpm, round(first_ms * rate / 1000))
            beats_version = "BeatGrid-2.0"
        elif grid:
            end = (duration * 1000) if duration else grid[-1].position_ms + 600_000
            frames = [round(p * rate / 1000) for p, _ in gridlib.beat_positions(grid, end)]
            beats, beats_version = encode_beatmap(frames), "BeatMap-1.0"

        size = info.file_size if info else track.file_size
        values = {
            "artist": track.artist,
            "title": track.title,
            "album": track.album,
            "album_artist": track.album_artist,
            "year": track.year,
            "genre": track.genre,
            "tracknumber": str(track.track_number) if track.track_number else "",
            "comment": track.comment,
            "composer": track.composer,
            "grouping": track.grouping,
            "duration": duration,
            "bitrate": (info.bitrate if info else 0) or track.bitrate,
            "samplerate": rate,
            "channels": (info.channels if info else 0) or 2,
            "bpm": track.bpm or (grid[0].bpm if grid else 0),
            "bpm_lock": int(track.bpm_locked),
            "filetype": track.extension,
            "timesplayed": track.play_count,
            "played": int(track.play_count > 0),
            "rating": track.rating,
            "key": format_key(track.key, options.key_notation),
            "key_id": mixxx_key_id(track.key) or 0,
            "color": track.colour,
            "beats": beats,
            "beats_version": beats_version,
            "beats_sub_version": "",
            "mixxx_deleted": 0,
            "header_parsed": 1,
        }
        if track.date_added:
            values["datetime_added"] = f"{track.date_added.isoformat()}T00:00:00.000Z"
        if existing:
            tid = int(existing[0])
            sets = ", ".join(f"{k} = ?" for k in values)
            db.execute(f"UPDATE library SET {sets} WHERE id = ?", (*values.values(), tid))
            db.execute("DELETE FROM cues WHERE track_id = ?", (tid,))
            result.updated += 1
        else:
            directory = location[: -len(path_name(location)) - 1] or "/"
            loc_id = db.execute(
                """INSERT INTO track_locations (location, filename, directory, filesize,
                       fs_deleted, needs_verification) VALUES (?, ?, ?, ?, 0, 0)""",
                (location, path_name(location), directory, size),
            ).lastrowid
            values["location"] = loc_id
            columns = ", ".join(values)
            marks = ", ".join("?" * len(values))
            tid = int(
                db.execute(
                    f"INSERT INTO library ({columns}) VALUES ({marks})", tuple(values.values())
                ).lastrowid
                or 0
            )
            result.added += 1
        track_ids[track.id] = tid
        per_ms = rate * ENGINE_CHANNELS / 1000.0
        for kind, hotcue, start, length, colour, label in _mixxx_cues(track, options):
            position = round((start - offset) * per_ms / 2) * 2
            db.execute(
                """INSERT INTO cues (track_id, type, position, length, hotcue, label, color)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    kind,
                    max(0, position),
                    round(length * per_ms / 2) * 2,
                    hotcue,
                    label,
                    colour,
                ),
            )

    playlist_names = {r[0].lower() for r in db.execute("SELECT name FROM Playlists")}
    crate_names = {r[0].lower() for r in db.execute("SELECT name FROM crates")}
    position = int(db.execute("SELECT COALESCE(MAX(position), 0) FROM Playlists").fetchone()[0])
    now = datetime.now().isoformat(timespec="seconds")
    for parents, playlist in library.playlists.walk():
        # Mixxx has no folders: keep the path in the name.
        parents = tuple(p for p in parents if p != "Crates" or not playlist.is_crate)
        name = " / ".join((*parents, playlist.name))
        ids = [track_ids[t] for t in playlist.track_ids or [] if t in track_ids]
        if playlist.is_crate or options.playlists_as_crates:
            row = db.execute("SELECT id FROM crates WHERE name = ?", (name,)).fetchone()
            if row and options.replace_playlists:
                cid = row[0]
                db.execute("DELETE FROM crate_tracks WHERE crate_id = ?", (cid,))
            else:
                cid = db.execute(
                    "INSERT INTO crates (name) VALUES (?)", (_unique(name, crate_names),)
                ).lastrowid
            db.executemany(
                "INSERT OR IGNORE INTO crate_tracks (crate_id, track_id) VALUES (?, ?)",
                [(cid, t) for t in ids],
            )
        else:
            row = db.execute(
                "SELECT id FROM Playlists WHERE name = ? AND hidden = 0", (name,)
            ).fetchone()
            if row and options.replace_playlists:
                pid = row[0]
                db.execute("DELETE FROM PlaylistTracks WHERE playlist_id = ?", (pid,))
                db.execute("UPDATE Playlists SET date_modified = ? WHERE id = ?", (now, pid))
            else:
                position += 1
                pid = db.execute(
                    """INSERT INTO Playlists (name, position, hidden, date_created, date_modified)
                       VALUES (?, ?, 0, ?, ?)""",
                    (_unique(name, playlist_names), position, now, now),
                ).lastrowid
            db.executemany(
                "INSERT INTO PlaylistTracks (playlist_id, track_id, position, pl_datetime_added) "
                "VALUES (?, ?, ?, ?)",
                [(pid, t, i, now) for i, t in enumerate(ids, 1)],
            )
    db.commit()
    db.close()
    result.files.append(db_path)
    if unknown_rate:
        result.warnings.append(
            f"{unknown_rate} track(s) had an unknown sample rate (file not found); assumed "
            f"{DEFAULT_RATE} Hz, so their cues will be off if that is wrong."
        )
    return result
