"""Build throwaway libraries with real (generated) audio for tests and demos.

Run as a script to create a demo Mixxx library and music folder::

    uv run python tests/fakelib.py /tmp/demo
"""

from __future__ import annotations

import array
import math
import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from djconvert.mixxx import create_database, encode_beatgrid, encode_beatmap

RATE = 44100


def click_track(path: Path, bpm: float, seconds: float, first_beat: float) -> None:
    """A stereo WAV with a click on every beat (accented downbeats)."""
    n = int(seconds * RATE)
    samples = array.array("h", bytes(4 * n))
    beat, t = 0, first_beat
    while t < seconds:
        start = int(t * RATE)
        freq, amp = (1500, 26000) if beat % 4 == 0 else (1000, 14000)
        for k in range(min(int(0.03 * RATE), n - start)):
            v = int(amp * math.exp(-k / (0.008 * RATE)) * math.sin(2 * math.pi * freq * k / RATE))
            samples[2 * (start + k)] = samples[2 * (start + k) + 1] = v
        beat += 1
        t += 60.0 / bpm
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())


CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "2"],
    ".m4a": ["-c:a", "aac", "-b:a", "128k"],
    ".aiff": ["-c:a", "pcm_s16be"],
}


def make_audio(path: Path, bpm: float = 120, seconds: float = 8, first_beat: float = 0.25) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".wav":
        click_track(path, bpm, seconds, first_beat)
        return path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is needed to create non-WAV audio")
    wav = path.with_name(path.stem + ".tmp.wav")
    click_track(wav, bpm, seconds, first_beat)
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav),
            *CODECS[path.suffix],
            str(path),
        ],
        check=True,
    )
    wav.unlink()
    return path


@dataclass
class FakeCue:
    type: int  # Mixxx cue type
    position: float  # seconds, -1 for unset
    hotcue: int = -1
    length: float = 0.0
    colour: int = 0xFF8000
    label: str = ""


@dataclass
class FakeTrack:
    filename: str
    title: str
    artist: str
    bpm: float = 120.0
    first_beat: float = 0.25
    seconds: float = 8.0
    beatmap: bool = False
    key_id: int = 22  # A minor
    rating: int = 3
    colour: int | None = 0xFF0000
    cues: list[FakeCue] = field(default_factory=list)
    missing: bool = False


def default_tracks() -> list[FakeTrack]:
    return [
        FakeTrack(
            "Alpha - First Light.mp3",
            "First Light",
            "Alpha",
            cues=[
                FakeCue(2, 0.25),  # main cue
                FakeCue(1, 0.25, hotcue=0, colour=0xC02626, label="Drop"),
                FakeCue(1, 4.25, hotcue=1, colour=0x00FF00),
                FakeCue(4, 2.25, hotcue=2, length=2.0, colour=0x0000FF, label="Loop"),
                FakeCue(6, 0.25, length=4.0),  # intro
                FakeCue(4, 6.25, length=1.0),  # saved loop
            ],
        ),
        FakeTrack(
            "Beta - Second Wind.flac",
            "Second Wind",
            "Beta",
            bpm=128,
            first_beat=0.1,
            key_id=8,
            colour=0x00FF00,
            cues=[FakeCue(1, 0.1, hotcue=0), FakeCue(1, 3.85, hotcue=5, label="Five")],
        ),
        FakeTrack(
            "Gamma - Drift.wav",
            "Drift",
            "Gamma",
            bpm=100,
            first_beat=0.5,
            beatmap=True,
            colour=None,
            cues=[FakeCue(1, 2.9, hotcue=3, label="Break")],
        ),
        FakeTrack(
            "Delta - Vorbis.ogg",
            "Vorbis",
            "Delta",
            bpm=124,
            cues=[FakeCue(1, 1.0, hotcue=0)],
        ),
        FakeTrack(
            "Epsilon - Apple.m4a",
            "Apple",
            "Epsilon",
            bpm=126,
            first_beat=0.3,
            cues=[FakeCue(1, 0.3, hotcue=0), FakeCue(1, 2.2, hotcue=1)],
        ),
        FakeTrack("Zeta - Gone.mp3", "Gone", "Zeta", missing=True),
    ]


