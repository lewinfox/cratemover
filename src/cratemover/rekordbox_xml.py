"""Read and write Rekordbox XML (``DJ_PLAYLISTS``).

This is the file Rekordbox writes with *File > Export Collection in xml format*
and imports under *Preferences > Advanced > Database > rekordbox xml*.
Format reference: AlphaTheta's "rekordbox XML format list"
(cdn.rekordbox.com/files/20200410160904/xml_format_list.pdf).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote

from .colours import REKORDBOX_DEFAULT_CUE, rekordbox_track_colour
from .keys import KeyNotation, format_key, parse_key
from .model import Cue, CueRole, Library, Playlist, TempoMarker, Track

HOT_CUES = 8  # A-H

# POSITION_MARK Type
CUE, FADE_IN, FADE_OUT, LOAD, LOOP = 0, 1, 2, 3, 4
_ROLE_OF_TYPE = {CUE: CueRole.CUE, FADE_IN: CueRole.FADE_IN, FADE_OUT: CueRole.FADE_OUT,
                 LOAD: CueRole.MAIN, LOOP: CueRole.LOOP}  # fmt: skip

_KINDS = {
    "mp3": "MP3 File", "m4a": "M4A File", "mp4": "M4A File", "aac": "AAC File",
    "flac": "FLAC File", "wav": "WAV File", "aif": "AIFF File", "aiff": "AIFF File",
}  # fmt: skip


# --- locations --------------------------------------------------------------------


def location_to_path(location: str) -> str:
    """``file://localhost/C:/Music/a.mp3`` -> ``C:/Music/a.mp3``; POSIX paths keep their slash."""
    text = location
    for prefix in ("file://localhost", "file://"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    path = unquote(text)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    return path


def path_to_location(path: str) -> str:
    path = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", path):
        return "file://localhost/" + quote(path, safe="/:")
    if path.startswith("//"):  # UNC share
        return "file:" + quote(path, safe="/")
    return "file://localhost" + quote(path, safe="/")


# --- reading ------------------------------------------------------------------------


def _float(value: str | None) -> float:
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0


def _int(value: str | None) -> int:
    try:
        return int(float(value)) if value else 0
    except ValueError:
        return 0


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _colour(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _track(elem: ET.Element) -> Track:
    a = elem.attrib
    track = Track(
        id=f"rb:{a.get('TrackID', '')}",
        location=location_to_path(a.get("Location", "")),
        title=a.get("Name", ""),
        artist=a.get("Artist", ""),
        album=a.get("Album", ""),
        genre=a.get("Genre", ""),
        composer=a.get("Composer", ""),
        grouping=a.get("Grouping", ""),
        comment=a.get("Comments", ""),
        label=a.get("Label", ""),
        remixer=a.get("Remixer", ""),
        year=a.get("Year", "") if a.get("Year", "0") != "0" else "",
        track_number=_int(a.get("TrackNumber")) or None,
        duration_s=_float(a.get("TotalTime")),
        sample_rate=_int(a.get("SampleRate")),
        bitrate=_int(a.get("BitRate")),
        file_size=_int(a.get("Size")),
        bpm=_float(a.get("AverageBpm")),
        key=parse_key(a.get("Tonality")),
        rating=round(_int(a.get("Rating")) / 51),
        colour=_colour(a.get("Colour")),
        play_count=_int(a.get("PlayCount")),
        date_added=_date(a.get("DateAdded")),
    )
    for tempo in elem.iter("TEMPO"):
        bpm = _float(tempo.get("Bpm"))
        if bpm > 0:
            beat = _int(tempo.get("Battito")) or 1
            track.grid.append(TempoMarker(_float(tempo.get("Inizio")) * 1000, bpm, beat))
    track.grid.sort(key=lambda m: m.position_ms)
    for mark in elem.iter("POSITION_MARK"):
        num = _int(mark.get("Num", "-1"))
        role = _ROLE_OF_TYPE.get(_int(mark.get("Type")), CueRole.CUE)
        end = mark.get("End")
        colour = None
        if "Red" in mark.attrib:
            colour = (
                (_int(mark.get("Red")) << 16)
                | (_int(mark.get("Green")) << 8)
                | _int(mark.get("Blue"))
            )
        track.cues.append(
            Cue(
                role,
                _float(mark.get("Start")) * 1000,
                _float(end) * 1000 if end and role is CueRole.LOOP else None,
                slot=num if num >= 0 else None,
                name=mark.get("Name", ""),
                colour=colour,
            )
        )
    return track


def _nodes(
    elem: ET.Element, target: Playlist, keys: dict[str, str], by_location: dict[str, str]
) -> None:
    for node in elem.findall("NODE"):
        name = node.get("Name", "")
        if node.get("Type") == "0":
            folder = Playlist.folder(name)
            _nodes(node, folder, keys, by_location)
            target.children.append(folder)
            continue
        by_path = node.get("KeyType") == "1"
        ids = []
        for entry in node.findall("TRACK"):
            key = entry.get("Key", "")
            tid = by_location.get(location_to_path(key)) if by_path else keys.get(key)
            if tid:
                ids.append(tid)
        target.children.append(Playlist(name, ids))


def read_rekordbox_xml(path: Path) -> Library:
    root = ET.parse(path).getroot()
    if root.tag != "DJ_PLAYLISTS":
        raise ValueError(f"{path} is not a Rekordbox XML file (root is <{root.tag}>)")
    library = Library(source=f"Rekordbox XML {path.name}")
    keys: dict[str, str] = {}
    by_location: dict[str, str] = {}
    collection = root.find("COLLECTION")
    for elem in collection.findall("TRACK") if collection is not None else []:
        track = library.add_track(_track(elem))
        keys[elem.get("TrackID", "")] = track.id
        by_location[track.location] = track.id
    playlists = root.find("PLAYLISTS")
    top = playlists.find("NODE") if playlists is not None else None
    if top is not None:
        _nodes(top, library.playlists, keys, by_location)
    return library


# --- writing ------------------------------------------------------------------------


@dataclass
class RekordboxWriteOptions:
    key_notation: KeyNotation = KeyNotation.CAMELOT
    memory_cues: bool = True  # also write non-hot cues/loops as memory cues
    hot_cues_as_memory: bool = False  # duplicate each hot cue as a memory cue (for CDJs)


@dataclass
class RekordboxWriteResult:
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _num(value: float, places: int = 3) -> str:
    text = f"{value:.{places}f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _year(value: str) -> str:
    match = re.match(r"\s*(\d{4})", value)
    return match.group(1) if match else ""


def _marks(track: Track, options: RekordboxWriteOptions) -> list[dict[str, str]]:
    marks: list[tuple[float, dict[str, str]]] = []

    def add(cue: Cue, num: int, name: str) -> None:
        mark_type = {
            CueRole.LOOP: LOOP,
            CueRole.FADE_IN: FADE_IN,
            CueRole.FADE_OUT: FADE_OUT,
        }.get(cue.role, CUE)
        attrs = {
            "Name": name,
            "Type": str(mark_type),
            "Start": _num(max(0.0, cue.position_ms) / 1000),
        }
        if cue.role is CueRole.LOOP and cue.end_ms is not None:
            attrs["End"] = _num(cue.end_ms / 1000)
        attrs["Num"] = str(num)
        if num >= 0:
            colour = cue.colour if cue.colour is not None else REKORDBOX_DEFAULT_CUE
            attrs |= {
                "Red": str((colour >> 16) & 0xFF),
                "Green": str((colour >> 8) & 0xFF),
                "Blue": str(colour & 0xFF),
            }
        marks.append((cue.position_ms, attrs))

    memory_seen: set[tuple[str, int]] = set()

    def memory(cue: Cue, name: str) -> None:
        key = (cue.role.value if cue.role is CueRole.LOOP else "cue", round(cue.position_ms))
        if key not in memory_seen:
            memory_seen.add(key)
            add(cue, -1, name)

    used: set[int] = set()
    order = sorted(
        track.cues,
        key=lambda c: (c.slot is None, c.slot or 0, c.position_ms, c.role is CueRole.MAIN),
    )
    for cue in order:
        label = cue.name
        if not label and cue.role in (CueRole.INTRO, CueRole.OUTRO):
            label = cue.role.value.title()
        if cue.slot is not None and cue.slot < HOT_CUES and cue.slot not in used:
            used.add(cue.slot)
            add(cue, cue.slot, label)
            if options.hot_cues_as_memory:
                memory(cue, label)
        elif options.memory_cues:
            memory(cue, label)
    return [attrs for _, attrs in sorted(marks, key=lambda m: m[0])]


def _track_element(track: Track, track_id: int, options: RekordboxWriteOptions) -> ET.Element:
    attrs = {
        "TrackID": str(track_id),
        "Name": track.title or track.filename.rsplit(".", 1)[0],
        "Artist": track.artist,
        "Composer": track.composer,
        "Album": track.album,
        "Grouping": track.grouping,
        "Genre": track.genre,
        "Kind": _KINDS.get(track.extension, ""),
        "Size": str(track.file_size) if track.file_size else "",
        "TotalTime": str(round(track.duration_s)) if track.duration_s else "",
        "TrackNumber": str(track.track_number) if track.track_number else "",
        "Year": _year(track.year),
        "AverageBpm": f"{track.bpm:.2f}" if track.bpm else "",
        "DateAdded": track.date_added.isoformat() if track.date_added else "",
        "BitRate": str(track.bitrate) if track.bitrate else "",
        "SampleRate": str(track.sample_rate) if track.sample_rate else "",
        "Comments": track.comment,
        "PlayCount": str(track.play_count) if track.play_count else "",
        "Rating": str(max(0, min(5, track.rating)) * 51) if track.rating else "",
        "Location": path_to_location(track.location),
        "Remixer": track.remixer,
        "Tonality": format_key(track.key, options.key_notation),
        "Label": track.label,
    }
    colour = rekordbox_track_colour(track.colour)
    if colour is not None:
        attrs["Colour"] = f"0x{colour:06X}"
    elem = ET.Element("TRACK", {k: v for k, v in attrs.items() if v != ""})
    for marker in track.grid:
        ET.SubElement(
            elem,
            "TEMPO",
            Inizio=_num(marker.position_ms / 1000),
            Bpm=_num(marker.bpm, 2),
            Metro="4/4",
            Battito=str(marker.beat),
        )
    for mark in _marks(track, options):
        ET.SubElement(elem, "POSITION_MARK", mark)
    return elem


def _node_element(node: Playlist, ids: dict[str, int]) -> ET.Element:
    if node.is_folder:
        elem = ET.Element("NODE", Type="0", Name=node.name, Count=str(len(node.children)))
        elem.extend(_node_element(child, ids) for child in node.children)
        return elem
    entries = [ids[t] for t in node.track_ids or [] if t in ids]
    elem = ET.Element("NODE", Name=node.name, Type="1", KeyType="0", Entries=str(len(entries)))
    for tid in entries:
        ET.SubElement(elem, "TRACK", Key=str(tid))
    return elem


def write_rekordbox_xml(
    library: Library, out_path: Path, options: RekordboxWriteOptions
) -> RekordboxWriteResult:
    ids = {track_id: n for n, track_id in enumerate(library.tracks, 1)}
    root = ET.Element("DJ_PLAYLISTS", Version="1.0.0")
    ET.SubElement(root, "PRODUCT", Name="rekordbox", Version="6.8.5", Company="AlphaTheta")
    collection = ET.SubElement(root, "COLLECTION", Entries=str(len(ids)))
    collection.extend(_track_element(t, ids[t.id], options) for t in library.tracks.values())
    playlists = ET.SubElement(root, "PLAYLISTS")
    top = Playlist.folder("ROOT", library.playlists.children)
    playlists.append(_node_element(top, ids))
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n', encoding="utf-8")
    return RekordboxWriteResult(files=[out_path])
