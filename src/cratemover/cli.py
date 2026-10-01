"""Command line: ``cratemover inspect``, ``convert``, ``sync``, ``convert-usb``, ``restore-usb``, ``keys`` and ``serve``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

from .convert import (
    SOURCE_FORMATS,
    SYNC_FORMATS,
    TARGET_FORMATS,
    ReadOptions,
    SyncSide,
    WriteOptions,
    read_library,
    sync_libraries,
    write_library,
)
from .keys import KeyNotation, format_key, parse_key
from .model import Format, Key
from .offsets import Mp3Decoder
from .paths import parse_rules
from .pioneer.usb import OneLibraryMode
from .sync import CuePolicy, Direction, PlaylistPolicy, Prefer, SyncOptions


def _add_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--from", dest="source_format", required=True, type=Format, choices=list(SOURCE_FORMATS)
    )
    parser.add_argument(
        "source",
        help="mixxxdb.sqlite (or its folder), rekordbox.xml, Rekordbox folder/master.db, or _Serato_ folder",
    )
    parser.add_argument(
        "--access",
        action="append",
        default=[],
        metavar="FROM=>TO",
        help="where to find the library's files on this machine, e.g. '/Users/me=>/home/me' (repeatable)",
    )
    parser.add_argument(
        "--mp3-decoder",
        type=Mp3Decoder,
        choices=list(Mp3Decoder),
        default=Mp3Decoder.MAD,
        help="Mixxx's MP3 decoder",
    )
    parser.add_argument(
        "--serato-root",
        default="/",
        help="drive root the Serato source paths are relative to (/, C:/, /Volumes/USB)",
    )
    parser.add_argument(
        "--no-file-tags", action="store_true", help="don't read Serato tags from files"
    )


def _read_options(args: argparse.Namespace) -> ReadOptions:
    return ReadOptions(
        format=args.source_format,
        path=args.source,
        access_rules=parse_rules("\n".join(args.access)),
        mp3_decoder=args.mp3_decoder,
        serato_root=args.serato_root,
        read_file_tags=not args.no_file_tags,
    )


def _add_usb_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-copy", action="store_true", help="don't copy tracks onto USB sticks")
    parser.add_argument("--no-waveforms", action="store_true", help="don't measure waveforms (USB)")
    parser.add_argument(
        "--onelibrary",
        type=OneLibraryMode,
        choices=list(OneLibraryMode),
        default=OneLibraryMode.AUTO,
    )
    parser.add_argument("--device-name", default="")


_SHARPS = {1: "C#", 3: "D#", 6: "F#", 8: "G#", 10: "A#"}
_FLATS = {1: "Db", 3: "Eb", 6: "Gb", 8: "Ab", 10: "Bb"}


def _spellings(key: Key) -> str:
    """A key's musical name, with both spellings on the black notes (Abm/G#m)."""
    suffix = "m" if key.minor else ""
    if key.tonic in _SHARPS:
        return f"{_FLATS[key.tonic]}{suffix}/{_SHARPS[key.tonic]}{suffix}"
    return format_key(key, KeyNotation.MUSICAL)


def _camelot(number: int, minor: bool) -> Key:
    key = parse_key(f"{(number - 1) % 12 + 1}{'A' if minor else 'B'}")
    assert key is not None
    return key


# How `keys` highlights a key and the keys that mix with it.
KEY_STYLES = {
    "key": "bold black on white",
    "next": "bold black on green3",
    "further": "black on dark_orange",
}


