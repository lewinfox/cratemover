# dj-library-converter

Convert DJ libraries between **Mixxx**, **Rekordbox** and **Serato**: tracks and their metadata,
playlists, crates, hot cues, memory cues, loops and beat grids. It has a web UI (Docker) and a
command line.

| From \ To      | Mixxx | Rekordbox XML | Serato |
|----------------|:-----:|:-------------:|:------:|
| Mixxx          |   ✓   |       ✓       |   ✓    |
| Rekordbox XML  |   ✓   |       ✓       |   ✓    |
| Serato         |   ✓   |       ✓       |   ✓    |

Everything goes through one format-neutral model (`src/djconvert/model.py`), so every
reader works with every writer.

## Run it

### Docker (web UI)

```sh
mkdir -p export sources
docker compose up --build        # then open http://localhost:8000
```

`compose.yaml` mounts:

| Variable       | Default     | Mounted at                | What                                                        |
|----------------|-------------|---------------------------|-------------------------------------------------------------|
| `MIXXX_DIR`    | `~/.mixxx`  | `/sources/mixxx` (ro)     | Mixxx's settings folder with `mixxxdb.sqlite`               |
| —              | `./sources` | `/sources/files` (ro)     | Drop a `rekordbox.xml`, `mixxxdb.sqlite` or `_Serato_` here |
| `MUSIC_DIR`    | `~/Music`   | **the same path** (ro)    | Your music (a Mac's `~/Music/_Serato_` comes with it)       |
| `MUSIC_ACCESS` | `ro`        |                           | Set to `rw` to let it write Serato tags into the files      |
| `EXPORT_DIR`   | `./export`  | `/export`                 | Where conversions are written                               |
| `PORT`         | `8000`      | 127.0.0.1                 |                                                             |
| `UID`/`GID`    | `1000`      |                           | User the container writes files as                          |

```sh
MUSIC_DIR=/data/music MUSIC_ACCESS=rw docker compose up --build
```

**Music is mounted at its real path** because libraries store absolute paths. If your library
spans several folders, add a volume line per folder. You can also upload a `rekordbox.xml`,
`mixxxdb.sqlite` or a zip of a `_Serato_` folder in the UI.

In networks that rate-limit Docker Hub: `docker compose build --build-arg
PYTHON_IMAGE=mirror.gcr.io/library/python:3.13-slim-bookworm`.

### Command line (uv)

```sh
uv run djconvert inspect --from serato ~/Music/_Serato_
uv run djconvert convert --from mixxx ~/.mixxx --to rekordbox_xml out/
uv run djconvert convert --from rekordbox_xml rekordbox.xml --to serato out/ \
    --path 'C:/Users/me/Music=>/Users/me/Music' --write-serato-tags
uv run djconvert convert --help
uv run djconvert serve           # the web UI on http://127.0.0.1:8000
```

## Using it

1. **Source.** Pick the format and the library (`mixxxdb.sqlite`; a Rekordbox XML from
   *File › Export Collection in xml format*; or a `_Serato_` folder). For Serato, say which drive
   root its paths are relative to (`/` for `~/Music/_Serato_` on a Mac, `C:/` on Windows, the
   mount point for an external drive's own `_Serato_`).
2. **Look.** Stats, warnings (e.g. files it cannot see), the playlist tree, and per-track cues
   and grids. Tick playlists to convert only those.
3. **Target.** Rewrite paths if the target runs elsewhere (`/home/me/Music => /Users/me/Music`),
   choose the key notation and format options, convert, download.

Then import:

- **Rekordbox:** *Preferences › Advanced › Database › rekordbox xml*, choose the file; the
  playlists appear under *rekordbox xml*; right-click › *Import Playlist*.
- **Serato:** quit Serato, back up `_Serato_`, copy the generated `database V2` and `Subcrates`
  into it. Cues/grids arrive only if you let it write the tags into the audio files. Give it your
  existing `database V2` as a merge base to keep tracks that aren't in the conversion.
- **Mixxx:** quit Mixxx, back up `mixxxdb.sqlite`, replace it with the generated one. Give it
  your existing database as a merge base to add to it rather than replace it.

## What converts, and how

| Thing              | Mixxx                         | Rekordbox XML                   | Serato                                  |
|--------------------|-------------------------------|---------------------------------|-----------------------------------------|
| Hot cues           | 36 slots (name, colour)       | A–H (name, colour)              | 8 or 16 slots, in file tags             |
| Memory cues        | *none*: into free hot slots 9+| memory cues                     | *none*: into free hot cue slots         |
| Hot loops          | hot loops                     | hot cue loops A–H               | hot cue + saved loop                    |
| Saved loops        | hot loops 9+                  | memory loops                    | 8 saved loops                           |
| Main cue, intro, outro | own cue types             | memory cues                     | hot cue slots, if free                  |
| Beat grid          | constant grid or beat map     | `TEMPO` sections                | beat grid markers                       |
| Playlists/crates   | playlists + flat crates       | folders + playlists             | crates with sub-crates (`A%%B.crate`)   |
| Key                | key id                        | Camelot/Open Key/musical        | text                                    |
| Track colour       | any RGB                       | nearest of 8                    | nearest of Serato's palette             |
| Rating, plays, date added, comment, genre, label, … | ✓ | ✓                     | ✓ (no rating in Serato)                 |

Not converted: Serato smart crates, Flip, history; Rekordbox My Tags, hot cue banks, history;
Mixxx Auto DJ and history playlists; artwork and waveforms (each program regenerates those).

**Timing.** All positions are kept on one reference timeline: how Rekordbox and Serato decode the
file. Mixxx skips an MP3's encoder delay differently depending on its LAME/Xing header (~26 ms
with MAD/FFmpeg, up to 50 ms with CoreAudio) and skips AAC priming samples; the Mixxx reader and
writer correct for both, which needs the audio files to be reachable. Tell it which MP3 decoder
your Mixxx uses (MAD on Linux/Windows builds, CoreAudio on macOS).

**Serato data lives in the audio files.** Serato's database only has metadata and paths. Cue
points, loops and beat grids are `Serato Markers2`, `Serato Markers_` and `Serato BeatGrid` tags
inside each MP3/AIFF/WAV (ID3 `GEOB`), FLAC and Ogg (Vorbis comments) and M4A (freeform atoms).
Reading Serato therefore needs the music mounted; writing Serato cues needs it mounted writable.
Existing Serato Flip data in a file is kept.

## Develop

```sh
uv sync --all-extras
uv run pytest                    # generates test audio with ffmpeg; those tests skip without it
uv run ruff check && uv run ruff format --check
uv run python tests/fakelib.py /tmp/demo   # a demo Mixxx library + music to point the app at
```

Layout: `model.py` (the shared model), `mixxx.py`, `rekordbox_xml.py`, `serato/` (`binfile`
for `database V2`/crates, `markers` for the tag payloads, `tags` for audio-file I/O, `library`),
`offsets.py` (decoder offsets), `grid.py`, `keys.py`, `colours.py`, `convert.py` (read → remap
paths → write), `cli.py`, `web/`. Tests use real files from Serato, Rekordbox and Mixxx's test
suite; see `tests/fixtures/README.md`.

Format sources: [Holzhaus/serato-tags](https://github.com/Holzhaus/serato-tags),
[bvandercar-vt/serato-tools](https://github.com/bvandercar-vt/serato-tools), Mixxx's
`src/track/serato/` and `src/library/serato/`, AlphaTheta's Rekordbox XML format list, and
[pyrekordbox](https://github.com/dylanljones/pyrekordbox). Decoder offsets and Mixxx database code
are adapted from [`../mixxx-to-rekordbox`](../mixxx-to-rekordbox) (copied, not shared). GPL-3.0.

## Open questions

Things not yet verified against the real programs:

- Serato has only been tested against sample files, not a running Serato. Unknown: whether it
  accepts a `database V2` that omits fields it normally writes (length, bitrate, size), the
  meaning of `ulbl` (assumed track colour) and `utpc` (assumed play count), and whether it reads
  ID3 tags in WAV files.
- Whether Serato and Rekordbox place cues identically on MP3s without a LAME header, and on AAC
  (both are assumed to match the reference timeline).
- Serato beat grid markers are assumed to sit on downbeats; the first marker is moved to one.
