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
| Rekordbox export                                       |    OK     |   X    |  OK   |     OK     |
| Serato (drag crates to the stick and "Copy")           |     X     |   OK   |  OK   |     OK     |
| cratemover: Mixxx → Rekordbox USB                      |     —     |   —    |   —   |     —      |
| cratemover: Mixxx → Serato                             |     —     |   —    |   —   |     —      |
| cratemover: convert Rekordbox stick → + Serato         |     —     |   —    |   —   |     —      |
| cratemover: convert Serato stick → + Rekordbox         |     —     |   —    |   —   |     —      |
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
