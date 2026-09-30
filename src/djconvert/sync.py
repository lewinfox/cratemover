"""Match tracks across two libraries and merge one into the other.

``merge(base, incoming)`` returns a copy of ``base`` (the library being
updated, with its own file locations) with ``incoming`` merged in:

* tracks are matched by path (after path rules), then file name + size, then
  artist + title + duration, then a unique file name;
* unmatched incoming tracks are added, with their locations rewritten;
* metadata, cues, beat grids and playlists are combined per :class:`SyncOptions`.

A two-way sync is ``merge(a, b)`` written to A plus ``merge(b, a)`` written to B,
with the same preference so both sides end up agreeing.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

from .model import Cue, Library, Playlist, Track, normalise_path, path_name
from .paths import apply_rules


class Prefer(StrEnum):
    INCOMING = "incoming"  # the other library wins conflicts
    BASE = "base"  # the library being updated keeps its values


class CuePolicy(StrEnum):
    MERGE = "merge"  # union: hot cue slots and memory cues from both, preferred side wins a slot
    REPLACE = "replace"  # the preferred side's cues, if it has any
    FILL = "fill"  # only give cues to tracks that have none


class PlaylistPolicy(StrEnum):
    MERGE = "merge"  # same-named playlists get the union (base order, then new tracks)
    REPLACE = "replace"  # same-named playlists take the incoming version
    ADD = "add"  # only add playlists the base doesn't have


@dataclass
class SyncOptions:
    prefer: Prefer = Prefer.INCOMING
    cues: CuePolicy = CuePolicy.MERGE
    grids: CuePolicy = CuePolicy.FILL  # MERGE behaves like REPLACE for grids
    metadata: CuePolicy = (
        CuePolicy.FILL
    )  # FILL: only empty fields; REPLACE: preferred side's non-empty values
    playlists: PlaylistPolicy = PlaylistPolicy.MERGE
    add_tracks: bool = True  # add incoming tracks the base lacks
    path_rules: list[tuple[str, str]] = field(default_factory=list)  # incoming paths -> base paths
    duration_tolerance_s: float = 2.0


@dataclass
class SyncReport:
    matched: int = 0
    added: int = 0
    updated: int = 0
    playlists_added: int = 0
    playlists_updated: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "added": self.added,
            "updated": self.updated,
            "playlists_added": self.playlists_added,
            "playlists_updated": self.playlists_updated,
            "matched_by": self.by_rule,
            "details": self.details[:200],
        }


# --- matching -------------------------------------------------------------------------


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def _stem(path: str) -> str:
    name = path_name(path)
    return _norm_text(name.rsplit(".", 1)[0] if "." in name else name)


def match_tracks(
    base: Library, incoming: Library, options: SyncOptions
) -> tuple[dict[str, str], dict[str, str]]:
    """``incoming id -> base id`` and ``incoming id -> rule that matched``."""
    by_path: dict[str, str] = {}
    by_name_size: dict[tuple[str, int], list[str]] = {}
    by_tags: dict[tuple[str, str], list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for t in base.tracks.values():
        by_path[normalise_path(t.location).casefold()] = t.id
        if t.file_size:
            by_name_size.setdefault((_stem(t.location), t.file_size), []).append(t.id)
        if t.title:
            by_tags.setdefault((_norm_text(t.artist), _norm_text(t.title)), []).append(t.id)
        by_name.setdefault(_stem(t.location), []).append(t.id)

    matches: dict[str, str] = {}
    rules: dict[str, str] = {}
    taken: set[str] = set()

    def take(inc: str, candidates: list[str], rule: str) -> bool:
        free = [c for c in candidates if c not in taken]
        if len(free) == 1:
            matches[inc] = free[0]
            rules[inc] = rule
            taken.add(free[0])
            return True
        return False

    pending = list(incoming.tracks.values())
    # Tier by tier, so a strong match is never stolen by a weaker one.
    for t in pending:
        key = normalise_path(apply_rules(t.location, options.path_rules)).casefold()
        if key in by_path and by_path[key] not in taken:
            take(t.id, [by_path[key]], "path")
    for t in pending:
        if t.id not in matches and t.file_size:
            take(t.id, by_name_size.get((_stem(t.location), t.file_size), []), "name+size")
    for t in pending:
        if t.id not in matches and t.title:
            candidates = [
                c
                for c in by_tags.get((_norm_text(t.artist), _norm_text(t.title)), [])
                if not (t.duration_s and base.tracks[c].duration_s)
                or abs(t.duration_s - base.tracks[c].duration_s) <= options.duration_tolerance_s
            ]
            take(t.id, candidates, "artist+title")
    for t in pending:
        if t.id not in matches:
            take(t.id, by_name.get(_stem(t.location), []), "file name")
    return matches, rules


# --- merging ---------------------------------------------------------------------------

_TEXT_FIELDS = ("title", "artist", "album", "album_artist", "genre", "composer", "grouping", "comment",
                "label", "remixer", "year")  # fmt: skip
_VALUE_FIELDS = ("track_number", "bpm", "key", "colour", "date_added")


def _merge_metadata(base: Track, inc: Track, options: SyncOptions) -> bool:
    changed = False
    incoming_wins = options.prefer is Prefer.INCOMING and options.metadata is CuePolicy.REPLACE
    for name in (*_TEXT_FIELDS, *_VALUE_FIELDS):
        old, new = getattr(base, name), getattr(inc, name)
        if new in (None, "", 0) or new == old:
            continue
        if old in (None, "", 0) or incoming_wins:
            setattr(base, name, new)
            changed = True
    if inc.rating and (not base.rating or incoming_wins):
        changed |= base.rating != inc.rating
        base.rating = inc.rating
    if inc.play_count > base.play_count:  # plays only ever go up
        base.play_count = inc.play_count
        changed = True
    return changed


def _cue_key(c: Cue) -> tuple[bool, int]:
    return (c.is_loop, round(c.position_ms / 5))  # within 5 ms counts as the same spot


def _fingerprint(cues: list[Cue]) -> tuple[frozenset[object], ...]:
    """What a DJ would notice: marked spots, hot cues A-H and their names.

    Formats differ in what they can store (Mixxx's main cue and hot cue 9 become
    Rekordbox memory cues, say), so comparing cue lists field by field would
    report a change on every sync.
    """
    hot = [c for c in cues if c.slot is not None and c.slot < 8]
    return (
        frozenset(_cue_key(c) for c in cues),
        frozenset((c.slot, _cue_key(c)) for c in hot),
        frozenset((c.slot, c.name) for c in hot if c.name),
    )


def _merge_cues(base: Track, inc: Track, options: SyncOptions) -> bool:
    if not inc.cues:
        return False
    before = _fingerprint(base.cues)
    prefer_incoming = options.prefer is Prefer.INCOMING
    if options.cues is CuePolicy.FILL:
        if base.cues:
            return False
        base.cues = copy.deepcopy(inc.cues)
        return True
    if options.cues is CuePolicy.REPLACE:
        if not (prefer_incoming or not base.cues):
            return False
        base.cues = copy.deepcopy(inc.cues)
        return _fingerprint(base.cues) != before
    # MERGE: all of the preferred side's cues, plus the other side's at spots and
    # slots the preferred side leaves free.
    first, second = (inc.cues, base.cues) if prefer_incoming else (base.cues, inc.cues)
    merged = copy.deepcopy(first)
    slots = {c.slot for c in merged if c.slot is not None}
    spots = {_cue_key(c) for c in merged}
    for c in second:
        if _cue_key(c) in spots or (c.slot is not None and c.slot in slots):
            continue
        merged.append(copy.deepcopy(c))
        spots.add(_cue_key(c))
        if c.slot is not None:
            slots.add(c.slot)
    merged.sort(key=lambda c: c.position_ms)
    changed = _fingerprint(merged) != before
    base.cues = merged
    return changed


def _merge_grid(base: Track, inc: Track, options: SyncOptions) -> bool:
    if not inc.grid:
        return False
    if base.grid and (options.grids is CuePolicy.FILL or options.prefer is Prefer.BASE):
        return False
    same = [(round(m.position_ms), round(m.bpm, 2)) for m in base.grid] == [
        (round(m.position_ms), round(m.bpm, 2)) for m in inc.grid
    ]
    base.grid = copy.deepcopy(inc.grid)
    base.bpm_locked = base.bpm_locked or inc.bpm_locked
    return not same


def _find(node: Playlist, path: tuple[str, ...]) -> Playlist | None:
    for child in node.children:
        if child.name == path[0]:
            if len(path) == 1:
                return child
            if child.is_folder:
                return _find(child, path[1:])
    return None


def _ensure_folder(root: Playlist, path: tuple[str, ...]) -> Playlist:
    node = root
    for name in path:
        found = next((c for c in node.children if c.is_folder and c.name == name), None)
        if found is None:
            found = Playlist.folder(name)
            node.children.append(found)
        node = found
    return node


# A matched pair where one file is an MP3 and the other isn't is almost always a
# transcode made for a player; LAME's encoder delay moves the audio this much.
TRANSCODE_OFFSET_MS = 26.0


def _timeline_shift(base: Track, inc: Track) -> float:
    if base.extension == inc.extension:
        return 0.0
    if base.extension == "mp3":
        return TRANSCODE_OFFSET_MS
    if inc.extension == "mp3":
        return -TRANSCODE_OFFSET_MS
    return 0.0


def _shift(track: Track, ms: float) -> Track:
    moved = copy.deepcopy(track)
    for cue in moved.cues:
        cue.position_ms += ms
        if cue.end_ms is not None:
            cue.end_ms += ms
    for marker in moved.grid:
        marker.position_ms += ms
    return moved


def merge(base: Library, incoming: Library, options: SyncOptions) -> tuple[Library, SyncReport]:
    """``base`` with ``incoming`` merged in. Neither input is modified."""
    result = copy.deepcopy(base)
    report = SyncReport()
    matches, rules = match_tracks(base, incoming, options)
    for rule in rules.values():
        report.by_rule[rule] = report.by_rule.get(rule, 0) + 1
    report.matched = len(matches)

    id_map: dict[str, str] = {}  # incoming id -> id in result
    for inc in incoming.tracks.values():
        if inc.id in matches:
            target = result.tracks[matches[inc.id]]
            if shift := _timeline_shift(target, inc):
                inc = _shift(inc, shift)
            changed = _merge_metadata(target, inc, options)
            changed |= _merge_cues(target, inc, options)
            changed |= _merge_grid(target, inc, options)
            if changed:
                report.updated += 1
                report.details.append(f"updated: {target.display_name}")
            id_map[inc.id] = target.id
        elif options.add_tracks:
            new = copy.deepcopy(inc)
            new.location = apply_rules(inc.location, options.path_rules)
            new.id = f"sync:{inc.id}"
            result.add_track(new)
            id_map[inc.id] = new.id
            report.added += 1
            report.details.append(f"added: {new.display_name}")

    for parents, playlist in incoming.playlists.walk():
        ids = [id_map[t] for t in playlist.track_ids or [] if t in id_map]
        existing = _find(result.playlists, (*parents, playlist.name))
        if existing is None or existing.is_folder:
            folder = _ensure_folder(result.playlists, parents)
            folder.children.append(Playlist(playlist.name, ids, is_crate=playlist.is_crate))
            report.playlists_added += 1
            continue
        old = list(existing.track_ids or [])
        if options.playlists is PlaylistPolicy.REPLACE:
            existing.track_ids = ids
        elif options.playlists is PlaylistPolicy.MERGE:
            existing.track_ids = old + [t for t in ids if t not in old]
        if existing.track_ids != old:
            report.playlists_updated += 1
    return result, report
