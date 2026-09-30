# dj-library-converter

Read, write, convert and sync DJ libraries between **Mixxx**, **Rekordbox** (USB sticks, XML and
Rekordbox 6/7's own library) and **Serato** (on the computer or a USB stick): tracks, playlists,
crates, hot cues, memory cues, loops and beat grids. It has a web UI (Docker) and a command line.

| From \ To                          | Mixxx | Rekordbox USB | Rekordbox XML | Serato |
|------------------------------------|:-----:|:-------------:|:-------------:|:------:|
| Mixxx                              |   ✓   |       ✓       |       ✓       |   ✓    |
| Rekordbox USB stick                |   ✓   |       ✓       |       ✓       |   ✓    |
| Rekordbox XML                      |   ✓   |       ✓       |       ✓       |   ✓    |
| Rekordbox 6/7 library (`master.db`)|   ✓   |       ✓       |       ✓       |   ✓    |
| Serato (computer or USB)           |   ✓   |       ✓       |       ✓       |   ✓    |

Everything goes through one format-neutral model (`src/djconvert/model.py`), so every reader works
with every writer, and **sync** can keep any two writable libraries in step (Rekordbox's own
`master.db` can only be read; sync into Rekordbox through a stick or XML).

## Run it

### Docker (web UI)

```sh
mkdir -p export sources
docker compose up --build        # then open http://localhost:8000
```

`compose.yaml` mounts:

| Variable        | Default               | Mounted at              | What                                                         |
|-----------------|-----------------------|-------------------------|--------------------------------------------------------------|
| `USB_DIR`       | `/media`              | **same path**, writable | Where USB sticks appear (`/run/media`, `/Volumes` on a Mac)  |
| `USB_MOUNT_MODE`| `rw,rslave`           |                         | `rslave` passes sticks plugged in later into the container    |
| `MIXXX_DIR`     | `~/.mixxx`            | `/sources/mixxx`, writable | Mixxx's folder with `mixxxdb.sqlite`                     |
| `REKORDBOX_DIR` | `./sources/rekordbox` | `/sources/rekordbox` (ro) | Rekordbox 6/7's folder (`~/Library/Pioneer/rekordbox`, `%APPDATA%\Pioneer\rekordbox`) |
| `MUSIC_DIR`     | `~/Music`             | **same path** (ro)      | Your music (a Mac's `~/Music/_Serato_` comes with it)        |
| `MUSIC_ACCESS`  | `ro`                  |                         | `rw` lets it write Serato cue tags into your music files     |
| —               | `./sources`           | `/sources/files` (ro)   | Drop a `rekordbox.xml`, `mixxxdb.sqlite` or `_Serato_` here  |
| `EXPORT_DIR`    | `./export`            | `/export`               | Where new libraries are written                              |
| `PORT`, `UID`/`GID` | `8000`, `1000`    |                         | Port on 127.0.0.1; the user files are written as             |

```sh
USB_DIR=/run/media MUSIC_DIR=/data/music docker compose up --build
```

Music and sticks are mounted **at their real paths** because libraries store absolute paths. If
your library spans several folders, add a volume line per folder. The image is ~32 MB to download
(128 MB unpacked): Alpine Python, the runtime wheels and a 2 MB audio-only ffmpeg (for waveforms
and MP3 conversion). In networks that rate-limit Docker Hub, add `--build-arg
PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-alpine3.22` to the build.

### Command line (uv)

```sh
uv run djconvert inspect --from rekordbox_usb /media/me/STICK
uv run djconvert convert --from mixxx ~/.mixxx --to rekordbox_usb /media/me/STICK
uv run djconvert convert --from serato /media/me/STICK --serato-root /media/me/STICK \
    --to rekordbox_usb /media/me/STICK                       # Serato stick -> CDJ stick, same audio
uv run djconvert sync --a-format mixxx ~/.mixxx --b-format rekordbox_usb /media/me/STICK --dry-run
uv run djconvert sync --a-format mixxx ~/.mixxx --b-format serato ~/Music/_Serato_ --direction a_to_b
uv run djconvert serve                                        # the web UI on http://127.0.0.1:8000
uv run djconvert convert --help                               # every option
```

## USB sticks

**Hot-plugging.** The *USB drives* panel at the top of the web UI lists every drive mounted under
`USB_DIR`, what's on it (Rekordbox, Serato, nothing yet), its filesystem and free space, and
updates within 3 s of a stick being plugged in or pulled out, with buttons to open it, sync it or
write a library onto it. It warns about NTFS sticks (rekordbox won't write to them), exFAT sticks
headed for older players (up to the CDJ-2000NXS2 only read FAT32) and read-only mounts.

This needs the host's mounts to reach the container: compose bind-mounts `USB_DIR` with `rslave`
propagation, so a stick your desktop auto-mounts after the container started appears inside it
(and disappears when ejected). That requires the host's `/` to be a *shared* mount, which systemd
distributions set up by default. If `docker compose up` fails with "not a shared or slave mount",
run `sudo mount --make-rshared /` (or set `USB_MOUNT_MODE=rw` and restart the container after
plugging a stick in). On macOS, Docker Desktop shares `/Volumes` as a folder rather than as
mounts; sticks that already hold a Rekordbox or Serato library are still found there, but a blank
stick only shows up in the file browser.

**Convert a stick in one go.** Plug the stick in, and under *Convert drive* press *Convert to
Rekordbox* or *Convert to Serato* (CLI: none yet; the web UI and `/api/drives/convert`). It:

1. backs up the stick's library folders (`PIONEER`, `_Serato_`) and, for every audio file whose
   Serato tags will change, the original tag values, to `BACKUP_DIR` (default `exports/backups`);
   tick *Full backup* to copy the whole stick instead. It checks there's room first;
2. reads the library on the stick and writes the other format onto the same stick, pointing at
   the audio already there (nothing is copied unless a format has to be transcoded, e.g. Ogg to MP3
   for Rekordbox), with cues and grids moved for each program's MP3 timing;
3. keeps the original library by default, so the stick works in both programs; untick *Keep the
   original* to remove it.

*Restore* on a backup puts the stick back as it was: library folders, Serato tags, and files the
conversion added removed. Nothing reformats or re-images the drive: each program only reads its
own library files, so rewriting those (byte for byte as that program writes them, for Rekordbox)
is the whole conversion, and the audio stays untouched.

**DDJ-400 / DDJ-FLX4 (the stick goes into the laptop).** These controllers have no USB port for a
stick, so what matters is what the laptop software does with it:

- **Mixxx** reads a stick's Rekordbox library (`export.pdb`, not OneLibrary) and plays straight from
  it, with hot cues, memory cues and the beat grid. It looks for sticks under `/media`,
  `/media/$USER`, `/run/media/$USER` and `/Volumes` (not `/mnt`).
- **Serato DJ Lite** (free with both controllers) shows a stick's `_Serato_` crates and plays from
  it. Lite has 4 hot cues per deck; cues 5–8 are kept but not shown. Serato DJ Pro/Lite 4.x moved to
  a new database; whether it picks up a stick's legacy `_Serato_` folder is unverified.
- **rekordbox** on a laptop shows a stick under *Devices* in Export mode but can't play from it in
  Performance mode: you import its playlists into your collection. Every Rekordbox stick this tool
  writes also gets a `rekordbox.xml` at its root for that; in rekordbox, add it under *Preferences
  → Advanced → rekordbox xml* and import the playlists from the *rekordbox xml* tree, which brings
  cues and grids with them. Set *rekordbox.xml paths* (e.g. `E:/` on Windows, `/Volumes/STICK` on a
  Mac) to how the laptop sees the stick, since the container sees it somewhere else. rekordbox 7
  deletes the play history from a stick after importing it unless you turn that off in its
  preferences.
- Format sticks **exFAT** (or FAT32 if they also go into older CDJs). Avoid NTFS: rekordbox won't
  write to it and macOS only reads it.

**Rekordbox (CDJ/XDJ).** Writing a stick builds `PIONEER/rekordbox/export.pdb` and, per track, the
`.DAT`/`.EXT`/`.2EX` analysis files (beat grid, hot and memory cues with names and colours,
waveforms) at the hashed paths players look for. It works in place on a mounted stick:

- audio already on the stick is used where it is, so a Serato or Mixxx library on the same stick
  converts without copying; other tracks are copied to `/Contents/<artist>/<album>/`;
- formats players can't play (Ogg, Opus, WMA…) are converted to 320 kbps MP3, with cues and grids
  moved 26 ms for the MP3 encoder delay;
- waveforms are reused from existing analysis files (the stick's own, or a Rekordbox library's),
  otherwise measured with ffmpeg (about 3 s per track, in parallel), otherwise flat placeholders;
- the previous `export.pdb` is kept as `export.pdb.djconvert-<time>`;
- **OneLibrary** (`exportLibrary.db`), which the CDJ-3000X, CDJ-1500X, OPUS-QUAD, OMNIS-DUO, XDJ-AZ
  and XDJ-AN need, is written when the stick already had one or when asked (`--onelibrary on`).
  This is experimental: nobody has published a hardware test of a third-party one. Otherwise a
  stale one is moved aside so it can't contradict the new `export.pdb`.

**Rekordbox and sticks written by other tools.** Using a stick in Rekordbox is what device
libraries are for, but there is one worrying report about sticks Rekordbox didn't write itself.
[rbsync](https://github.com/aquarazorda/rbsync) (a third-party stick writer) measured two things
with Rekordbox 7.2.6 on macOS:

- merely having a stick plugged in while Rekordbox runs changed one byte of its database header and
  added write-ahead-log files and macOS sidecar files (harmless bookkeeping);
- opening its self-written 5,825-entry stick in Rekordbox deleted 768 playlist entries across 30
  playlists. All the tracks survived; the memberships didn't. The cause isn't known.

That's a single report, about another tool's output, and not yet reproduced with this tool's sticks.
Until it is, keep the source library (this tool never modifies the source when writing a stick), and
if Rekordbox does damage a stick, write it again: that rebuilds `export.pdb` in seconds, reusing the
audio and waveforms already there. The previous `export.pdb` is also kept as a backup.

How it was checked: the reader agrees with an independent parser
([fragmede/rekordbox-pdb](https://github.com/fragmede/rekordbox-pdb), vendored in
`tests/oracles/`) on real exports up to 3,886 tracks; rewriting a real Rekordbox 6.8.6 stick
reproduces its colour, column, menu, genre, artist, album, playlist and entry pages byte for byte,
and its cue sections too; the analysis-folder hash matches Rekordbox's; pyrekordbox parses every
analysis file we write. The page-layout rules are ported from
[baken](https://github.com/M-Igashi/baken), whose sticks a CDJ-2000NXS2 has read. **This tool's
own sticks have not been tried on a player yet.**

**Serato.** A Serato library on a stick is a `_Serato_` folder at the stick's root with paths
relative to the stick: choose Serato and set "Serato paths relative to" to the stick's mount point
(the UI does this for sticks it finds). Cues and grids go into the audio files' tags.

## Sync

Sync reads two libraries, matches their tracks and writes the merged result back **in place**,
backing up every file it changes (`*.djconvert-<time>`). Quit the DJ software first. One way
(A → B or B → A) or both ways; *Preview* (`--dry-run`) reports what would change.

- **Matching**, strongest first: the same path (after path rules), then the same file name and
  size, then the same artist and title with lengths within 2 s, then a unique file name. So a
  track copied onto a stick still matches its original.
- **Tracks** the other side lacks are added (and copied onto sticks).
- **Cues**: *merge* (the winning side's cues plus the other's in slots and spots it leaves free),
  *replace*, or *fill* (only tracks with none). **Grids** and **tags**: *fill* or *replace*.
  **Playlists**: *merge* same-named ones (add missing tracks), *replace* them, or only *add* new ones.
  Nothing is deleted.
- Syncing twice in a row changes nothing: changes are judged by what a player shows (spots marked,
  hot cues A–H and their names), not by format details that can't survive the round trip.
- A matched MP3 and non-MP3 pair (a transcode made for a stick) has its cues shifted by the MP3
  encoder delay.

## What converts, and how

| Thing              | Mixxx                         | Rekordbox                       | Serato                                  |
|--------------------|-------------------------------|---------------------------------|-----------------------------------------|
| Hot cues           | 36 slots (name, colour)       | A–H (name, colour)              | 8 or 16 slots, in file tags             |
| Memory cues        | *none*: into free hot slots 9+| memory cues                     | *none*: into free hot cue slots         |
| Hot loops          | hot loops                     | hot cue loops A–H               | hot cue + saved loop                    |
| Saved loops        | hot loops 9+                  | memory loops                    | 8 saved loops                           |
| Main cue, intro, outro | own cue types             | memory cues ("Intro", "Outro")  | hot cue slots, if free                  |
| Beat grid          | constant grid or beat map     | tempo sections / per-beat grid  | beat grid markers                       |
| Playlists/crates   | playlists + flat crates       | folders + playlists             | crates with sub-crates (`A%%B.crate`)   |
| Key                | key id                        | Camelot/Open Key/musical text   | text                                    |
| Track colour       | any RGB                       | nearest of 8                    | nearest of Serato's palette             |
| Rating, plays, date added, comment, genre, label, … | ✓ | ✓                     | ✓ (no rating in Serato)                 |

Not converted: Serato smart crates, Flip, history; Rekordbox My Tags, hot cue banks, history,
artwork, phrase analysis; Mixxx Auto DJ and history playlists. Waveforms are only carried between
Rekordbox libraries (or measured); the other programs draw their own.

**Timing.** All positions are kept on one reference timeline, the way Rekordbox and Serato decode
the file. Mixxx skips an MP3's encoder delay differently depending on its LAME/Xing header (~26 ms
with MAD/FFmpeg, up to 50 ms with CoreAudio) and skips AAC priming samples; the Mixxx reader and
writer correct for both, which needs the audio files to be reachable. Tell it which MP3 decoder
your Mixxx uses (MAD on Linux/Windows builds, CoreAudio on macOS).

**Serato data lives in the audio files.** Serato's database only has metadata and paths. Cue
points, loops and beat grids are `Serato Markers2`, `Serato Markers_` and `Serato BeatGrid` tags
inside each MP3/AIFF/WAV (ID3 `GEOB`), FLAC and Ogg (Vorbis comments) and M4A (freeform atoms).
Reading Serato needs the music mounted; writing Serato cues needs it writable. Existing Flip data
in a file is kept.

**Rekordbox's own library.** `master.db` is SQLCipher-encrypted with a publicly known key (as are
OneLibrary databases); the `rekordbox` extra (`sqlcipher3`) opens them. It is copied before
reading, so Rekordbox can stay open. Beat grids come from the analysis files under `share/`, which
is why it wants the whole Rekordbox folder. Intelligent playlists are skipped.

## Develop

```sh
uv sync --all-extras             # web and rekordbox extras plus dev tools
uv run pytest                    # generates test audio with ffmpeg; those tests skip without it
uv run ruff check && uv run ruff format --check
uv run python tests/fakelib.py /tmp/demo   # a demo Mixxx library + music to point the app at
```

Layout (`src/djconvert/`): `model.py` (the shared model), `mixxx.py`, `rekordbox_xml.py`,
`rekordbox_db.py`, `pioneer/` (`pdb` for `export.pdb`, `anlz` for analysis files, `waveform`,
`onelibrary`, `keys` for the SQLCipher keys, `usb`), `devices.py` (drive detection), `serato/` (`binfile` for `database V2`/crates,
`markers` for the tag payloads, `tags` for audio-file I/O, `library`), `sync.py` (matching and
merging), `convert.py` (read → remap paths → write, in place or new; `sync_libraries`),
`offsets.py`, `grid.py`, `keys.py`, `colours.py`, `paths.py`, `cli.py`, `web/`. Tests use real
files from Serato, Rekordbox (6.6, 6.8 and a 3,886-track export) and Mixxx's test suite; see
`tests/fixtures/README.md`.

Format sources: [Deep Symmetry's Rekordbox analysis](https://djl-analysis.deepsymmetry.org/),
[baken](https://github.com/M-Igashi/baken), [rekordbox-pdb](https://github.com/fragmede/rekordbox-pdb),
[pyrekordbox](https://github.com/dylanljones/pyrekordbox), [rekordcrate](https://github.com/Holzhaus/rekordcrate),
[Holzhaus/serato-tags](https://github.com/Holzhaus/serato-tags),
[serato-tools](https://github.com/bvandercar-vt/serato-tools), Mixxx's `src/track/serato/` and
`src/library/serato/`, and AlphaTheta's Rekordbox XML format list. The research behind the USB
writer is in [`../mixxx-to-rekordbox/usb-analysis-plan.md`](../mixxx-to-rekordbox/usb-analysis-plan.md).
Decoder offsets and Mixxx database code are adapted from `../mixxx-to-rekordbox` (copied, not
shared). GPL-3.0.

## Open questions

Things not yet verified against real hardware or software:

- **Players.** No stick written by this tool has been on a CDJ/XDJ yet. Highest risk: older
  players (CDJ-350/900/2000, XDJ-1000MK2) that other projects' sticks failed on, multi-page tables
  (big libraries), and OneLibrary on the newest players.
- **Serato** has only been tested against sample files, not a running Serato: whether it accepts a
  `database V2` without the fields it normally writes (length, bitrate, size), what `ulbl` (assumed
  track colour) and `utpc` (assumed play count) mean, and whether it reads ID3 tags in WAV files.
- Whether Serato and Rekordbox place cues identically on MP3s without a LAME header, and on AAC.
- Serato beat grid markers are assumed to sit on downbeats; the first marker is moved to one.
- Rekordbox `master.db`: hot cue slots are read from `djmdCue.Kind` as 1, 2, 3, 5, 6, 7, 8, 9 for
  A–H, and cue colours from `ColorTableIndex`; both come from secondary sources.
- The 26 ms MP3 transcode shift was measured for FLAC → MP3 only.