def keys_table(key: Key | None) -> Table:
    """The Camelot wheel as a table, with musical and Open Key names. With a key: it, the keys
    next to it on the wheel (a fifth either way, or the relative major/minor) and the keys a
    step further (two fifths, or the relative's neighbours) are highlighted."""
    marks: dict[Key, str] = {}
    if key is not None:
        number, minor = int(format_key(key, KeyNotation.CAMELOT)[:-1]), key.minor
        for d, other in ((-1, False), (1, False), (0, True)):
            marks[_camelot(number + d, minor ^ other)] = "next"
        for d, other in ((-2, False), (2, False), (-1, True), (1, True)):
            marks[_camelot(number + d, minor ^ other)] = "further"
        marks[key] = "key"
    table = Table(box=box.ROUNDED, header_style="bold", show_lines=False, padding=(0, 1))
    for title in ("Minor", "Major"):
        table.add_column(title, justify="left", no_wrap=True)
    for number in range(1, 13):
        cells = []
        for minor in (True, False):
            k = _camelot(number, minor)
            text = (
                f" {format_key(k, KeyNotation.CAMELOT):>3}  {_spellings(k):<8} "
                f"{format_key(k, KeyNotation.OPEN_KEY):>3} "
            )
            cells.append(Text(text, style=KEY_STYLES.get(marks.get(k, ""), "")))
        table.add_row(*cells)
    if key is not None:
        table.caption = Text.assemble(
            (f" {_spellings(key)} ", KEY_STYLES["key"]), "  ",
            (" next to it ", KEY_STYLES["next"]), "  ",
            (" a step further ", KEY_STYLES["further"]),
        )  # fmt: skip
    return table


def keys_strip(key: Key) -> Table:
    """A flattened slice of the Camelot wheel around ``key``: five numbers wide, minor keys
    above and major below, with ``key`` in the middle. Highlighted as in :func:`keys_table`;
    the two corners are on the slice but don't mix with ``key``."""
    number, minor = int(format_key(key, KeyNotation.CAMELOT)[:-1]), key.minor
    table = Table(box=box.ROUNDED, show_header=False, show_lines=True, padding=(0, 1))
    for _ in range(5):
        table.add_column(justify="center", no_wrap=True, min_width=9)
    for row_minor in (True, False):
        cells = []
        for d in (-2, -1, 0, 1, 2):
            k = _camelot(number + d, row_minor)
            steps = abs(d) + (row_minor != minor)  # moves around the wheel to get there
            style = KEY_STYLES["key"] if steps == 0 else (
                KEY_STYLES["next"] if steps == 1 else KEY_STYLES["further"] if steps == 2 else "dim"
            )  # fmt: skip
            cells.append(
                Text.assemble(
                    (f"{_spellings(k)}\n", "bold"), format_key(k, KeyNotation.CAMELOT), style=style
                )
            )
        table.add_row(*cells)
    table.caption = Text.assemble(
        (" next to it ", KEY_STYLES["next"]), "  ", (" a step further ", KEY_STYLES["further"]),
    )  # fmt: skip
    return table


def _print_keys(chosen: str | None) -> int:
    key = parse_key(chosen) if chosen else None
    if chosen and key is None:
        print(f"Not a key: {chosen!r}", file=sys.stderr)
        return 1
    Console().print(keys_strip(key) if key is not None else keys_table(None))
    return 0


class _Log:
    """Timestamped progress lines, and a progress bar for each per-file step (``steps`` then
    ``tick`` per file), so a long step on a big stick visibly moves."""

    def __init__(self) -> None:
        self.console = Console(stderr=True, highlight=False)
        self.bar: Progress | None = None
        self.task: Any = None

    def __call__(self, message: str) -> None:
        self.done()
        self.console.print(Text.assemble((time.strftime("%H:%M:%S"), "dim"), "  ", message))

    def steps(self, total: int) -> None:
        self.done()
        self.bar = Progress(
            TextColumn("          "),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=self.console,
            transient=False,
        )
        self.task = self.bar.add_task("", total=total)
        self.bar.start()

    def tick(self) -> None:
        if self.bar is not None:
            self.bar.advance(self.task)

    def done(self) -> None:
        if self.bar is not None:
            self.bar.stop()
            self.bar = None


FORMAT_NAMES = {Format.REKORDBOX_USB: "Rekordbox", Format.SERATO: "Serato"}
USB_TARGETS = {"rekordbox": Format.REKORDBOX_USB, "serato": Format.SERATO}  # convert-usb --to


