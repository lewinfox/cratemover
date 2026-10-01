"""Where each DJ program puts audio files on a USB stick.

Converting a stick moves its audio to where the target program would have put it itself, so
the result looks as if that program wrote the stick (GitHub issue #5). The rules below copy
each program's own behaviour; each says what it's based on. Where a program's behaviour is
unknown or looks like a bug, the rule says so and says what we do instead.

**Rekordbox** (export, rekordbox 7.2.19; seen on sticks UFC8 and C9YD, see
``docs/rekordbox-usb-format.md``)

* ``Contents/<artist>/<album>/<name>``: the artist and album tags verbatim (non-ASCII kept),
  ``UnknownArtist`` / ``UnknownAlbum`` (no space) when the tag is empty;
* the file name's stem is cut to 44 characters, mid-word and keeping a trailing space
  (``[HouseU] Jerome Robins - Groovejet (If This .mp3`` from an 81-character original);
* **unverified:** what rekordbox does with characters FAT32 can't store (we use ``_``), whether
  the 44 counts characters or UTF-16 units (we count characters), and what it does when two
  files cut to the same name in one folder (we add `` (2)``, `` (3)``... before the extension).

**Serato** (Files panel, "Copy" a crate onto the stick; Serato DJ 4.0.10, 2026-10-01/02)

* ``<crate name>/<file name>``: a folder named after the crate, file names unchanged
  (crates ``test_crate_1``, ``test_crate_2``);
* **departure from Serato:** a track in two crates. Serato copies the file into *both* crate
  folders and then lists *both* copies in *both* crates, so each crate shows the track twice
  (stick FX5Z, 2026-10-02: one shared track became two files and four crate entries). That
  looks like a Serato bug, so we don't copy it: a shared track is stored once, in the folder
  of the first crate it's in (library order), and listed once in each of its crates;
* **unverified:** subcrates (we use the subcrate's own name as the folder) and tracks in no
  crate (Serato copies crates, so it never puts those on a stick; we leave them where they
  are).
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

from .model import Format, Library, Track

# Characters FAT32 can't store in a name.
_UNSAFE = re.compile(r'[\x00-\x1f"*/:<>?\\|]')
REKORDBOX_STEM_LIMIT = 44


def _fat_name(name: str) -> str:
    """A name FAT32 can store, in NFC (players look files up by the NFC string)."""
    return unicodedata.normalize("NFC", _UNSAFE.sub("_", name))


def rekordbox_path(track: Track, filename: str) -> PurePosixPath:
    """Where a rekordbox export puts this file, relative to the stick."""
    stem, suffix = os.path.splitext(filename)
    return PurePosixPath(
        "Contents",
        _fat_name(track.artist.strip()) or "UnknownArtist",
        _fat_name(track.album.strip()) or "UnknownAlbum",
        _fat_name(stem)[:REKORDBOX_STEM_LIMIT] + suffix,
    )


def serato_path(crate: str, filename: str) -> PurePosixPath:
    """Where Serato's crate Copy puts this file, relative to the stick."""
    return PurePosixPath(_fat_name(crate).strip() or "Unsorted", _fat_name(filename))


def first_crates(library: Library) -> dict[str, str]:
    """Each track's first crate (or playlist), in library order: track id -> its name."""
    first: dict[str, str] = {}
    for _, playlist in library.playlists.walk():
        for track_id in playlist.track_ids or []:
            first.setdefault(track_id, playlist.name)
    return first


def target_paths(
    library: Library, target: Format, local: dict[str, Path], occupied: set[str]
) -> dict[str, PurePosixPath]:
    """Where each track's audio goes on the stick (relative to ``root``) in ``target``'s
    layout: track id -> path. ``local`` maps track ids to their files on the stick now.
    Tracks that stay put (Serato: in no crate) are left out. Two different files that would
    land on the same path, or a path in ``occupied`` (lower-cased paths of other files on the
    stick), get `` (2)``, `` (3)``... before the extension."""
    crates = first_crates(library)
    wanted: dict[str, PurePosixPath] = {}
    taken: dict[str, Path] = {}  # lower-cased path -> the file going there (FAT32 ignores case)
    for track_id, file in sorted(local.items(), key=lambda item: str(item[1])):
        track = library.tracks[track_id]
        if target == Format.REKORDBOX_USB:
            path = rekordbox_path(track, file.name)
        elif track_id in crates:
            path = serato_path(crates[track_id], file.name)
        else:
            continue
        candidate, n = path, 1
        while ((owner := taken.get(str(candidate).lower())) is not None and owner != file) or str(
            candidate
        ).lower() in occupied:
            n += 1
            candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        taken[str(candidate).lower()] = file
        wanted[track_id] = candidate
    return wanted
