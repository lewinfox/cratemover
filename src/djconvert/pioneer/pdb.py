"""Read and write ``export.pdb``, the DeviceSQL database on Rekordbox USB sticks.

The file is an array of 4096-byte pages. Page 0 is a directory of 20 tables,
each a chain of pages: an index page first, then data pages holding rows in a
heap that grows up from 0x28, with row offsets in groups of 16 growing down
from the page end.

Reading follows Deep Symmetry's analysis and fragmede/rekordbox-pdb (MIT).
Writing ports the fresh-export rules of M-Igashi/baken ``crates/baken-export``
(MIT), which were compared byte for byte against Rekordbox 7 exports and read
by a CDJ-2000NXS2. See ``mixxx-to-rekordbox/usb-analysis-plan.md`` in this
repo for the evidence behind each constant.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field

PAGE = 4096
HEAP = 0x28
GROUP = 0x24  # 16 u16 offsets + presence flags + transaction flags
NONE = 0x03FFFFFF
NUM_TABLES = 20

TRACKS, GENRES, ARTISTS, ALBUMS, LABELS, KEYS, COLORS = 0, 1, 2, 3, 4, 5, 6
PLAYLIST_TREE, PLAYLIST_ENTRIES, ARTWORK, COLUMNS, HISTORY = 7, 8, 13, 16, 19

# Track file types (must match the codec, or players hang on load).
FILE_TYPES = {
    "mp3": 1,
    "m4a": 4,
    "mp4": 4,
    "aac": 4,
    "flac": 5,
    "wav": 0x0B,
    "aif": 0x0C,
    "aiff": 0x0C,
}
FILE_TYPE_ALAC = 6


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _align4(n: int) -> int:
    return (n + 3) & ~3


# --- DeviceSQL strings -------------------------------------------------------------


def encode_string(text: str) -> bytes:
    """Short ASCII for ASCII under 127 chars, else UTF-16LE (Rekordbox 7 never writes 0x40)."""
    if text.isascii() and len(text) < 0x7F:
        return bytes([((len(text) + 1) << 1) | 1]) + text.encode("ascii")
    data = text.encode("utf-16-le")
    return bytes([0x90]) + struct.pack("<H", len(data) + 4) + b"\x00" + data


def decode_string(b: bytes, o: int) -> str:
    flag = b[o]
    if flag & 1:
        return b[o + 1 : o + (flag >> 1)].decode("ascii", errors="replace")
    total = _u16(b, o + 1)
    body = b[o + 4 : o + total]
    if flag == 0x90:
        return body.decode("utf-16-le", errors="replace")
    return body.decode("latin-1")  # 0x40 long ASCII


def _string_at(at: int, encoded: bytes) -> int:
    """UTF-16 strings start on a 4-byte boundary inside a row (NXS2 froze otherwise)."""
    return _align4(at) if encoded[0] == 0x90 else at


# --- rows --------------------------------------------------------------------------


@dataclass
class TrackRow:
    id: int
    title: str = ""
    file_path: str = ""  # on the stick, e.g. /Contents/Artist/Album/x.mp3
    filename: str = ""
    artist_id: int = 0
    album_id: int = 0
    genre_id: int = 0
    label_id: int = 0
    key_id: int = 0
    remixer_id: int = 0
    composer_id: int = 0
    original_artist_id: int = 0
    artwork_id: int = 0
    sample_rate: int = 44100
    sample_depth: int = 16
    bitrate: int = 0  # kbps
    file_size: int = 0
    tempo: int = 0  # BPM x 100
    duration: int = 0  # seconds
    track_number: int = 0
    disc_number: int = 0
    play_count: int = 0
    year: int = 0
    color_id: int = 0
    rating: int = 0  # stars
    file_type: int = 1
    date_added: str = ""
    release_date: str = ""
    mix_name: str = ""
    analyze_path: str = ""
    analyze_date: str = ""
    comment: str = ""
    isrc: str = ""
    master_db_id: int = 0xEA7615E6  # any constant works (baken, vynull)
    master_content_id: int = 0  # 0 froze a CDJ (vynull); defaults to id + 20

    def encode(self, slot: int) -> bytes:
        head = struct.pack(
            "<HHIIIIIIIIIIIIIIIIII",
            0x24, slot * 0x20, 0xC0700, self.sample_rate, self.composer_id, self.file_size,
            self.master_content_id or self.id + 20, self.master_db_id, self.artwork_id,
            self.key_id, self.original_artist_id, self.label_id, self.remixer_id,
            self.bitrate, self.track_number, self.tempo, self.genre_id, self.album_id,
            self.artist_id, self.id,
        )  # fmt: skip
        head += struct.pack(
            "<HHHHHHBBHH",
            self.disc_number, self.play_count, self.year, self.sample_depth,
            min(self.duration, 0xFFFF), 41, self.color_id, self.rating, self.file_type, 3,
        )  # fmt: skip
        assert len(head) == 0x5E
        strings = [
            self.isrc, "", "1", "1", "1", "", "ON", "ON", "", "",
            self.date_added, self.release_date, self.mix_name, "", self.analyze_path,
            self.analyze_date, self.comment, self.title, "", self.filename, self.file_path,
        ]  # fmt: skip
        encoded = [encode_string(s) for s in strings]
        offsets, at = [], 0x88
        for e in encoded:
            at = _string_at(at, e)
            offsets.append(at)
            at += len(e)
        row = bytearray(head + struct.pack("<21H", *offsets))
        for e, off in zip(encoded, offsets, strict=True):
            row.extend(bytes(off - len(row)))
            row.extend(e)
        alloc = 0x88 + sum(_align4(len(e)) for e in encoded) + 4
        return bytes(row.ljust(alloc, b"\x00"))

    @classmethod
    def decode(cls, b: bytes, o: int) -> TrackRow:
        (_, _, _, sample_rate, composer_id, file_size, content_id, db_id, artwork_id, key_id,
         original_artist_id, label_id, remixer_id, bitrate, track_number, tempo, genre_id,
         album_id, artist_id, track_id) = struct.unpack_from("<HHIIIIIIIIIIIIIIIIII", b, o)  # fmt: skip
        (disc, plays, year, depth, duration, _, color_id, rating, file_type, _) = (
            struct.unpack_from("<HHHHHHBBHH", b, o + 0x4C)
        )
        offsets = struct.unpack_from("<21H", b, o + 0x5E)
        s = [decode_string(b, o + off) for off in offsets]
        return cls(
            id=track_id, title=s[17], file_path=s[20], filename=s[19], artist_id=artist_id,
            album_id=album_id, genre_id=genre_id, label_id=label_id, key_id=key_id,
            remixer_id=remixer_id, composer_id=composer_id, original_artist_id=original_artist_id,
            artwork_id=artwork_id, sample_rate=sample_rate, sample_depth=depth, bitrate=bitrate,
            file_size=file_size, tempo=tempo, duration=duration, track_number=track_number,
            disc_number=disc, play_count=plays, year=year, color_id=color_id, rating=rating,
            file_type=file_type, date_added=s[10], release_date=s[11], mix_name=s[12],
            analyze_path=s[14], analyze_date=s[15], comment=s[16], isrc=s[0],
            master_db_id=db_id, master_content_id=content_id,
        )  # fmt: skip


def _named(id_: int, name: str) -> bytes:  # genres, labels
    return struct.pack("<I", id_) + encode_string(name)


def _with_name(head: bytes, name: str) -> bytes:
    """The ``0x03, name_offset, name`` tail of artist and album rows."""
    encoded = encode_string(name)
    header = len(head) + 2
    at = _string_at(header, encoded)
    row = head + bytes([0x03, at]) + bytes(at - header) + encoded
    return row.ljust(_align4(header) + _align4(len(encoded)) + 4, b"\x00")


def _artist(slot: int, id_: int, name: str) -> bytes:
    return _with_name(struct.pack("<HHI", 0x60, slot * 0x20, id_), name)


def _album(slot: int, id_: int, artist_id: int, name: str) -> bytes:
    return _with_name(struct.pack("<HHIIII", 0x80, slot * 0x20, 0, artist_id, id_, 0), name)


def _key(id_: int, name: str) -> bytes:
    return struct.pack("<II", id_, id_) + encode_string(name)


def _color(id_: int, name: str) -> bytes:
    return bytes([0, 0, 0, 0, id_, id_, 0, 0]) + encode_string(name)


def _playlist_node(parent: int, order: int, id_: int, folder: bool, name: str) -> bytes:
    return struct.pack("<IIIII", parent, 0, order, id_, int(folder)) + encode_string(name)


def _history_property(tracks: int, date: str, device: str) -> bytes:
    row = bytearray(struct.pack("<III", 0x0280, tracks, 0))
    row += encode_string(date) + bytes([0x19, 0x1E]) + encode_string("1000")
    name = encode_string(device)
    row += bytes(_string_at(len(row), name) - len(row)) + name
    return bytes(row.ljust(40, b"\x00"))


# Rows Rekordbox writes identically into every export (baken fixed.rs).
COLORS_DEFAULT = [(1, "Pink"), (2, "Red"), (3, "Orange"), (4, "Yellow"), (5, "Green"), (6, "Aqua"),
                  (7, "Blue"), (8, "Purple")]  # fmt: skip
COLUMN_ROWS = [
    (1, 0x80, "GENRE"), (2, 0x81, "ARTIST"), (3, 0x82, "ALBUM"), (4, 0x83, "TRACK"),
    (5, 0x85, "BPM"), (6, 0x86, "RATING"), (7, 0x87, "YEAR"), (8, 0x88, "REMIXER"),
    (9, 0x89, "LABEL"), (10, 0x8A, "ORIGINAL ARTIST"), (11, 0x8B, "KEY"), (12, 0x8D, "CUE"),
    (13, 0x8E, "COLOR"), (14, 0x92, "TIME"), (15, 0x93, "BITRATE"), (16, 0x94, "FILE NAME"),
    (17, 0x84, "PLAYLIST"), (18, 0x98, "HOT CUE BANK"), (19, 0x95, "HISTORY"), (20, 0x91, "SEARCH"),
    (21, 0x96, "COMMENTS"), (22, 0x8C, "DATE ADDED"), (23, 0x97, "DJ PLAY COUNT"),
    (24, 0x90, "FOLDER"), (25, 0xA1, "DEFAULT"), (26, 0xA2, "ALPHABET"), (27, 0xAA, "MATCHING"),
]  # fmt: skip
CATEGORY_ROWS = [bytes.fromhex(h) for h in (
    "0100010063010000", "0500060005010000", "0600070063010000", "0700080063010000",
    "0800090063010000", "09000a0063010000", "0a000b0063010000", "0d000f0063010000",
    "0e00130004010000", "0f00140006010000", "1000150063010000", "1200170063010000",
    "0200020002000100", "0300030003000200", "0400040001000300", "0b000c0063000400",
    "1100050063000500", "1300160063000600", "1400120063000700", "1b001a0063020800",
    "1800110063000900", "16001b0063000a00",
)]  # fmt: skip
SORT_ROWS = [bytes.fromhex(h) for h in (
    "0100060001000000", "1500070001000000", "0e00080001000000", "0800090001000000",
    "09000a0001000000", "0a000b0001000000", "0f000d0001000000", "0d000f0001000000",
    "1700100001000000", "1600110001000000", "1900000000010000", "1a00010000020000",
    "0200020000030000", "0300030000040000", "0500040000050000", "0600050000060000",
    "0b000c0000070000",
)]  # fmt: skip


# --- the export model ---------------------------------------------------------------


@dataclass
class PlaylistNode:
    id: int
    parent: int  # 0 at the top level
    order: int
    name: str
    is_folder: bool
    track_ids: list[int] = field(default_factory=list)


@dataclass
class Pdb:
    tracks: list[TrackRow] = field(default_factory=list)
    artists: dict[int, str] = field(default_factory=dict)
    albums: dict[int, tuple[str, int]] = field(default_factory=dict)  # id -> (name, artist id)
    genres: dict[int, str] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    keys: dict[int, str] = field(default_factory=dict)
    colors: dict[int, str] = field(default_factory=dict)
    artwork: dict[int, str] = field(default_factory=dict)
    playlists: list[PlaylistNode] = field(default_factory=list)
    device_name: str = ""
    export_date: str = ""


# --- reading -------------------------------------------------------------------------


def _row_offsets(page: bytes) -> list[int]:
    if page[0x1B] & 0x40:  # index page
        return []
    slots = page[0x18] | (page[0x19] & 0x1F) << 8
    offsets = []
    for group in range((slots + 15) // 16):
        base = PAGE - group * GROUP
        present = _u16(page, base - 4)
        for i in range(min(16, slots - 16 * group)):
            if present >> i & 1:
                offsets.append(HEAP + _u16(page, base - 6 - 2 * i))
    return offsets


def _table_rows(data: bytes, first: int, last: int) -> list[tuple[bytes, int]]:
    rows = []
    index, seen = first, set()
    while index not in seen and (index + 1) * PAGE <= len(data):
        seen.add(index)
        page = data[index * PAGE : (index + 1) * PAGE]
        rows.extend((page, o) for o in _row_offsets(page))
        if index == last:
            break
        index = _u32(page, 0x0C)
    return rows


def read_pdb(data: bytes) -> Pdb:
    if len(data) < PAGE or _u32(data, 4) != PAGE:
        raise ValueError("not an export.pdb (unexpected page size)")
    tables = {}
    for i in range(_u32(data, 8)):
        type_, _, first, last = struct.unpack_from("<IIII", data, 0x1C + 16 * i)
        tables[type_] = _table_rows(data, first, last)
    pdb = Pdb()

    def each(table: int, parse: Callable[[bytes, int], None]) -> None:
        for page, o in tables.get(table, []):
            parse(page, o)

    each(TRACKS, lambda p, o: pdb.tracks.append(TrackRow.decode(p, o)))
    each(GENRES, lambda p, o: pdb.genres.__setitem__(_u32(p, o), decode_string(p, o + 4)))
    each(LABELS, lambda p, o: pdb.labels.__setitem__(_u32(p, o), decode_string(p, o + 4)))
    each(KEYS, lambda p, o: pdb.keys.__setitem__(_u32(p, o), decode_string(p, o + 8)))
    each(COLORS, lambda p, o: pdb.colors.__setitem__(_u16(p, o + 5), decode_string(p, o + 8)))
    each(ARTWORK, lambda p, o: pdb.artwork.__setitem__(_u32(p, o), decode_string(p, o + 4)))

    def artist(p: bytes, o: int) -> None:
        name_at = _u16(p, o + 0x0A) if _u16(p, o) == 0x64 else p[o + 9]
        pdb.artists[_u32(p, o + 4)] = decode_string(p, o + name_at)

    def album(p: bytes, o: int) -> None:
        name_at = _u16(p, o + 0x16) if _u16(p, o) == 0x84 else p[o + 0x15]
        pdb.albums[_u32(p, o + 12)] = (decode_string(p, o + name_at), _u32(p, o + 8))

    each(ARTISTS, artist)
    each(ALBUMS, album)
    nodes: dict[int, PlaylistNode] = {}

    def node(p: bytes, o: int) -> None:
        parent, _, order, id_, folder = struct.unpack_from("<IIIII", p, o)
        nodes[id_] = PlaylistNode(id_, parent, order, decode_string(p, o + 20), bool(folder))

    each(PLAYLIST_TREE, node)
    entries: list[tuple[int, int, int]] = []
    each(PLAYLIST_ENTRIES, lambda p, o: entries.append(struct.unpack_from("<III", p, o)))
    for _index, track_id, playlist_id in sorted(entries, key=lambda e: (e[2], e[0])):
        if playlist_id in nodes:
            nodes[playlist_id].track_ids.append(track_id)
    pdb.playlists = sorted(nodes.values(), key=lambda n: (n.parent, n.order))
    return pdb


# --- writing -------------------------------------------------------------------------


def _dir_bytes(rows: int) -> int:
    full, rest = divmod(rows, 16)
    return full * GROUP + (rest * 2 + 4 if rest else 0)


class _DataPage:
    def __init__(self, table: int, fixed: bool) -> None:
        self.table, self.fixed = table, fixed
        self.rows: list[bytes] = []
        self.used = 0
        self.index = self.next = self.seq = 0

    def try_push(self, row: bytes) -> bool:
        need = self.used + _align4(len(row)) + _dir_bytes(len(self.rows) + 1)
        if need > PAGE - HEAP:
            return False
        self.used += _align4(len(row))
        self.rows.append(row)
        return True

    def to_bytes(self) -> bytes:
        p = bytearray(PAGE)
        n = len(self.rows)
        free = PAGE - HEAP - self.used - _dir_bytes(n)
        u5, nrl = (n, 0) if self.fixed else (1, max(0, n - 1))
        struct.pack_into("<III", p, 4, self.index, self.table, self.next)
        struct.pack_into("<I", p, 0x10, self.seq)
        p[0x18] = n & 0xFF
        p[0x19] = ((n & 7) << 5) | ((n >> 8) & 0x1F)
        p[0x1A] = (n >> 3) & 0xFF
        p[0x1B] = 0x24
        struct.pack_into("<HHHH", p, 0x1C, free, self.used, u5, nrl)
        at = 0
        for i, row in enumerate(self.rows):
            p[HEAP + at : HEAP + at + len(row)] = row
            base = PAGE - (i // 16) * GROUP
            slot = i % 16
            struct.pack_into("<H", p, base - 6 - 2 * slot, at)
            present = _u16(p, base - 4) | 1 << slot
            struct.pack_into("<HH", p, base - 4, present, present)
            at += _align4(len(row))
        return bytes(p)


def _index_page(index: int, table: int, next_: int, first_data: int | None) -> bytes:
    p = bytearray(PAGE)
    struct.pack_into("<IIII", p, 4, index, table, next_, 1)
    p[0x1B] = 0x64
    struct.pack_into("<HHH", p, 0x20, 0x1FFF, 0x1FFF, 0x03EC)
    struct.pack_into(
        "<IIIII",
        p,
        HEAP,
        index,
        first_data if first_data is not None else NONE,
        NONE,
        0,
        0x1FFF0000,
    )
    struct.pack_into("<1004I", p, HEAP + 20, *([0x1FFFFFF8] * 1004))
    return bytes(p)


RowMaker = Callable[[int], bytes]  # slot in page -> row bytes


def write_pdb(pdb: Pdb) -> bytes:
    """A fresh export: page 0, each table's index and data pages, one empty page per table."""
    tables: list[tuple[bool, int | None, list[RowMaker]]] = [
        (False, None, []) for _ in range(NUM_TABLES)
    ]

    def fixed(rows: list[bytes]) -> list[RowMaker]:
        return [lambda _slot, r=r: r for r in rows]

    tables[TRACKS] = (False, None, [lambda slot, t=t: t.encode(slot) for t in pdb.tracks])
    tables[GENRES] = (False, None, fixed([_named(i, n) for i, n in pdb.genres.items()]))
    tables[ARTISTS] = (
        False,
        None,
        [lambda slot, i=i, n=n: _artist(slot, i, n) for i, n in pdb.artists.items()],
    )
    tables[ALBUMS] = (
        False,
        None,
        [lambda slot, i=i, n=n, a=a: _album(slot, i, a, n) for i, (n, a) in pdb.albums.items()],
    )
    tables[LABELS] = (False, None, fixed([_named(i, n) for i, n in pdb.labels.items()]))
    tables[KEYS] = (False, None, fixed([_key(i, n) for i, n in pdb.keys.items()]))
    tables[COLORS] = (
        True,
        2,
        fixed([_color(i, n) for i, n in (pdb.colors.items() or COLORS_DEFAULT)]),
    )
    tables[PLAYLIST_TREE] = (
        False,
        None,
        fixed(
            [_playlist_node(n.parent, n.order, n.id, n.is_folder, n.name) for n in pdb.playlists]
        ),
    )
    tables[PLAYLIST_ENTRIES] = (
        False,
        None,
        fixed(
            [
                struct.pack("<III", i, tid, n.id)
                for n in pdb.playlists
                if not n.is_folder
                for i, tid in enumerate(n.track_ids, 1)
            ]
        ),
    )
    tables[ARTWORK] = (False, None, fixed([_named(i, p) for i, p in pdb.artwork.items()]))
    tables[COLUMNS] = (
        True,
        3,
        fixed([struct.pack("<HH", i, c) + encode_string(f"￺{n}￻") for i, c, n in COLUMN_ROWS]),
    )
    tables[17] = (False, None, fixed(CATEGORY_ROWS))
    tables[18] = (False, None, fixed(SORT_ROWS))
    tables[HISTORY] = (
        False,
        None,
        fixed([_history_property(len(pdb.tracks), pdb.export_date, pdb.device_name)]),
    )

    pages: list[bytes] = [b""]
    plan: list[tuple[int, int, list[_DataPage]]] = []
    seq, max_seq = 10, 3
    for type_, (is_fixed, fixed_seq, makers) in enumerate(tables):
        index_at = len(pages)
        pages.append(b"")
        data: list[_DataPage] = []
        page = _DataPage(type_, is_fixed)
        for make in makers:
            if not page.try_push(make(len(page.rows))):
                data.append(page)
                page = _DataPage(type_, is_fixed)
                if not page.try_push(make(0)):
                    raise ValueError("row larger than a page")
        if page.rows:
            data.append(page)
        for p in data:
            p.index = len(pages)
            if fixed_seq is None:
                seq += 1
                p.seq = seq
            else:
                p.seq = fixed_seq
            max_seq = max(max_seq, p.seq)
            pages.append(b"")
        plan.append((type_, index_at, data))

    first_empty = len(pages)
    pointers = []
    for n, (type_, index_at, data) in enumerate(plan):
        empty = first_empty + n
        first_data = data[0].index if data else None
        pointers.append((type_, empty, index_at, data[-1].index if data else index_at))
        pages[index_at] = _index_page(
            index_at, type_, first_data if first_data is not None else empty, first_data
        )
        for k, p in enumerate(data):
            p.next = data[k + 1].index if k + 1 < len(data) else empty
            pages[p.index] = p.to_bytes()
    pages.extend(bytes(PAGE) for _ in range(NUM_TABLES))
    header = bytearray(PAGE)
    struct.pack_into("<IIIIII", header, 4, PAGE, NUM_TABLES, len(pages), 1, max_seq + 1, 0)
    for n, pointer in enumerate(pointers):
        struct.pack_into("<IIII", header, 0x1C + 16 * n, *pointer)
    pages[0] = bytes(header)
    return b"".join(pages)