def _drive_formats(drive: Path) -> list[Format]:
    from .devices import _libraries

    return [Format(lib["format"]) for lib in _libraries(drive)] if drive.is_dir() else []


def _converted(drive: str, source: Format | None, target: Format, result: Any) -> None:
    """A summary of a stick conversion: what's on it now, what moved, where the backup is,
    and anything worth knowing (in yellow)."""
    summary = result.written.get(target, {}).get("summary", {})
    facts = Table.grid(padding=(0, 2))
    facts.add_column(style="dim", no_wrap=True)
    facts.add_column(overflow="fold")
    names = f"{FORMAT_NAMES.get(source, source or '?')} → {FORMAT_NAMES[target]}"
    facts.add_row("Library", names + f"  (removed {', '.join(result.removed) or 'nothing'})")
    facts.add_row(
        "Tracks",
        f"{summary.get('tracks', 0)} in {summary.get('playlists', 0)} playlist(s), "
        f"{summary.get('hot_cues', 0) + summary.get('memory_cues', 0)} cue(s), "
        f"{summary.get('gridded', 0)} beat grid(s)",
    )
    facts.add_row(
        "Audio",
        f"{len(result.moved)} file(s) moved into {FORMAT_NAMES[target]}'s layout"
        if result.moved
        else "left where it was",
    )
    facts.add_row(
        "Checked",
        f"every track, cue and beat matches the source (fingerprint {result.fingerprint[:12]})",
    )
    facts.add_row("Backup", result.backup)
    parts: list[Any] = [facts]
    for line in result.warnings:
        parts.append(Text(f"• {line}", style="dim"))
    for note in result.notes:
        parts.append(Text(f"⚠ {note}", style="yellow"))
    Console().print(
        Panel(
            Group(*parts),
            title=f"✔ Converted {Path(drive).name} to {FORMAT_NAMES[target]}",
            title_align="left",
            border_style="green",
        )
    )