def build_mixxx_library(root: Path, tracks: list[FakeTrack] | None = None) -> Path:
    """Create ``root/mixxx/mixxxdb.sqlite`` and audio under ``root/music``. Returns the db path."""
    tracks = default_tracks() if tracks is None else tracks
    music, mixxx = root / "music", root / "mixxx"
    mixxx.mkdir(parents=True, exist_ok=True)
    db_path = mixxx / "mixxxdb.sqlite"
    db_path.unlink(missing_ok=True)
    db = create_database(db_path)
    db.execute("INSERT INTO directories (directory) VALUES (?)", (str(music),))
    ids = []
    for t in tracks:
        path = music / t.filename
        if not t.missing:
            make_audio(path, t.bpm, t.seconds, t.first_beat)
        size = path.stat().st_size if path.exists() else 0
        loc = db.execute(
            "INSERT INTO track_locations (location, filename, directory, filesize, fs_deleted, "
            "needs_verification) VALUES (?, ?, ?, ?, 0, 0)",
            (str(path), path.name, str(path.parent), size),
        ).lastrowid
        if t.beatmap:
            times, x = [], t.first_beat
            while x < t.seconds:
                times.append(x)
                x += 60 / t.bpm if x < t.seconds / 2 else 60 / t.bpm / 1.04
            beats, version = encode_beatmap([round(s * RATE) for s in times]), "BeatMap-1.0"
        else:
            beats, version = encode_beatgrid(t.bpm, round(t.first_beat * RATE)), "BeatGrid-2.0"
        tid = db.execute(
            """INSERT INTO library (artist, title, album, year, genre, location, comment,
                   duration, bitrate, samplerate, bpm, channels, datetime_added, mixxx_deleted,
                   filetype, timesplayed, rating, key_id, beats, beats_version, color, tracknumber)
               VALUES (?, ?, 'Fixtures', '2024', 'Techno', ?, 'test', ?, 128, ?, ?, 2,
                   '2024-03-01T12:00:00.000Z', 0, ?, 2, ?, ?, ?, ?, ?, '1')""",
            (t.artist, t.title, loc, t.seconds, RATE, t.bpm, path.suffix[1:], t.rating,
             t.key_id, beats, version, t.colour),
        ).lastrowid  # fmt: skip
        ids.append(tid)
        for c in t.cues:
            pos = c.position * RATE * 2 if c.position >= 0 else -1
            db.execute(
                "INSERT INTO cues (track_id, type, position, length, hotcue, label, color) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tid, c.type, pos, c.length * RATE * 2, c.hotcue, c.label, c.colour),
            )

    def playlist(name: str, members: list[int], hidden: int = 0) -> None:
        pid = db.execute(
            "INSERT INTO Playlists (name, position, hidden) VALUES (?, 0, ?)", (name, hidden)
        ).lastrowid
        for pos, idx in enumerate(members, start=1):
            db.execute(
                "INSERT INTO PlaylistTracks (playlist_id, track_id, position) VALUES (?, ?, ?)",
                (pid, ids[idx], pos),
            )

    if len(ids) >= 6:
        playlist("Warm Up", [1, 0, 2])
        playlist("Peak Time", [0, 3, 4, 5])
        playlist("Auto DJ", [0], hidden=1)
        cid = db.execute("INSERT INTO crates (name) VALUES ('Techno')").lastrowid
        for idx in (0, 1, 3):
            db.execute(
                "INSERT INTO crate_tracks (crate_id, track_id) VALUES (?, ?)", (cid, ids[idx])
            )
    db.commit()
    db.close()
    return db_path


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo-library").resolve()
    print(build_mixxx_library(target))
