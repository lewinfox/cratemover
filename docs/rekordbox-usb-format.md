# Rekordbox USB stick format: a reverse-engineering reference

What a rekordbox-exported USB stick contains, byte by byte, as far as we know it. Written for
people building tools that read or write these sticks. Most of it is checked against real
rekordbox 7.2.19 exports; the rest is marked with where it comes from.

Contents: [Evidence](#evidence-and-how-claims-are-marked) ·
[1 Layout](#1-stick-layout) · [2 export.pdb](#2-exportpdb-device-library) ·
[3 exportExt.pdb](#3-exportextpdb) · [4 ANLZ](#4-analysis-files-anlz0000datextex) ·
[5 OneLibrary](#5-onelibrary-exportlibrarydb) · [6 Artwork](#6-artwork) ·
[7 Settings](#7-settings-and-profile-files) · [8 Behaviour](#8-how-rekordbox-behaves) ·
[9 Versions](#9-version-specific-values) · [10 cratemover vs rekordbox](#10-what-cratemover-writes-vs-rekordbox) ·
[11 Open questions](#11-open-questions) · [Sources](#sources)

## Evidence, and how claims are marked

| Tag                   | Evidence                                                                                                                                                                                                                                                                                                                                                                                                 |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **[UFC8]**            | A full copy of a stick exported by **rekordbox 7.2.19 for Windows (under Wine, in Docker on Linux)** on 2026-10-01 UTC. Freshly formatted FAT32, one MBR partition. One playlist `pop classics`, 21 MP3s, 50 memory cues on 18 tracks, no hot cues, all gridded, 14 artwork images. Copied while rekordbox was still open (WAL checkpointed, 0 bytes). sha256 of every file re-checked against the copy. |
| **[C9YD]**            | `PIONEER/` of an earlier rekordbox 7.2.19 export (3 tracks, one playlist, memory cues on 2 tracks), copied after rekordbox closed, with checksums from before and after closing. Plus cratemover's OneLibrary written from that stick's `export.pdb` by an older writer build.                                                                                                                           |
| **[6.8.6]**           | `tests/fixtures/rekordbox/stick-6.8.6/`: a rekordbox 6.8.6 export (2 tracks) from acrilique/rekordlib.                                                                                                                                                                                                                                                                                                   |
| **[code]**            | What cratemover's code does (`src/cratemover/pioneer/`). Its constants were ported from baken and Deep Symmetry, who checked them against rekordbox output. Not re-checked here unless also tagged with an export.                                                                                                                                                                                       |
| **[DS]**, **[cited]** | Deep Symmetry's analysis, or another linked source.                                                                                                                                                                                                                                                                                                                                                      |
| **[session]**         | Seen by hand during this investigation (rekordbox running on a stick), with no saved file to show it.                                                                                                                                                                                                                                                                                                    |

Byte order: `export.pdb`/`exportExt.pdb` are **little-endian**; ANLZ files are **big-endian**.
Offsets are hex unless stated. "Row offset" means from the start of the row.

Some caveats up front:

- rekordbox ran under Wine. File contents should be the same as on real Windows, but that is
  untested.
- All tracks are MP3. Nothing here is checked for AAC, FLAC, WAV or AIFF.
- No stick discussed here has been on real CDJ/XDJ hardware.

---

## 1. Stick layout

Every file on UFC8, outside `Contents/` and the per-track analysis folders:

```
/Contents/<artist>/<album>/<file>.mp3          21 audio files
/PIONEER/
    rekordbox/export.pdb                        233472 B (57 pages)
    rekordbox/exportExt.pdb                      73728 B (18 pages)
    rekordbox/exportLibrary.db                  126976 B (31 pages, SQLCipher)
    rekordbox/exportLibrary.db-wal                   0 B
    rekordbox/exportLibrary.db-shm               32768 B
    USBANLZ/Pxxx/xxxxxxxx/ANLZ0000.DAT|.EXT|.2EX 21 × 3 files
    Artwork/00001/{a,b}N.jpg, {a,b}N_m.jpg       14 × 4 files
    MYSETTING.DAT                                  148 B
    MYSETTING2.DAT                                 148 B
    DJMMYSETTING.DAT                               160 B
    djprofile.nxs                                  160 B
    extracted/gcred.dat                             66 B
```

| Path                                                   | Purpose                                                                                                                                                            | Written by                                                              | Do players need it?                                                                                |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `Contents/…`                                           | Audio. rekordbox copies tracks to `/Contents/<artist>/<album>/<name>`. Missing tags become `UnknownArtist` / `UnknownAlbum` (no space). [UFC8]                     | rekordbox export                                                        | Yes: the databases point here by full path.                                                        |
| `PIONEER/rekordbox/export.pdb`                         | Device Library: the database older players browse (§2).                                                                                                            | rekordbox export; rewritten on open and on close [C9YD hashes, session] | Yes, for Device Library players [cited].                                                           |
| `PIONEER/rekordbox/exportExt.pdb`                      | My Tag names and track tags, plus one property row (§3).                                                                                                           | rekordbox                                                               | Unknown. What older players do when it is missing is untested (cratemover moves it aside; see #1). |
| `PIONEER/rekordbox/exportLibrary.db` (+`-wal`, `-shm`) | OneLibrary: the encrypted SQLite database newer players browse (§5). `-wal`/`-shm` exist only while rekordbox has it open; closing folds the WAL in [C9YD hashes]. | rekordbox ≥ 6.8.1 [cited]                                               | Yes, for OneLibrary players (CDJ-3000X, XDJ-AZ, OPUS-QUAD, OMNIS-DUO, …) [cited].                  |
| `PIONEER/USBANLZ/…`                                    | Analysis per track: beat grid, cues, waveforms (§4). Folder name is a hash of the track's path.                                                                    | rekordbox export; rewritten when you edit cues [session]                | Yes. Cues and grids exist **only** here (OneLibrary's `cue` table is empty).                       |
| `PIONEER/Artwork/00001/…`                              | Cover art thumbnails (§6).                                                                                                                                         | rekordbox export                                                        | Only to show artwork.                                                                              |
| `MYSETTING.DAT`, `MYSETTING2.DAT`, `DJMMYSETTING.DAT`  | Player/mixer preferences carried on the stick (§7).                                                                                                                | rekordbox, on export and on open [session]                              | Unknown. Probably optional: players have their own defaults.                                       |
| `DEVSETTING.DAT`                                       | Device settings (§7). **Not on UFC8 or C9YD**; present in [6.8.6] and written by rekordbox 7.2.19 when it opened a cratemover stick [session].                     | rekordbox, sometimes                                                    | Unknown.                                                                                           |
| `djprofile.nxs`                                        | DJ profile: a timestamp and the DJ name (§7).                                                                                                                      | rekordbox                                                               | Unknown. Probably optional.                                                                        |
| `extracted/gcred.dat`                                  | Unknown, 64 base64 characters + CRLF (§7).                                                                                                                         | rekordbox 7                                                             | Unknown.                                                                                           |
| `PIONEER/rekordbox/brokendb`                           | A 1-byte marker rekordbox wrote after crashing on a bad stick (§8).                                                                                                | rekordbox                                                               | —                                                                                                  |

Where the audio goes [UFC8]:

- Folder names come from the artist and album tags verbatim, including non-ASCII (`RÄVΞN`,
  `Hōhā`, full-width commas `，` that are in the tags themselves, and an emoji in [C9YD]).
- The file stem is cut to **44 characters** (13 of 21 stems are exactly 44, none longer, and
  some are cut mid-word: `135. Wanna Be - Lissat, Ghostbusterz (Origin.mp3`). The cut seems
  to be rekordbox's, but the source file names weren't kept, so this is not proven.

---

## 2. `export.pdb` (Device Library)

A "DeviceSQL" database: an array of 4096-byte pages. Page 0 is a header with one pointer per
table. Each table is a linked chain of pages: an index page, then data pages. Reading and writing
code: `src/cratemover/pioneer/pdb.py`.

### 2.1 File header (page 0)

UFC8 `export.pdb`, first 0x60 bytes:

```
00000000: 0000 0000 0010 0000 1400 0000 3a00 0000  ............:...
00000010: 0500 0000 7500 0000 0000 0000 0000 0000  ....u...........
00000020: 3900 0000 0100 0000 3800 0000 0100 0000  9.......8.......
00000030: 3300 0000 0300 0000 0400 0000 0200 0000  3...............
```

| Off | Size   | Field            | UFC8   | Notes                                                                                  |
| --- | ------ | ---------------- | ------ | -------------------------------------------------------------------------------------- |
| 00  | 4      | zero             | 0      |                                                                                        |
| 04  | 4      | page size        | 0x1000 |                                                                                        |
| 08  | 4      | number of tables | 20     | `exportExt.pdb`: 9                                                                     |
| 0C  | 4      | next unused page | 58     | File has 57 pages (0–56), so it points past the end.                                   |
| 10  | 4      | unknown          | 5      | 5 in every rekordbox file here (UFC8, C9YD, 6.8.6, both `.pdb`s). cratemover writes 1. |
| 14  | 4      | sequence         | 0x75   | Bumped on edits [DS].                                                                  |
| 18  | 4      | zero             | 0      |                                                                                        |
| 1C  | 16 × n | table pointers   |        | `type, empty_candidate, first_page, last_page`, each u32.                              |

First pointer above: type 0 (tracks), empty candidate 0x39, first page 1, last page 0x38.

### 2.2 Page header

```
page 2 (tracks data):
00 00 00 00 02 00 00 00 00 00 00 00 31 00 00 00 68 00 00 00 00 00 00 00
08 e0 00 34 00 01 c4 0e ff 1f ff 1f 00 00 00 00
```

| Off | Size | Field                                                                             | Page 2 value                                                                         |
| --- | ---- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| 00  | 4    | zero                                                                              |                                                                                      |
| 04  | 4    | page index                                                                        | 2                                                                                    |
| 08  | 4    | table type                                                                        | 0 (tracks)                                                                           |
| 0C  | 4    | next page in chain                                                                | 0x31                                                                                 |
| 10  | 4    | page sequence                                                                     | 0x68                                                                                 |
| 14  | 4    | unknown, 0                                                                        |                                                                                      |
| 18  | 3    | packed counts: low 13 bits = number of row slots; high 11 bits = live rows        | 8 slots, 7 rows                                                                      |
| 1B  | 1    | flags: `0x64` index page, `0x24` data page, `0x34` data page holding deleted rows | 0x34                                                                                 |
| 1C  | 2    | free bytes                                                                        | 0x100                                                                                |
| 1E  | 2    | used heap bytes                                                                   | 0xEC4                                                                                |
| 20  | 2    | unknown                                                                           | 0x1FFF here; 1 on single-page tables; = row count on fixed tables (colours, columns) |
| 22  | 2    | unknown ("num rows large" [DS])                                                   | 0x1FFF here; else rows − 1 or 0                                                      |
| 24  | 4    | zero                                                                              |                                                                                      |

Rows live in a heap from 0x28, growing upward. Their positions are stored at the **end of the
page, growing downward**, in groups of 16. Each group is 0x24 bytes: 16 × u16 row offsets
(relative to 0x28; slot _i_ at `group_end − 6 − 2i`), then u16 "present" bits, then u16
"transaction" bits [DS, UFC8]. Page 2's last group:

```
… 18 0d f0 0a c8 08 fc 06 44 05 8c 03 c0 01 00 00 | fb 00 | 00 00
  slot7 ………………………………………………………… slot1 slot0   present=0b11111011  tx=0
```

Slot 2 is not present. Its row is a stale copy of track 3, and slot 3 holds the live one.
**rekordbox leaves deleted rows in place even in a fresh export** [UFC8]. Tracks use 27 slots
across 4 pages for 21 live rows. The history table (type 0x13) has 22 slots and 1 live row: it
was rewritten 21 times during one export. Readers must honour the present bits.

Index pages (flag 0x64) start at 0x28 with `page_index, first_data_page, 0x03FFFFFF, 0,
0x1FFF0000`, then entries that are `0x1FFFFFF8` when empty [code]. For the multi-page tracks
table, rekordbox fills entries with `page << 3`: pages 2, 49 and 56, which are exactly the pages
flagged 0x34 (holding deleted rows). The data page without deleted rows, 53, is not listed. The
u16 at index-page offset 0x24 is 4. cratemover writes no entries [UFC8 vs code]. What the entries
mean is an open question.

### 2.3 Tables

| Type      | Table [DS name]           | UFC8 rows     | Row layout (row offsets)                                                                                                    |
| --------- | ------------------------- | ------------- | --------------------------------------------------------------------------------------------------------------------------- |
| 0         | tracks                    | 21 (27 slots) | §2.5                                                                                                                        |
| 1         | genres                    | 6             | `u32 id, string name`                                                                                                       |
| 2         | artists                   | 21            | `u16 subtype 0x60, u16 index_shift, u32 id, u8 0x03, u8 name_offset, …name` (subtype 0x64 uses a u16 offset at 0x0A)        |
| 3         | albums                    | 1             | `u16 0x80, u16 index_shift, u32 0, u32 artist_id, u32 id, u32 0, u8 0x03, u8 name_offset, …name` (0x84: u16 offset at 0x16) |
| 4         | labels                    | 1             | `u32 id, string name`                                                                                                       |
| 5         | keys                      | 11            | `u32 id, u32 id (again), string name` (e.g. `3A`)                                                                           |
| 6         | colors                    | 8             | `u32 0, u8 id, u8 id, u16 0, string name`. Pink, Red, Orange, Yellow, Green, Aqua, Blue, Purple (ids 1–8)                   |
| 7         | playlist tree             | 1             | `u32 parent_id, u32 0, u32 sort_order, u32 id, u32 is_folder, string name`                                                  |
| 8         | playlist entries          | 21            | `u32 entry_index (from 1), u32 track_id, u32 playlist_id`                                                                   |
| 9–12      | (unknown)                 | 0             | index page only                                                                                                             |
| 13 (0x0D) | artwork                   | 14            | `u32 id, string path` (`/PIONEER/Artwork/00001/aN.jpg`)                                                                     |
| 14, 15    | (unknown)                 | 0             |                                                                                                                             |
| 16 (0x10) | columns                   | 27            | `u16 id, u16 kind, UTF-16 string "￺NAME￻"` (browse menu items GENRE … MATCHING)                                             |
| 17 (0x11) | [DS: history playlists]   | 22            | 8-byte rows, **identical to OneLibrary `category`**                                                                         |
| 18 (0x12) | [DS: history entries]     | 17            | 8-byte rows, **identical to OneLibrary `sort`**                                                                             |
| 19 (0x13) | history / export property | 1             | §2.6                                                                                                                        |

The fixed tables (6, 16, 17, 18) on UFC8 match cratemover's constants byte for byte
(`COLORS_DEFAULT`, `COLUMN_ROWS`, `CATEGORY_ROWS`, `SORT_ROWS`) [UFC8 + code]. Deep Symmetry
names 0x11/0x12 as history tables. On these exports they hold menu category and sort rows, and
their counts (22, 17) match OneLibrary's `category` and `sort` tables.

`index_shift` is `slot × 0x20` (0, 0x20, 0x40, …) [UFC8].

Raw rows from UFC8:

```
genre    01 00 00 00 19 44 61 6e 63 65 20 26 20 45 44 4d          id 1 "Dance & EDM"
artist   60 00 00 00 01 00 00 00 03 0a 0f 4d 4e 45 45 4d 4f       id 1, name at +0x0a "MNEEMO"
artist   60 00 a0 01 0e 00 00 00 03 0c 00 00 90 0e 00 00 52 00 c4 00 56 00 9e 03 4e 00
         (slot 13, id 14, UTF-16 name aligned to +0x0c: "RÄVΞN")
album    80 00 00 00 00 00 00 00 0c 00 00 00 01 00 00 00 00 00 00 00 03 16 55 42 65 61 …
         artist 12 ("Various Artists"), id 1, name at +0x16
key      01 00 00 00 01 00 00 00 07 33 41                          id 1 "3A"
color    00 00 00 00 01 01 00 00 0b 50 69 6e 6b                    id 1 "Pink"
playlist 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 1b 70 6f 70 20 63 6c 61 …
         parent 0, order 0, id 1, not folder, "pop classics"
entry    01 00 00 00 01 00 00 00 01 00 00 00                       #1: track 1 in playlist 1
artwork  01 00 00 00 3d 2f 50 49 4f 4e 45 45 52 2f 41 72 74 …      id 1 "/PIONEER/Artwork/00001/a1.jpg"
column   01 00 80 00 90 12 00 00 fa ff 47 00 45 00 4e 00 52 00 45 00 fb ff   GENRE
```

### 2.4 Strings

The first byte says the encoding [DS, UFC8]:

| First byte      | Format                                                                                                                             | Example                     |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| odd (bit 0 set) | Short ASCII. Total length incl. this byte = `byte >> 1`; so text length = `(byte >> 1) − 1`, max 126 chars. `0x03` = empty string. | `07 33 41` = "3A"           |
| `0x90`          | UTF-16LE. `u16 total length (incl. 4-byte header)`, `u8 0`, then text.                                                             | `90 0e 00 00 52 00 c4 00 …` |
| `0x40`          | Long ASCII, same header as 0x90. rekordbox 7 was not seen writing it.                                                              |                             |

rekordbox uses UTF-16 for any non-ASCII text, and also for the column names, which contain
U+FFFA/U+FFFB. Inside artist and album rows the UTF-16 name starts on a 4-byte boundary. The
artist row above has its name at +0x0c, not +0x0a. cratemover does the same (an NXS2 is reported
to freeze otherwise) [UFC8, code].

### 2.5 Track rows

UFC8 track 1 (page 2, slot 0, page offset 0x28), the fixed part:

```
0000: 24 00 00 00 00 07 0c 00 80 bb 00 00 00 00 00 00
0010: 7d f2 4a 00 33 49 77 0c 47 93 37 4a 00 00 00 00
0020: 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030: 40 01 00 00 00 00 00 00 e0 2e 00 00 00 00 00 00
0040: 00 00 00 00 01 00 00 00 01 00 00 00 00 00 00 00
0050: 00 00 10 00 c3 00 29 00 00 00 01 00 03 00 88 00
0060: 89 00 8a 00 8c 00 8e 00 90 00 91 00 94 00 95 00
0070: 96 00 97 00 a2 00 a3 00 a4 00 a5 00 d1 00 dc 00
0080: dd 00 0a 01 0b 01 3c 01
```

| Off | Size | Field              | Track 1    | Notes                                                                                           |
| --- | ---- | ------------------ | ---------- | ----------------------------------------------------------------------------------------------- |
| 00  | 2    | subtype            | 0x24       |                                                                                                 |
| 02  | 2    | index_shift        | 0          | slot × 0x20                                                                                     |
| 04  | 4    | bitmask            | 0x000C0700 | Constant. Same value as OneLibrary `content.contentLink` (788224).                              |
| 08  | 4    | sample rate        | 48000      |                                                                                                 |
| 0C  | 4    | composer id        | 0          |                                                                                                 |
| 10  | 4    | file size          | 4911741    |                                                                                                 |
| 14  | 4    | master content id  | 209144115  | The track's id in the rekordbox collection (= OneLibrary `masterContentId`). Differs per track. |
| 18  | 4    | master DB id       | 1245156167 | Same for all tracks: identifies the collection (= OneLibrary `masterDbId`).                     |
| 1C  | 4    | artwork id         | 0          | → artwork table                                                                                 |
| 20  | 4    | key id             | 1          |                                                                                                 |
| 24  | 4    | original artist id | 0          |                                                                                                 |
| 28  | 4    | label id           | 0          |                                                                                                 |
| 2C  | 4    | remixer id         | 0          |                                                                                                 |
| 30  | 4    | bitrate (kbps)     | 320        | rekordbox wrote 32 for two 48 kHz files, likely a misread VBR header.                           |
| 34  | 4    | track number       | 0          |                                                                                                 |
| 38  | 4    | tempo × 100        | 12000      |                                                                                                 |
| 3C  | 4    | genre id           | 0          |                                                                                                 |
| 40  | 4    | album id           | 0          |                                                                                                 |
| 44  | 4    | artist id          | 1          |                                                                                                 |
| 48  | 4    | track id           | 1          | = OneLibrary `content_id`                                                                       |
| 4C  | 2    | disc number        | 0          |                                                                                                 |
| 4E  | 2    | play count         | 0          |                                                                                                 |
| 50  | 2    | year               | 0          |                                                                                                 |
| 52  | 2    | bits per sample    | 16         |                                                                                                 |
| 54  | 2    | duration (s)       | 195        |                                                                                                 |
| 56  | 2    | unknown            | 41         | Constant. = OneLibrary `analysedBits`.                                                          |
| 58  | 1    | colour id          | 0          |                                                                                                 |
| 59  | 1    | rating             | 0          |                                                                                                 |
| 5A  | 2    | file type          | 1          | 1 MP3, 4 M4A/AAC, 5 FLAC, 6 ALAC, 0x0B WAV, 0x0C AIFF [code]                                    |
| 5C  | 2    | unknown            | 3          | Constant.                                                                                       |
| 5E  | 21×2 | string offsets     |            | From the row start.                                                                             |

The 21 strings (names from [DS]; values for UFC8 track 1; "varies" from all 21 rows):

| #    | DS name            | UFC8                                                 | Notes                                                                                                                     |
| ---- | ------------------ | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| 0    | isrc               | ""                                                   |                                                                                                                           |
| 1    | lyricist           | ""                                                   |                                                                                                                           |
| 2    | unknown_string_2   | "2"                                                  | "2" on 20 tracks, "3" on one. [6.8.6]: "2". cratemover writes "1".                                                        |
| 3    | unknown_string_3   | "1"                                                  | "1", "2" on two tracks.                                                                                                   |
| 4    | unknown_string_4   | "1"                                                  | Empty on exactly the 3 tracks with no cues; otherwise 1–6, often cue count + 1. Maybe a cue-edit counter (open question). |
| 5    | message            | ""                                                   |                                                                                                                           |
| 6    | publish_track_info | "ON"                                                 | All tracks. = OneLibrary `isKuvoDeliverStatusOn` 1.                                                                       |
| 7    | autoload_hotcues   | ""                                                   | All UFC8 tracks; "ON" in [6.8.6]. Tracks OneLibrary `isHotCueAutoLoadOn` (0 vs 1).                                        |
| 8, 9 | unknown            | ""                                                   |                                                                                                                           |
| 10   | date_added         | "2026-08-13"                                         | Equals OneLibrary `dateCreated` (the file's date) on all 21.                                                              |
| 11   | release_date       | ""                                                   |                                                                                                                           |
| 12   | mix_name           | ""                                                   |                                                                                                                           |
| 13   | unknown_string_7   | ""                                                   |                                                                                                                           |
| 14   | analyze_path       | "/PIONEER/USBANLZ/P05A/00014EA6/ANLZ0000.DAT"        |                                                                                                                           |
| 15   | analyze_date       | "2026-09-29"                                         | **Equals OneLibrary `dateAdded` (StockDate) on all 24 tracks of UFC8 + C9YD.** See §11.                                   |
| 16   | comment            | ""                                                   |                                                                                                                           |
| 17   | title              | "Rihanna - Umbrella (MNEEMO Afro House Remix)"       |                                                                                                                           |
| 18   | unknown_string_8   | ""                                                   |                                                                                                                           |
| 19   | filename           | "[MNEEMO] Rihanna - Umbrella (MNEEMO Afro Hou.mp3"   |                                                                                                                           |
| 20   | file_path          | "/Contents/MNEEMO/UnknownAlbum/[MNEEMO] Rihanna - …" |                                                                                                                           |

The strings follow the offset array (at 0x88) with no padding between short-ASCII strings. Empty
strings are a single `0x03`:

```
+0x88: 03 03 05 32 05 31 05 31 03 07 4f 4e 03 03 03 17 32 30 32 36 2d 30 38 2d 31 33 …
       ""  "" "2"   "1"   "1"   "" "ON"    "" "" "" "2026-08-13"
```

### 2.6 Table 0x13 (history / export property)

UFC8's single live row:

```
80 02 a0 02 15 00 00 00 00 00 00 00 17 32 30 32 36 2d 31 30 2d 30 31 19 1e 0b 31 30 30 30 03
u16 0x0280, u16 index_shift 0x2a0, u32 21 (track count), u32 0,
"2026-10-01", 0x19 0x1e, "1000", "" (device name)
```

The date is the **UTC** export date (local time was already 2026-10-02) [UFC8]. The device name
is empty. [6.8.6] has no row in this table. cratemover writes the same shape [code].

---

## 3. `exportExt.pdb`

Same page format, 9 tables, 18 pages. On UFC8 only two tables have rows:

| Type         | Table                                  | UFC8 rows              |
| ------------ | -------------------------------------- | ---------------------- |
| 3            | tags (My Tag categories and tags) [DS] | 28                     |
| 4            | tag ↔ track [DS]                       | 0 (no track is tagged) |
| 7            | one property-like row (new finding)    | 1                      |
| 0–2, 5, 6, 8 | —                                      | 0                      |

**Tag rows** (subtype 0x0680), e.g. the category "Genre" and the tag "Acid House":

```
80 06 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 01 03 1f 25 0d 47 65 6e 72 65 03
80 06 20 00 00 00 00 00 00 00 00 00 01 00 00 00 00 00 00 00 88 89 4d 5b 00 00 00 00 03 1f 2a 17 41 63 69 64 20 48 6f 75 73 65 03
```

| Off | Size | Field                                    | Genre / Acid House          |
| --- | ---- | ---------------------------------------- | --------------------------- |
| 00  | 2    | subtype                                  | 0x0680                      |
| 02  | 2    | index_shift                              | 0 / 0x20                    |
| 04  | 8    | zero                                     |                             |
| 0C  | 4    | category id (0 for a category)           | 0 / 1                       |
| 10  | 4    | position in category                     | 0 / 0                       |
| 14  | 4    | id                                       | 1 / 0x5B4D8988 (1531808136) |
| 18  | 4    | is category (byte 0x1B = 1)              | 0x01000000 / 0              |
| 1C  | 1    | 0x03                                     |                             |
| 1D  | 1    | name offset                              | 0x1F                        |
| 1E  | 1    | second string offset (always empty here) | 0x25 / 0x2A                 |

The 4 categories have ids 1–4 (Genre, Components, Situation, Untitled Column). The 24 tags have
random-looking u32 ids. **These are the same ids, names and order as OneLibrary's `myTag`
table** [UFC8]. They are the stock rekordbox tags from the user's collection; the user never set
them up.

**Table 7 row** (all of it):

```
00 07 00 00 00…00 (to +0x17) 6c 15 0b 24 03 22 23 24 25 26 03 03 03 03 03
```

The u32 at +0x18 is 0x240B156C = **604706156 = OneLibrary `property.myTagMasterDBID`** [UFC8].
Then come `0x03` and five offsets to five empty strings. So this row links the tags to the user's
collection.

Closing rekordbox rewrote `exportExt.pdb` (and `export.pdb`) on C9YD [C9YD hashes].

---

## 4. Analysis files (`ANLZ0000.DAT/.EXT/.2EX`)

Code: `src/cratemover/pioneer/anlz.py`, `waveform.py`. Layouts follow Deep Symmetry's
`anlz.html` [DS]. All big-endian.

### 4.1 Where they go: the USBANLZ hash

`/PIONEER/USBANLZ/P{p:03X}/{r:08X}/ANLZ0000.DAT`, computed from the track's path on the stick
(e.g. `/Contents/…/x.mp3`):

```python
h = 0
for unit in utf16le_code_units(path):  # surrogate pairs count as two units
    h = (h * 0x5BC9 + unit) & 0xFFFFFFFF
    h = (h * 0x93B5 + unit) & 0xFFFFFFFF
r = h % 200003
p = (
    (r & 1)
    | ((r >> 1) & 2)
    | ((r >> 4) & 4)
    | ((r >> 4) & 8)
    | ((r >> 5) & 0x10)
    | ((r >> 8) & 0x20)
    | ((r >> 10) & 0x40)
)
```

Verified on all 21 UFC8 tracks and all 3 C9YD tracks, including a path with an emoji
[UFC8, C9YD, code `anlz_dir`]. Players can compute the folder themselves, but `export.pdb`
(string 14) and OneLibrary (`analysisDataFilePath`) also store it.

### 4.2 File header

```
50 4d 41 49 00 00 00 1c 00 00 1d 00 00 00 00 01 00 01 00 00 00 01 00 00 00 00 00 00
"PMAI"      hdr len 28  file len 7424 (0x1d00)  tail (16 bytes, constant)
```

The tail `00 00 00 01 00 01 00 00 00 01 00 00 00 00 00 00` is the same in every UFC8 and 6.8.6
file. Sections follow at offset 28. Each starts with `tag[4], u32 header_len, u32 total_len`
[UFC8].

### 4.3 Sections, in order (rekordbox 7.2.19)

| File   | Sections [UFC8]                                                                                  |
| ------ | ------------------------------------------------------------------------------------------------ |
| `.DAT` | PPTH, PVBR, PQTZ, PWAV, PWV2, PCOB (hot), PCOB (memory)                                          |
| `.EXT` | PPTH, PWV3, PCOB (hot), PCOB (memory, always empty), PCO2 (hot), PCO2 (memory), PQT2, PWV5, PWV4 |
| `.2EX` | PPTH, PWV7, PWV6, PWVC, **PVDI**                                                                 |

`PVDI` is new: not in the 6.8.6 files, and cratemover doesn't write it (§9).

| Tag  | hdr  | Contents (observed on UFC8 track 7, 292 s, 48 kHz)                                                                                                                                                                                                                                                             |
| ---- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PPTH | 0x10 | `u32 byte length`, then the path in UTF-16BE with a NUL (82 chars → 0xA4).                                                                                                                                                                                                                                     |
| PVBR | 0x10 | `u32 0`, then 400 × u32 rising byte offsets into the audio file (last one 6871680, file size 6880914), then u32 total samples (14037120 = 292.44 s × 48000). Looks like a seek table. **Filled on 7 of the 8 48 kHz files, all zero on the 13 44.1 kHz files.** cratemover writes zeros plus the sample total. |
| PQTZ | 0x18 | `u32 0, u32 0x00080000, u32 n`, then n × `u16 beat-in-bar, u16 BPM×100, u32 ms`. Track 7: 609 beats; first `00 02 30 d4 00 00 01 2e` = beat 2, 125.00 BPM, 302 ms.                                                                                                                                             |
| PWAV | 0x14 | `u32 400, u32 0x10000`, 400 one-byte columns (monochrome preview).                                                                                                                                                                                                                                             |
| PWV2 | 0x14 | `u32 100, u32 0x10000`, 100 bytes (tiny preview).                                                                                                                                                                                                                                                              |
| PWV3 | 0x18 | `u32 1 (bytes per entry), u32 n, u32 0x00960000`, n = 150 per second (43866).                                                                                                                                                                                                                                  |
| PWV5 | 0x18 | `u32 2, u32 n, u32 0x00960305`: colour detail, 2 bytes per column.                                                                                                                                                                                                                                             |
| PWV4 | 0x18 | `u32 6, u32 1200, u32 0`: colour preview.                                                                                                                                                                                                                                                                      |
| PWV7 | 0x18 | `u32 3, u32 n, u32 0x00960000`: 3-band detail (CDJ-3000).                                                                                                                                                                                                                                                      |
| PWV6 | 0x14 | `u32 3, u32 1200`: 3-band preview.                                                                                                                                                                                                                                                                             |
| PWVC | 0x0E | `u16 0, u16 ×3`. Varies per track (e.g. `0050 0067 0072`). Probably per-band scale or colour thresholds [unverified].                                                                                                                                                                                          |
| PVDI | 0x18 | 12 bytes, **identical in all 21 files**: `00 00 04 00 56 22 00 01 00 00 00 00`. Meaning unknown.                                                                                                                                                                                                               |
| PQT2 | 0x38 | Extended beat grid. Track 7: `u32 0, 01 00 00 02, u32 0`, two 8-byte beat entries (first and last beat: 302 ms and 292142 ms, both 125.00 BPM), `u32 609` (beat count), `u32 0x05C2F6B4`, 8 zero bytes, then u16 entries (`02 9a …`). Not decoded further. cratemover writes an empty one.                     |

### 4.4 Cue lists: PCOB (`.DAT`, `.EXT`) and PCO2 (`.EXT`)

**PCOB** section header (24 bytes): `tag, u32 0x18, u32 len, u32 kind (1 = hot, 0 = memory),
u16 0, u16 count, u32 memory_count`. `memory_count` is `count − 1` for a non-empty memory list,
else `0xFFFFFFFF` [UFC8]. Each entry is a 0x38-byte `PCPT`.

UFC8 track 7 ("Backstreet Boys - Everybody (Mike & Me Edit)"), `.DAT` file offset 0x1C78, the
memory PCOB with 2 cues:

```
0000: 50 43 4f 42 00 00 00 18 00 00 00 88 00 00 00 00   PCOB hdr 0x18 len 0x88 kind 0 (memory)
0010: 00 00 00 02 00 00 00 01                           count 2, memory_count 1
0018: 50 43 50 54 00 00 00 1c 00 00 00 38 00 00 00 00   PCPT hdr 0x1c len 0x38 hot_cue 0
0028: 00 00 00 00 00 01 00 00 ff ff 00 01 01 00 03 e8   status 0, 0x10000, prev 0xffff next 1, type 1, 00 03 e8
0038: 00 00 ef 4e ff ff ff ff 00 00 11 f2 00 00 00 00   time 61262 ms, loop end none
0048: 00 14 d6 00 00 00 00 00                           (16 trailing bytes, see below)
0050: 50 43 50 54 00 00 00 1c 00 00 00 38 00 00 00 00   second PCPT
0060: 00 00 00 00 00 01 00 00 00 00 ff ff 01 00 03 e8   prev 0, next 0xffff
0070: 00 00 01 2e ff ff ff ff 00 00 00 16 00 00 00 00   time 302 ms
0080: 00 00 01 80 00 00 00 00
```

PCPT entry fields (from the entry start):

| Off | Size | Field                                    |
| --- | ---- | ---------------------------------------- |
| 00  | 4    | `PCPT`                                   |
| 04  | 4    | header length 0x1C                       |
| 08  | 4    | length 0x38                              |
| 0C  | 4    | hot cue number (1 = A …; 0 = memory cue) |
| 10  | 4    | status (0)                               |
| 14  | 4    | 0x00010000                               |
| 18  | 2    | previous entry (0xFFFF = none)           |
| 1A  | 2    | next entry (0xFFFF = none)               |
| 1C  | 1    | type: 1 = cue, 2 = loop                  |
| 1D  | 3    | `00 03 e8` (constant)                    |
| 20  | 4    | position (ms)                            |
| 24  | 4    | loop end (ms), 0xFFFFFFFF if not a loop  |
| 28  | 16   | see below                                |

- **Memory cues are listed latest-first** (61262 ms, then 302 ms). Hot cues: latest slot first
  [UFC8, code].
- **The 16 trailing bytes are not always zero.** On the 48 kHz MP3s rekordbox writes
  `u32 a, u32 0, u32 b, u32 0`, with `a` ≈ position × 75/s (61262 ms → 4594) and `b` rising with
  position (1365504 at 61 s). On the 44.1 kHz files they are all zero, the same pattern as PVBR.
  It looks like seek data (OneLibrary's `cue` table has columns like `inMpegFrameNumber` and
  `inFileOffsetInBlock`), but that is unconfirmed. cratemover writes zeros.
- `.DAT` holds hot cues A–C and all memory cues. `.EXT`'s PCOBs hold hot cues D–H and an
  **always-empty** memory list [UFC8, code].

**PCO2** (in `.EXT`): header `tag, u32 0x14, u32 len, u32 kind, u16 count, u16 0`. Entries are
`PCP2`. Same track, memory list:

```
0014: 50 43 50 32 00 00 00 10 00 00 00 58 00 00 00 00   PCP2 hdr 0x10 len 0x58 hot 0
0024: 01 00 03 e8 00 00 ef 4e ff ff ff ff 05 01 00 00   type 1, 61262 ms, no loop, colour id 5, 01
0034: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 …       loop num/den at +0x24, comment len at +0x28 = 0
```

| Off           | Size | Field                                                                |
| ------------- | ---- | -------------------------------------------------------------------- |
| 0C            | 4    | hot cue number (0 = memory)                                          |
| 10            | 1    | type (1 cue, 2 loop), then `00 03 e8`                                |
| 14            | 4    | position ms                                                          |
| 18            | 4    | loop end ms / 0xFFFFFFFF                                             |
| 1C            | 1    | memory-cue colour id (5 on the first cue here, 0 on the second)      |
| 1D            | 1    | 0x01                                                                 |
| 24            | 2+2  | loop length as beats: numerator, denominator                         |
| 28            | 4    | comment length in bytes (UTF-16BE with NUL), comment follows at 0x2C |
| after comment | 4    | hot cue colour: `code, r, g, b`                                      |
| …             |      | padding to `length` (0x2C + comment + 4 + 40)                        |

Across UFC8 the cue counts read from these files total **50 memory cues and 0 hot cues**, which
matches what the user set [UFC8]. A decoded list for track 7: memory cues at 302 ms and 61262 ms.

---

## 5. OneLibrary (`exportLibrary.db`)

### 5.1 Encryption

SQLCipher 4 with **default settings**: 4096-byte pages, PBKDF2-HMAC-SHA512 with 256000
iterations, HMAC-SHA512, no plaintext header. The first 16 bytes of the file are the random salt
[UFC8: opens with these settings, 31 pages × 4096 = file size]. The passphrase is a **fixed public
key, the same for every stick**. rekordbox stores it obfuscated, and pyrekordbox publishes it. In
this repo it is in `src/cratemover/pioneer/keys.py` (`ONE_LIBRARY`; `open_encrypted()` opens a
file). The journal mode is WAL. Encoding UTF-8, `user_version` 0.

### 5.2 Tables and row counts (UFC8)

The schema is identical to cratemover's `SCHEMA` in `onelibrary.py` (taken from [6.8.6]), and
C9YD's is identical too [UFC8, C9YD].

| Table                                                                                        | Rows  | What rekordbox puts in it                                                                                                                                 |
| -------------------------------------------------------------------------------------------- | ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| content                                                                                      | 21    | One per track, `content_id` = `export.pdb` track id.                                                                                                      |
| artist                                                                                       | 21    | Same ids and names as `export.pdb`. `nameForSearch` NULL.                                                                                                 |
| album                                                                                        | 1     | `(1, 'Beatport - …', artist_id 12, image_id NULL, isComplation 0, nameForSearch NULL)`                                                                    |
| genre                                                                                        | 6     | Same as `export.pdb`.                                                                                                                                     |
| label                                                                                        | 1     | Same.                                                                                                                                                     |
| key                                                                                          | 11    | Same (`3A`, `10A`, …).                                                                                                                                    |
| color                                                                                        | 8     | Fixed: Pink … Purple, ids 1–8.                                                                                                                            |
| playlist                                                                                     | 1     | `(1, sequenceNo 0, 'pop classics', image_id NULL, attribute 0, parent 0)`. attribute 1 = folder.                                                          |
| playlist_content                                                                             | 21    | `(playlist_id, content_id, sequenceNo from 1)`                                                                                                            |
| image                                                                                        | 14    | `(N, '/PIONEER/Artwork/00001/bN.jpg')`: the **`b`** files.                                                                                                |
| menuItem                                                                                     | 27    | Fixed: `(id, kind, '￺NAME￻')`, kind 128–170, same list as `export.pdb` table 16.                                                                          |
| category                                                                                     | 22    | Fixed, = `export.pdb` table 17.                                                                                                                           |
| sort                                                                                         | 17    | Fixed, = `export.pdb` table 18.                                                                                                                           |
| myTag                                                                                        | 28    | The 4 categories `(1..4, seq, name, attribute 1, parent 0)` plus 24 tags `(random id, seq, name, attribute 0, parent category)`, same as `exportExt.pdb`. |
| property                                                                                     | 1     | `('', '1000', 21, '2026-10-01', 0, 604706156)`                                                                                                            |
| cue                                                                                          | **0** | Empty, although 18 tracks have cues.                                                                                                                      |
| hotCueBankList, hotCueBankList_cue, history, history_content, myTag_content, recommendedLike | 0     |                                                                                                                                                           |

**The `cue` table is empty in rekordbox's own exports**: on UFC8 (50 cues), C9YD and [6.8.6].
Cues live only in the ANLZ files. DJCrate saw the same ([cited]).

`property` columns: `deviceName` '' · `dbVersion` '1000' (7.2.19; '10000' in [6.8.6]) ·
`numberOfContents` 21 · `createdDate` '2026-10-01' (the **UTC** date; local was 10-02) ·
`backGroundColorType` 0 · `myTagMasterDBID` 604706156 (= the `exportExt.pdb` table 7 row; the
same value on C9YD, so it identifies the user's collection, not the stick).

### 5.3 `content` columns, as rekordbox 7.2.19 fills them

Values on UFC8, with the `export.pdb` field each one matches:

| Column                                                          | Value / rule                             | `export.pdb`                            |
| --------------------------------------------------------------- | ---------------------------------------- | --------------------------------------- |
| content_id                                                      | 1…                                       | track id                                |
| title                                                           | title                                    | s17                                     |
| titleForSearch                                                  | NULL                                     |                                         |
| subtitle                                                        | ''                                       |                                         |
| bpmx100, length, trackNo, discNo                                | numbers                                  | tempo, duration, track number, disc     |
| artist_id_artist                                                | id or NULL                               | artist id                               |
| artist_id_remixer, _originalArtist, _composer                   | NULL when none                           |                                         |
| artist_id_lyricist                                              | **0** (not NULL)                         |                                         |
| album_id, genre_id, label_id                                    | id or **NULL** when none                 | (0 in `export.pdb`)                     |
| key_id                                                          | id                                       |                                         |
| color_id                                                        | 0                                        | colour                                  |
| image_id                                                        | artwork id or NULL                       | artwork id                              |
| djComment                                                       | comment                                  | s16                                     |
| rating, releaseYear                                             | 0 / year                                 |                                         |
| releaseDate                                                     | ''                                       |                                         |
| dateCreated                                                     | file date                                | **s10 `date_added`**                    |
| dateAdded                                                       | date added to the collection (StockDate) | **s15 `analyze_date`** (equal on 24/24) |
| path, fileName                                                  | stick path, name                         | s20, s19                                |
| fileSize, fileType, bitrate, bitDepth, samplingRate             |                                          | same                                    |
| isrc                                                            | ''                                       |                                         |
| djPlayCount                                                     | 0                                        |                                         |
| isHotCueAutoLoadOn                                              | 0 (1 in [6.8.6])                         | s7 '' / 'ON'                            |
| isKuvoDeliverStatusOn                                           | 1                                        | s6 'ON'                                 |
| kuvoDeliveryComment                                             | ''                                       |                                         |
| masterDbId                                                      | 1245156167 (all)                         | +0x18                                   |
| masterContentId                                                 | per track                                | +0x14                                   |
| analysisDataFilePath                                            | `/PIONEER/USBANLZ/…/ANLZ0000.DAT`        | s14                                     |
| analysedBits                                                    | 41                                       | +0x56                                   |
| contentLink                                                     | 788224 (0xC0700)                         | +0x04 bitmask                           |
| hasModified                                                     | 0                                        |                                         |
| cueUpdateCount, analysisDataUpdateCount, informationUpdateCount | NULL                                     | (maybe s2–s4? open question)            |

**Every id is shared with `export.pdb`**: track, artist, album, genre, label, key, playlist,
artwork/image, colour [UFC8]. A reader can treat both databases as one id space.

### 5.4 rekordbox 7.2.19 vs cratemover on the same stick (C9YD)

Table by table, after cratemover was aligned (dbVersion, createdDate, isHotCueAutoLoadOn, NULL
ids, album artist). These differences remain [C9YD]:

| Where                      | rekordbox                 | cratemover                                   |
| -------------------------- | ------------------------- | -------------------------------------------- |
| `image`                    | 3 rows (`b1–b3.jpg`)      | 0 rows                                       |
| `content.image_id`         | 1, 2, 3                   | 0 (rekordbox uses NULL when there is no art) |
| `content.dateAdded`        | StockDate (2026-09-29)    | copied from `export.pdb` s10 (the file date) |
| `myTag`                    | 28 rows (the user's tags) | 4 default categories                         |
| `property.createdDate`     | UTC date                  | local date (one day later at 00:08 NZDT)     |
| `property.myTagMasterDBID` | the collection's id       | random                                       |

Every other table, including the fixed ones, is identical.

---

## 6. Artwork

[UFC8]:

- `PIONEER/Artwork/00001/` holds 4 JPEGs per artwork id N:

  | File       | Size    |
  | ---------- | ------- |
  | `aN.jpg`   | 80×80   |
  | `aN_m.jpg` | 240×240 |
  | `bN.jpg`   | 80×80   |
  | `bN_m.jpg` | 240×240 |

- `a` and `b` are **byte-identical** on all 14 ids here (and on C9YD).
- `export.pdb` artwork table → `/PIONEER/Artwork/00001/aN.jpg`. OneLibrary `image` →
  `…/bN.jpg`. Track → art: `export.pdb` +0x1C = `content.image_id`. Players presumably add `_m`
  for the large version; this is how Deep Symmetry describes it.
- The art files' modification times are older than the export (some 2 days earlier). They seem
  to be copied from rekordbox's local cache, not re-encoded per export (an inference).
- Ids are numbered per export (1…14 here). Tracks without art have id 0 / NULL.

---

## 7. Settings and profile files

### 7.1 `MYSETTING.DAT`, `MYSETTING2.DAT`, `DJMMYSETTING.DAT`, `DEVSETTING.DAT`

All share a layout [UFC8, 6.8.6]:

```
MYSETTING.DAT (UFC8, 148 bytes)
00000000: 6000 0000 5049 4f4e 4545 5200 0000 0000  `...PIONEER.....
00000020: 0000 0000 7265 6b6f 7264 626f 7800 0000  ....rekordbox...
00000040: 0000 0000 302e 3030 3100 0000 0000 0000  ....0.001.......
00000060: 0000 0000 2800 0000 7856 3412 0200 0000  ....(...xV4.....
00000070: 8183 8188 8101 8281 8101 0101 8280 8081  ................
00000080: 8081 8000 0081 0000 8181 8180 8180 0000  ................
00000090: 487a 0000                                Hz..
```

| Off  | Size | Field                                                                                                                                              |
| ---- | ---- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 00   | 4    | 0x60, the length of the string header                                                                                                              |
| 04   | 32   | brand: `PIONEER` (MYSETTING, MYSETTING2), `PioneerDJ` (DJMMYSETTING), `PIONEER DJ` (DEVSETTING)                                                    |
| 24   | 32   | `rekordbox`                                                                                                                                        |
| 44   | 32   | version: `0.001` (MYSETTING, MYSETTING2), `1.000` (DJMMYSETTING), `6.8.6` (DEVSETTING in [6.8.6])                                                  |
| 64   | 4    | payload length L: 40, 40, 52, 32                                                                                                                   |
| 68   | L    | payload. MYSETTING, DJMMYSETTING and DEVSETTING start with magic `78 56 34 12` (0x12345678) and a u32. The settings bytes are mostly 0x80 + value. |
| 68+L | 2    | **CRC-16/XMODEM**, little-endian                                                                                                                   |
| 6A+L | 2    | 0                                                                                                                                                  |

The checksum is over the payload for MYSETTING, MYSETTING2 and DEVSETTING. For DJMMYSETTING it is
over **the whole file before the checksum** [verified by computing all five files]. What the
individual setting bytes mean is not decoded here. rekordcrate and Deep Symmetry describe them.

- UFC8's MYSETTING and DJMMYSETTING are byte-identical to [6.8.6]'s.
- MYSETTING2 differs from [6.8.6].
- C9YD's four files are identical to UFC8's.

So these files follow the user's rekordbox preferences, not the export.

### 7.2 `djprofile.nxs` (160 bytes)

```
00000000: 005d 6cee 0000 019a 1382 15ad 0000 0000  .]l.............
00000010: 0000 0000 0000 0000 0000 0000 01ee 7447  ..............tG
00000020: <DJ name, UTF-8, NUL-padded — redacted>
```

`u32` unknown, then a **u64 big-endian Unix time in ms** at 0x04: 0x019A138215AD =
2025-10-23 23:58:03 UTC, probably the profile's creation time. Another u32 at 0x1C. The DJ name
from the rekordbox account is at 0x20, in UTF-8 ([6.8.6] has a name with `ó` as `c3 b3`). The
rest is zeros. Identical on UFC8 and C9YD.

### 7.3 `extracted/gcred.dat` (66 bytes)

64 base64 characters + CRLF, decoding to 48 bytes. Identical on UFC8 and C9YD. Not in [6.8.6].
Unknown: the name suggests some kind of credential, so its content is not reproduced here.
Whether players need it is unknown.

---

## 8. How rekordbox behaves

1. **Export writes both libraries.** rekordbox exports `export.pdb` and `exportLibrary.db`
   together [UFC8, C9YD]. The release notes put this at 6.8.1. AlphaTheta's notice says "Both
   OneLibrary and Device Library are automatically generated when exporting" for 7.2.11+
   [cited]. OneLibrary was called "Device Library Plus" until 7.2.5 [cited].
2. **Which players read which library** [cited, AlphaTheta notice and FAQ]:
   - OneLibrary: CDJ-3000X, XDJ-AZ, OPUS-QUAD, OMNIS-DUO (the FAQ adds CDJ-1500X and XDJ-AN).
   - `export.pdb`: CDJ-3000, CDJ-2000NXS2 and older, XDJ-1000MK2, XDJ-700, CDJ-900NXS2, XDJ-XZ,
     XDJ-RX3, XDJ-RR.

   The notice warns that Device Library playlists "may not appear on OneLibrary compatible units".

3. **The two libraries drift apart.** Players write playlists and history only to the library
   they use. The FAQ: "There may be differences between the playlist and Histories of the 2
   libraries". rekordbox can sync them [cited].
4. **Converting is one-way and overwrites.** Converting Device Library → OneLibrary replaces an
   existing OneLibrary, losing "any playlists or playback histories stored only in OneLibrary"
   [cited FAQ, USB export guide].
5. **Opening a stick writes to it.** rekordbox 7.2.19 opened a cratemover-written stick (only
   `export.pdb` + ANLZ). It then:
   - rewrote `export.pdb`;
   - added `exportExt.pdb`, `exportLibrary.db` (+`-wal`/`-shm` while open), `DEVSETTING.DAT`,
     `MYSETTING.DAT`, `MYSETTING2.DAT`, `DJMMYSETTING.DAT`, `djprofile.nxs` and
     `extracted/gcred.dat`.

   It also asked whether to use the device with rekordbox. On a Serato-only stick, answering yes
   led to "Unexpected application error" [session; docs/usb-compatibility.md].

6. **Closing rekordbox rewrites both `.pdb` files** and folds the WAL into `exportLibrary.db`
   [C9YD: all three hashes changed, `-wal`/`-shm` gone].
7. **Edits are saved straight away.** Moving a track's main cue rewrote that track's
   `.DAT`/`.EXT` within a minute, stored as a memory cue. Other tracks' files were untouched
   [session].
8. **Deleted rows stay in a fresh export** (§2.2). rekordbox seems to update rows in place as it
   builds the export: the history row was rewritten 21 times.
9. **`brokendb`.** In issue #1, cratemover had:
   - rewritten `export.pdb`;
   - moved `exportExt.pdb` aside;
   - left backup files in `PIONEER/rekordbox`;
   - left a stale OneLibrary.

   rekordbox crashed and wrote a 1-byte `PIONEER/rekordbox/brokendb` [cited #1]. baken hit a
   similar stale-OneLibrary problem (baken #208) [cited]. Which of these changes caused it is
   still untested (`docs/usb-compatibility.md` §3).

---

## 9. Version-specific values

| Value                                 | rekordbox 6.8.6 [6.8.6]          | rekordbox 7.2.19 [UFC8, C9YD]                                                    |
| ------------------------------------- | -------------------------------- | -------------------------------------------------------------------------------- |
| OneLibrary `property.dbVersion`       | `'10000'`                        | `'1000'`                                                                         |
| `.2EX` sections                       | PPTH PWV7 PWV6 PWVC              | PPTH PWV7 PWV6 PWVC **PVDI**                                                     |
| `extracted/gcred.dat`                 | absent                           | present                                                                          |
| `DEVSETTING.DAT`                      | present (version string `6.8.6`) | absent after a plain export; written on opening a stick that lacked it [session] |
| `export.pdb` table 0x13 row           | none                             | `u32 0x0280 …, date, "1000", device`                                             |
| Track string 7 / `isHotCueAutoLoadOn` | `ON` / 1                         | `''` / 0. Probably a user preference, not the version.                           |
| `export.pdb` header +0x10             | 5                                | 5                                                                                |
| OneLibrary schema                     | same                             | same                                                                             |
| OneLibrary `cue` table                | empty                            | empty                                                                            |

The `DEVSETTING.DAT` version string is the rekordbox version that wrote it. The other settings
files carry format versions (`0.001`, `1.000`), not the app version.

---

## 10. What cratemover writes vs rekordbox

cratemover (`usb.py`) writes `Contents/` (only when it has to copy or transcode), `export.pdb`,
ANLZ `.DAT/.EXT/.2EX`, and optionally `exportLibrary.db`. Gaps and differences:

| Item                                    | rekordbox 7.2.19                                       | cratemover                                      | Risk                                                                          |
| --------------------------------------- | ------------------------------------------------------ | ----------------------------------------------- | ----------------------------------------------------------------------------- |
| `exportExt.pdb`                         | written                                                | moved aside (`*.cratemover-<stamp>`)            | Unknown. May relate to #1.                                                    |
| Backups in `PIONEER/rekordbox/`         | none                                                   | `export.pdb.cratemover-*` etc.                  | Unknown (#1).                                                                 |
| Settings / profile files                | written                                                | not written                                     | Probably low. rekordbox adds them on open.                                    |
| Artwork                                 | 4 files per id, linked from both databases             | none (#4)                                       | No artwork on players.                                                        |
| My Tags                                 | copied from the collection (`exportExt.pdb` + `myTag`) | 4 default categories, no tags                   | Tags lost.                                                                    |
| `content.dateAdded` (StockDate)         | real value                                             | file date (s10). s15 may hold StockDate (§11).  | Wrong "date added" sort.                                                      |
| `myTagMasterDBID`                       | collection id                                          | random                                          | Unknown.                                                                      |
| `createdDate` / history date            | UTC date                                               | local date                                      | Cosmetic.                                                                     |
| Track strings 2/3/4/7                   | `"2"`, `"1"`, cue counter, `""`                        | `"1"`, `"1"`, `"1"`, `"ON"`                     | Unknown.                                                                      |
| Deleted rows / index entries            | present                                                | none                                            | None expected: a cleaner file.                                                |
| PVBR seek table, PCPT trailing 16 bytes | filled for some files                                  | zeros                                           | rekordbox itself writes zeros for 44.1 kHz files, so zeros are probably fine. |
| PQT2                                    | full                                                   | empty (header only)                             | CDJ-3000 extended grid missing. Probably falls back to PQTZ.                  |
| PVDI                                    | present                                                | absent                                          | Unknown.                                                                      |
| Contents naming                         | `UnknownArtist/UnknownAlbum`, stem ≤ 44                | `Unknown Artist/Unknown Album`, stem ≤ 100, NFC | Paths are stored in full, so probably harmless.                               |
| OneLibrary `image_id` without art       | NULL                                                   | 0                                               | Low.                                                                          |

**MP3 timing** (`src/cratemover/offsets.py`): Serato and rekordbox are believed to place
positions one MPEG frame (~26 ms) apart for MP3s that have a Xing/Info header but no valid LAME
tag. 17 of 21 UFC8 files are in that case (Xing, no LAME). This comes from decoder analysis and
is not verified on hardware or in both programs side by side.

---

## 11. Open questions

1. **Is `export.pdb` string 15 really "analyze date"?** It equals OneLibrary `dateAdded`
   (StockDate) on all 24 tracks we have. These tracks were probably analysed on the day they
   were added, so this doesn't prove anything. Test: re-analyse an old track and re-export. If
   s15 doesn't change, it is StockDate, and cratemover can fill `dateAdded` correctly when
   converting.
2. Track strings 2, 3 and 4: are they `analysisDataUpdateCount`, `informationUpdateCount` and
   `cueUpdateCount`? (OneLibrary leaves those NULL.)
3. What turns on the PVBR seek table and the PCPT trailing fields? Not sample rate alone: one
   48 kHz file has none. Maybe VBR detection. What are the units of `b`?
4. PVDI meaning, and whether any player needs it. PWVC meaning. The PQT2 layout beyond the
   header.
5. What the `export.pdb` index-page entries mean (they list pages holding deleted rows), and
   header field +0x10 (= 5).
6. `exportExt.pdb` table 7 row: the other five (empty) strings.
7. `gcred.dat`: what it is, and whether anything reads it.
8. Do players need `exportExt.pdb`, the settings files or `djprofile.nxs`? Which change in #1
   made rekordbox write `brokendb`?
9. Does rekordbox cut file stems to 44 characters, or did the source files already have those
   names?
10. Do real players (not Wine) accept cratemover's sticks, especially its OneLibrary? No hardware
    test yet.
11. Non-MP3 formats: none of the above is checked for AAC/FLAC/WAV/AIFF.

---

## Sources

- AlphaTheta, _Important notice for customers using USB devices_:
  https://alphatheta.com/en/information/important-notice-for-customers-using-usb-devices-with-our-dj-equipment/
- rekordbox FAQ, OneLibrary: https://rekordbox.com/en/support/faq/onelibrary-7/
- rekordbox USB export guide (2025-10):
  https://cdn.rekordbox.com/files/20251021171528/USB_export_guide_en_251007.pdf
- rekordbox release notes 6.6.11, 6.8.1, 7.2.5 (rekordbox.com)
- Deep Symmetry, rekordbox export analysis: https://djl-analysis.deepsymmetry.org/
  (`exports.html`, `anlz.html`)
- pyrekordbox (SQLCipher keys, OneLibrary): https://github.com/dylanljones/pyrekordbox
- rekordcrate (PDB, ANLZ, settings files): https://github.com/Holzhaus/rekordcrate
- crate-digger: https://github.com/Deep-Symmetry/crate-digger
- baken (export writer, constants): https://github.com/M-Igashi/baken, issue #208:
  https://github.com/M-Igashi/baken/issues/208
- rekordbox-pdb: https://github.com/fragmede/rekordbox-pdb
- rekordlib (6.8.6 test export): https://github.com/acrilique/rekordlib
- DJCrate issue #41 (empty `cue` table): https://github.com/fotoner/DJCrate/issues/41
- cratemover issue #1: https://github.com/lewinfox/cratemover/issues/1
- This repo: `src/cratemover/pioneer/` (`pdb.py`, `anlz.py`, `waveform.py`, `onelibrary.py`,
  `keys.py`, `usb.py`), `src/cratemover/offsets.py`, `docs/usb-compatibility.md`,
  `tests/fixtures/README.md`