def _failed(title: str, details: str) -> None:
    lines = [line for line in details.splitlines() if line.strip()]
    body = Group(*(Text(line, style="red" if line.startswith("- ") else "") for line in lines))
    Console().print(Panel(body, title=f"✘ {title}", title_align="left", border_style="red"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cratemover", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="summarise a library")
    _add_read_args(inspect)

    convert = sub.add_parser("convert", help="convert a library")
    _add_read_args(convert)
    convert.add_argument(
        "--to", dest="target_format", required=True, type=Format, choices=list(TARGET_FORMATS)
    )
    convert.add_argument("output", help="output folder")
    convert.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="FROM=>TO",
        help="rewrite track paths for the target machine (repeatable)",
    )
    convert.add_argument("--key-notation", type=KeyNotation, choices=list(KeyNotation))
    convert.add_argument(
        "--playlist", action="append", default=[], help="only this playlist (repeatable)"
    )
    convert.add_argument("--target-serato-root", default="/")
    convert.add_argument(
        "--write-serato-tags",
        action="store_true",
        help="write cues and beat grids into the audio files (modifies them)",
    )
    convert.add_argument("--serato-max-hot-cues", type=int, default=8)
    convert.add_argument("--serato-base-database", default="")
    convert.add_argument("--mixxx-base-database", default="")
    convert.add_argument("--mixxx-playlists-as-crates", action="store_true")
    convert.add_argument("--no-memory-to-hot", action="store_true", help="drop memory cues instead")
    convert.add_argument(
        "--in-place",
        action="store_true",
        help="update the existing library at OUTPUT (backed up first) instead of writing a new one",
    )
    _add_usb_args(convert)

    sync = sub.add_parser("sync", help="sync two libraries in place")
    sync.add_argument("--a-format", required=True, type=Format, choices=list(SOURCE_FORMATS))
    sync.add_argument("a", help="library A")
    sync.add_argument("--b-format", required=True, type=Format, choices=list(SOURCE_FORMATS))
    sync.add_argument("b", help="library B")
    sync.add_argument(
        "--direction", type=Direction, choices=list(Direction), default=Direction.BOTH
    )
    sync.add_argument(
        "--prefer",
        type=Prefer,
        choices=list(Prefer),
        default=Prefer.INCOMING,
        help="incoming: the side changes come from wins (A, for both ways); base: the other",
    )
    fill_or_replace = [CuePolicy.FILL, CuePolicy.REPLACE]
    sync.add_argument("--cues", type=CuePolicy, choices=list(CuePolicy), default=CuePolicy.MERGE)
    sync.add_argument("--grids", type=CuePolicy, choices=fill_or_replace, default=CuePolicy.FILL)
    sync.add_argument("--metadata", type=CuePolicy, choices=fill_or_replace, default=CuePolicy.FILL)
    sync.add_argument(
        "--playlists",
        type=PlaylistPolicy,
        choices=list(PlaylistPolicy),
        default=PlaylistPolicy.MERGE,
    )
    sync.add_argument("--no-add", action="store_true", help="don't add tracks the other side lacks")
    sync.add_argument(
        "--path", action="append", default=[], metavar="FROM=>TO", help="A paths => B paths"
    )
    sync.add_argument("--a-serato-root", default="/")
    sync.add_argument("--b-serato-root", default="/")
    sync.add_argument(
        "--mp3-decoder", type=Mp3Decoder, choices=list(Mp3Decoder), default=Mp3Decoder.MAD
    )
    sync.add_argument("--write-serato-tags", action="store_true")
    sync.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    _add_usb_args(sync)

    drive = sub.add_parser(
        "convert-usb",
        help="replace a USB stick's library with the other format, in place",
        description="Back the stick's library and music up to this computer byte for byte, "
        "replace the library with the other format, check the result exactly, and restore the "
        "backup if the check fails.",
    )
    drive.add_argument("drive", help="the stick's mount point, e.g. /media/me/STICK")
    drive.add_argument("--to", dest="target_name", required=True, choices=list(USB_TARGETS))
    drive.add_argument(
        "--backup-dir", default="export/backups", help="where the backup goes (on this computer)"
    )
    drive.add_argument("--whole-drive", action="store_true", help="back up every file on the stick")
    drive.add_argument(
        "--onelibrary", action="store_true", help="Rekordbox: also write OneLibrary (experimental)"
    )
    drive.add_argument("--no-waveforms", action="store_true", help="Rekordbox: flat waveforms")
    drive.add_argument(
        "--mp3-decoder", type=Mp3Decoder, choices=list(Mp3Decoder), default=Mp3Decoder.MAD
    )

    restore = sub.add_parser("restore-usb", help="put a stick back as a backup has it")
    restore.add_argument("backup", help="a backup folder made by convert-usb")
    restore.add_argument("drive", help="the stick's mount point")

    keys = sub.add_parser(
        "keys",
        help="print the Camelot wheel, or the keys near a key",
        description="With no key: the Camelot wheel with musical and Open Key names. With a key "
        "in any notation (8A, Am, G#m, 1m): the slice of the wheel around it, minor keys above "
        "and major below, highlighting the keys next to it (green) and a step further (orange).",
    )
    keys.add_argument("key", nargs="?", help="e.g. 8A, Am, F#m, 1m")

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-browser", action="store_true", help="don't open the web UI")

    args = parser.parse_args(argv)
    if args.command == "keys":
        return _print_keys(args.key)

    if args.command == "serve":
        import threading
        import webbrowser

        import uvicorn

        if not args.no_browser:
            url = (
                f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
            )
            threading.Timer(1.5, webbrowser.open, [url]).start()
        uvicorn.run("cratemover.web.server:app", host=args.host, port=args.port)
        return 0

    progress = _Log()

    if args.command == "convert-usb":
        from .drive_convert import DriveConvertOptions, convert_drive

        args.target_format = USB_TARGETS[args.target_name]
        source = next(iter(_drive_formats(Path(args.drive))), None)
        try:
            result = convert_drive(
                Path(args.drive),
                DriveConvertOptions(
                    target=args.target_format,
                    backup_dir=args.backup_dir,
                    full_backup=args.whole_drive,
                    waveforms=not args.no_waveforms,
                    onelibrary=args.onelibrary,
                    mp3_decoder=args.mp3_decoder,
                ),
                progress,
            )
        except (ValueError, OSError) as exc:
            progress.done()
            _failed(
                f"Not converted: {args.drive} was left (or put back) as it was",
                str(exc),
            )
            return 1
        progress.done()
        _converted(args.drive, source, args.target_format, result)
        return 0

    if args.command == "restore-usb":
        from .drive_convert import restore_drive

        try:
            done = restore_drive(Path(args.backup), Path(args.drive), progress)
        except OSError as exc:
            _failed(f"Restore failed: {args.drive} doesn't match the backup", str(exc))
            return 1
        body = Text("\n".join(done))
        Console().print(
            Panel(body, title=f"✔ Restored {args.drive}", title_align="left", border_style="green")
        )
        return 0

    if args.command == "sync":
        written = {
            Direction.BOTH: [args.a_format, args.b_format],
            Direction.A_TO_B: [args.b_format],
            Direction.B_TO_A: [args.a_format],
        }[args.direction]
        for fmt in written:
            if fmt not in SYNC_FORMATS:
                parser.error(f"{fmt} can only be read; sync it one way only")

        def side(fmt: Format, path: str, root: str) -> SyncSide:
            read = ReadOptions(
                format=fmt, path=path, mp3_decoder=args.mp3_decoder, serato_root=root
            )
            write = WriteOptions(
                format=fmt,
                output_dir=path,
                mp3_decoder=args.mp3_decoder,
                serato_write_tags=args.write_serato_tags,
                copy_missing=not args.no_copy,
                waveforms=not args.no_waveforms,
                onelibrary=args.onelibrary,
                device_name=args.device_name,
            )
            return SyncSide(read, write)

        options = SyncOptions(
            prefer=args.prefer,
            cues=args.cues,
            grids=args.grids,
            metadata=args.metadata,
            playlists=args.playlists,
            add_tracks=not args.no_add,
            path_rules=parse_rules("\n".join(args.path)),
        )
        result = sync_libraries(
            side(args.a_format, args.a, args.a_serato_root),
            side(args.b_format, args.b, args.b_serato_root),
            args.direction,
            options,
            args.dry_run,
            progress,
        )
        print(json.dumps(result.as_dict(), indent=2, default=str))
        return 0

    read = _read_options(args)
    library = read_library(read, progress)
    if args.command == "inspect":
        print(json.dumps({"source": library.source, **library.summary()}, indent=2))
        for parents, playlist in library.playlists.walk():
            print(f"  {' / '.join((*parents, playlist.name))} ({len(playlist.track_ids or [])})")
        for warning in library.warnings:
            print(f"warning: {warning}")
        return 0

    write = WriteOptions(
        format=args.target_format,
        output_dir=args.output,
        path_rules=parse_rules("\n".join(args.path)),
        key_notation=args.key_notation,
        mp3_decoder=args.mp3_decoder,
        memory_cues_to_hot_cues=not args.no_memory_to_hot,
        serato_root=args.target_serato_root,
        serato_write_tags=args.write_serato_tags,
        serato_max_hot_cues=args.serato_max_hot_cues,
        serato_base_database=args.serato_base_database,
        mixxx_base_database=args.mixxx_base_database,
        mixxx_playlists_as_crates=args.mixxx_playlists_as_crates,
        copy_missing=not args.no_copy,
        waveforms=not args.no_waveforms,
        onelibrary=args.onelibrary,
        device_name=args.device_name,
        in_place=args.in_place,
        playlists=args.playlist,
    )
    result = write_library(library, write, read.access_rules, progress)
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
