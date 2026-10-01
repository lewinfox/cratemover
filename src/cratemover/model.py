"""The format-neutral library model every reader produces and every writer consumes.

Times are milliseconds on the *reference timeline*: the audio as Rekordbox and
Serato decode it (encoder delay included). Mixxx decodes some files with a
different amount of leading audio, so the Mixxx reader and writer shift
positions on the way in and out (see :mod:`cratemover.offsets`).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath


class CueRole(StrEnum):
    CUE = "cue"  # a hot cue (slot set) or a memory cue (slot None)
    LOOP = "loop"  # a hot loop (slot set) or a saved/memory loop
    MAIN = "main"  # the main / load cue (Mixxx "cue", Rekordbox "load")
    INTRO = "intro"
    OUTRO = "outro"
    FADE_IN = "fade_in"
    FADE_OUT = "fade_out"


@dataclass
class Cue:
    role: CueRole
    position_ms: float
    end_ms: float | None = None  # loops, and intro/outro ranges
    slot: int | None = None  # 0-based hot cue slot (A=0), None for memory cues/loops
    name: str = ""
    colour: int | None = None  # 0xRRGGBB as displayed by the DJ software
    locked: bool = False  # Serato saved-loop lock

    @property
    def is_loop(self) -> bool:
        return self.role is CueRole.LOOP

    @property
    def is_hot(self) -> bool:
        return self.slot is not None


@dataclass
class TempoMarker:
    """A beat at ``position_ms`` from which ``bpm`` applies until the next marker.

    ``beat`` is that beat's number within its bar (1 = downbeat), as in the
    Rekordbox XML ``TEMPO Battito`` attribute. Bars are always 4/4.
    """

    position_ms: float
    bpm: float
    beat: int = 1


@dataclass(frozen=True)
class Key:
    """A musical key: ``tonic`` is a pitch class (0 = C, 1 = C#/Db, ... 11 = B)."""

    tonic: int
    minor: bool


@dataclass
class Track:
    id: str  # unique within one Library; readers use their source's own id
    location: str  # absolute path as the source software stores it (POSIX or Windows form)
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    composer: str = ""
    grouping: str = ""
    comment: str = ""
    label: str = ""
    remixer: str = ""
    year: str = ""
    track_number: int | None = None
    duration_s: float = 0.0
    sample_rate: int = 0
    bitrate: int = 0  # kbps
    file_size: int = 0
    bpm: float = 0.0
    bpm_locked: bool = False
    key: Key | None = None
    rating: int = 0  # 0-5 stars
    colour: int | None = None  # 0xRRGGBB
    play_count: int = 0
    date_added: date | None = None
    cues: list[Cue] = field(default_factory=list)
    grid: list[TempoMarker] = field(default_factory=list)
    # Format-specific extras that survive a conversion, e.g. "anlz": the path of
    # a Rekordbox analysis file whose waveforms can be reused.
    extra: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def filename(self) -> str:
        return path_name(self.location)

    @property
    def extension(self) -> str:
        name = self.filename
        return name.rsplit(".", 1)[-1].lower() if "." in name else ""

    @property
    def display_name(self) -> str:
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        return self.title or self.filename

    @property
    def hot_cues(self) -> list[Cue]:
        return sorted((c for c in self.cues if c.slot is not None), key=lambda c: c.slot or 0)


@dataclass
class Playlist:
    """A playlist or crate (``track_ids`` set) or a folder (``children`` set)."""

    name: str
    track_ids: list[str] | None = None
    children: list[Playlist] = field(default_factory=list)
    is_crate: bool = False  # came from a Mixxx/Serato crate rather than an ordered playlist

    @property
    def is_folder(self) -> bool:
        return self.track_ids is None

    @staticmethod
    def folder(name: str, children: list[Playlist] | None = None) -> Playlist:
        return Playlist(name, None, children or [])

    def walk(self, parents: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], Playlist]]:
        """Yield (path of folder names above it, node) for every playlist below this node."""
        for child in self.children:
            if child.is_folder:
                yield from child.walk((*parents, child.name))
            else:
                yield parents, child

    def count(self) -> int:
        return sum(1 for _ in self.walk())


@dataclass
class Library:
    source: str  # human-readable description of where it came from
    tracks: dict[str, Track] = field(default_factory=dict)
    playlists: Playlist = field(default_factory=lambda: Playlist.folder("ROOT"))
    warnings: list[str] = field(default_factory=list)

    def add_track(self, track: Track) -> Track:
        self.tracks.setdefault(track.id, track)
        return self.tracks[track.id]

    def summary(self) -> dict[str, int]:
        tracks = self.tracks.values()
        return {
            "tracks": len(self.tracks),
            "playlists": self.playlists.count(),
            "hot_cues": sum(1 for t in tracks for c in t.cues if c.slot is not None),
            "memory_cues": sum(1 for t in tracks for c in t.cues if c.slot is None),
            "gridded": sum(1 for t in tracks if t.grid),
        }


# --- paths --------------------------------------------------------------------


def is_windows_path(path: str) -> bool:
    return (len(path) >= 2 and path[1] == ":") or path.startswith("\\\\")


def path_name(path: str) -> str:
    return (PureWindowsPath(path) if is_windows_path(path) else PurePosixPath(path)).name


def normalise_path(path: str) -> str:
    """Forward slashes everywhere, so prefixes compare the same across OSes."""
    return path.replace("\\", "/")
