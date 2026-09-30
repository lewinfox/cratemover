# Test fixtures

Real files written by the DJ programs, used to check the parsers against more than our own output.

| Path | What | Source (licence) |
|------|------|------------------|
| `serato-db/database_v2_test.bin`, `database_v2_duplicates.bin` | Serato `database V2` files | [bvandercar-vt/serato-tools](https://github.com/bvandercar-vt/serato-tools) `test/data` @ `e24000e` (MIT) |
| `serato-db/TestCrate.crate`, `TestSmartCrate.scrate` | A Serato crate and smart crate | same |
| `serato-tags/{mp3,mp4,flac,ogg}/…` | Serato `Markers2`, `Markers_` and `BeatGrid` tag payloads, one feature each | [mixxxdj/mixxx](https://github.com/mixxxdj/mixxx) `src/test/serato/data` @ `5fefde0` (GPL-2.0-or-later), originally from [Holzhaus/serato-tags](https://github.com/Holzhaus/serato-tags) (MIT) |
| `serato-tags/real-*.bin` | The three Serato GEOB frames of a real analysed track with 6 hot cues and a 2-marker grid | extracted from serato-tools `test/data/test_mp3.mp3` (MIT) |
| `rekordbox/rekordbox5-database.xml`, `rekordbox6-database.xml` | Rekordbox XML exports | [dylanljones/pyrekordbox](https://github.com/dylanljones/pyrekordbox) `.testdata` @ `0416d1d` (MIT) |
| `mixxx_schema.xml` | *(in the package)* Mixxx's `res/schema.xml` | Mixxx (GPL-2.0-or-later) |

Audio for the round-trip tests is generated at test time with ffmpeg (`tests/fakelib.py`).
| `rekordbox/anlz/demo-track-1.DAT` | A Rekordbox analysis file (beat grid, cues, waveform) for "Demo Track 1" | [Holzhaus/rekordcrate](https://github.com/Holzhaus/rekordcrate) `data/complete_export/demo_tracks` (MPL-2.0) |
| `rekordbox/stick-6.8.6/PIONEER/…` | A real Rekordbox 6.8.6 USB export: `export.pdb`, `exportExt.pdb`, OneLibrary `exportLibrary.db`, analysis files and settings for 2 tracks (artwork left out) | [acrilique/rekordlib](https://github.com/acrilique/rekordlib) `testdata/complete_export/with_anlz` @ `7802a31` (MPL-2.0) |
| `rekordbox/export-3886-tracks.pdb.gz` | A large real `export.pdb` (3,886 tracks, 104 playlists, pages with deleted rows) | [Holzhaus/rekordcrate](https://github.com/Holzhaus/rekordcrate) `data/pdb/num_rows` @ `14d54ed` (MPL-2.0) |
| `rekordbox/one-song-export.pdb` | A one-track `export.pdb` | [fragmede/rekordbox-pdb](https://github.com/fragmede/rekordbox-pdb) `tests/data` @ `ee3bac2` (MIT) |
