# USB stick compatibility

Goal: know what Rekordbox, Serato and cratemover each accept as a valid USB stick, then make
cratemover never write a stick that one of them rejects. Started from
[#1](https://github.com/lewinfox/cratemover/issues/1) (cratemover broke a Rekordbox export).

## Setup

| Software   | Version                    | How it runs             |
| ---------- | -------------------------- | ----------------------- |
| Rekordbox  | 7.2.19                     | Wine in Docker on Linux |
| Serato DJ  | 4.0.10                     | Wine in Docker on Linux |
| Mixxx      | 2.5.6                      | native Linux            |
| cratemover | branch / commit under test | `uv run cratemover`     |

The Wine builds may not behave exactly like the real Mac/Windows ones. Treat a failure here as
"probably fails", a pass as "probably works".

Stick: 32 GB, reset before every test with `scripts/wipe-usb /dev/sdX` (one MBR partition, FAT32).

## Plan

1. **Write from X, read in Y.** Fill in the table below. Each cell: start from a wiped stick,
   write with the row's tool, open with the column's tool.
2. **Fix** every cratemover row until it's all ✅.
3. **What breaks a good stick.** Starting from a stick that works everywhere, try one change at a
   time (below) and record who rejects it.
4. **Safeguards.** Turn step 3 into checks in cratemover: refuse, or warn, before writing.

## 1. Write from X, read in Y

✅ works · ⚠️ partly (say what's missing) · ❌ rejected (say how) · — not tested yet

| Written by ↓ / read in →                               | Rekordbox | Serato | Mixxx | cratemover |
| ------------------------------------------------------ | :-------: | :----: | :---: | :--------: |
| Rekordbox export                                       |    OK     |  N/A   |  OK   |     OK     |
| Serato (drag crates to the stick and "Copy")           |    N/A    |   OK   |  OK   |     OK     |
| cratemover: Mixxx → Rekordbox USB                      |     —     |   —    |   —   |     —      |
| cratemover: Mixxx → Serato                             |     —     |   —    |   —   |     —      |
| cratemover: convert Rekordbox stick → + Serato         |    N/A    |   —    |   —   |     —      |
| cratemover: convert Serato stick → + Rekordbox         |    OK     |  N/A   |  OK   |     OK     |
| cratemover: sync into an existing Rekordbox stick (#1) |     —     |   —    |   —   |     —      |

Notes:

- Serato -> RB: RB prompted "use this device with Rekordbox?" which I think means "write the folders I need?".
  Yes -> "Unexpected application error". This may be normal! The USB now contains `PIONEER/`,
  `_Serato_` and `test/` (test is the crate exported from Serato). This smells like our original
  issue, and I think it's "Rekordbox can't handle other files on the USB".
- Serato -> Serato: "Serato library on your drive failed to open and will not appear in your library".

What "works" means for each reader:

- **Rekordbox:** stick shows under Devices, playlists open, tracks play, cues and grid show; no
  `PIONEER/rekordbox/brokendb` file appears.
- **Serato:** stick shows in the library, crates open, tracks play, cues show.
- **Mixxx:** the Rekordbox (and Serato) library on the stick shows up and plays with cues.
- **cratemover:** `cratemover inspect` reads it with no warnings.

### Notes

<!-- One entry per test: date, cell, what happened, files kept as evidence. -->

- **2026-10-01, Serato setup:** Serato 4 writes cue points into the audio file only when the
  track is unloaded from the deck (or Serato closes). Moving a still-loaded track to a stick left
  it with no cues. Unload before moving or exporting.
- **2026-10-01, Serato setup:** Serato 4 keeps its library in
  `AppData/Local/Serato/Library/master.sqlite` (and `location.sqlite` on a stick), as well as the
  old `_Serato_/database V2`. Cues are not in either database, only in the file's tags.
- **2026-10-01, Serato setup:** Serato saves crates in two places. Old-style: the `_Serato_`
  folder (`database V2` plus one `Subcrates/*.crate` file per crate), which is all Serato used
  before version 4 and all cratemover reads. New: Serato 4's `master.sqlite`. Serato 4 updates
  `master.sqlite` at once but rewrites the old-style files only later (seen: a track added to a
  crate at 22:39 was still missing from its `.crate` file). So reading `_Serato_` while Serato is
  open can give stale crates. Close Serato before converting.
- **2026-10-01, Serato → Rekordbox (cratemover convert):** Rekordbox 7.2.19 read the converted
  stick fine. On opening it, Rekordbox wrote to the stick by itself: rewrote `export.pdb`, and
  added `exportExt.pdb`, a OneLibrary database (`exportLibrary.db`, with `-wal`/`-shm` while
  open), `DEVSETTING.DAT`, `MYSETTING.DAT`, `MYSETTING2.DAT`, `DJMMYSETTING.DAT`,
  `djprofile.nxs` and `extracted/gcred.dat`. So it showed a OneLibrary section although we didn't
  write one.
- **2026-10-01, Rekordbox track lifecycle:** unlike Serato, Rekordbox saves edits to a stick's
  tracks straight away. Moving Uptown Funk's main cue rewrote that track's analysis files
  (`.DAT`/`.EXT`) within a minute, as a memory cue at the new spot; the other track's files were
  untouched. Rekordbox keeps cues in the analysis files and its databases, not in the audio.
- **2026-10-01, Rekordbox export (stick C9YD, 3 tracks, memory cues):** rekordbox 7.2.19 writes
  both `export.pdb` and OneLibrary (`exportLibrary.db`). OneLibrary's `cue` table stays empty
  even with cues set: cues are only in the analysis files. Compared with cratemover's OneLibrary
  for the same `export.pdb`, the schema is identical. Aligned to rekordbox 7.2.19:
  `property.dbVersion` `1000` (a rekordbox 6.8.6 export had `10000`, so it depends on the
  version), `createdDate` set, `isHotCueAutoLoadOn` 0, missing album or label `NULL` not 0,
  album artist set. Still different: artwork (#4); My Tags (rekordbox copies the user's tags;
  we write only the 4 default categories); `myTagMasterDBID` (identifies the user's collection;
  ours is random); `content.dateAdded`, which rekordbox takes from the collection's
  `StockDate` (date added to the collection) while `export.pdb` holds `DateCreated`. Converting
  from `export.pdb` can't recover `StockDate`.
- **2026-10-02, Serato crate Copy with a shared track (stick FX5Z):** crates `test_crate_1` and
  `test_crate_2` shared one track (HUGEL). Copying both crates to the stick put HUGEL in *both*
  folders (`test_crate_1/` and `test_crate_2/`, identical files), and then each crate listed
  *both* copies, in the `.crate` files and in Serato 4's stick database alike: one track, two
  files, four crate entries. Probably a Serato bug (it seems to treat the two copies as one
  track). cratemover deliberately departs from this when it relocates audio for Serato: a
  shared track is stored once, in its first crate's folder, and listed once per crate. See
  `src/cratemover/layout.py` and #5.
- **2026-10-02, Rekordbox file names:** rekordbox cuts a file's stem to 44 characters when it
  exports (checked against the originals: an 81-character name became 44, mid-word, trailing
  space kept). cratemover's relocation does the same. What it does with two names that cut to
  the same thing is still untested.
- **2026-10-02, round trip Rekordbox → Serato → Rekordbox (stick 9HKT): `brokendb` cause
  found.** rekordbox 7.2.19 crashed ("Unexpected application error") and wrote `brokendb` on the
  stick cratemover converted back. Swap tests: rekordbox's original `export.pdb` with our analysis
  files loaded fine (A); our `export.pdb` with only its three key names changed from musical
  (`Cm`, `Bb`, `Abm`) to Camelot (`5A`, `6B`, `1A`), 8 bytes, loaded fine (C). So rekordbox
  rejects musical key names in a stick's key table. cratemover now always writes Camelot there,
  as rekordbox's own exports do. Earlier converted sticks loaded because they had no keys. The
  round trip itself kept tracks, playlists, paths, audio frames, cue positions and grids (beats
  within 0.007 ms); memory cues came back as hot cues (warned), and Serato DJ Lite showed only
  hot cues 1-4 of 6 (it has 4 slots; all 6 stay in the file).

## 3. What breaks a good stick

| Change                                                        | Rekordbox | Serato |
| ------------------------------------------------------------- | :-------: | :----: |
| `exportExt.pdb` missing (what happened in #1)                 |     —     |   —    |
| `exportLibrary.db` (OneLibrary) out of step with `export.pdb` |     —     |   —    |
| `export.pdb` rewritten, analysis files unchanged              |     —     |   —    |
| Backup files (`*.cratemover-*`) left in `PIONEER/rekordbox/`  |     —     |   —    |
| Extra folders at the stick root                               |     —     |   —    |
| exFAT instead of FAT32                                        |     —     |   —    |
| GPT instead of MBR                                            |     —     |   —    |

Add rows as we find more.

## 4. Safeguards to build

<!-- Filled in from step 3. -->
