"""Command line: ``cratemover inspect``, ``convert``, ``sync`` and ``serve``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

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
from .keys import KeyNotation
from .model import Format
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


def _outcome(ok: bool, message: str) -> None:
    """A last line that stands out: green for success, red for failure."""
    line = f"{'✔' if ok else '✘'} {message}"
    if sys.stdout.isatty():
        line = f"\033[1;{32 if ok else 31}m{line}\033[0m"
    print(f"\n{line}")


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
        "convert-drive",
        help="replace a USB stick's library with the other format, in place",
        description="Back the stick's library and music up to this computer byte for byte, "
        "replace the library with the other format, check the result exactly, and restore the "
        "backup if the check fails.",
    )
    drive.add_argument("drive", help="the stick's mount point, e.g. /media/me/STICK")
    drive.add_argument(
        "--to", dest="target_format", required=True, type=Format,
        choices=[Format.REKORDBOX_USB, Format.SERATO],
    )  # fmt: skip
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

    restore = sub.add_parser("restore-drive", help="put a stick back as a backup has it")
    restore.add_argument("backup", help="a backup folder made by convert-drive")
    restore.add_argument("drive", help="the stick's mount point")

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-browser", action="store_true", help="don't open the web UI")

    args = parser.parse_args(argv)
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

    def progress(message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')}  {message}", file=sys.stderr)

    if args.command == "convert-drive":
        from .drive_convert import DriveConvertOptions, convert_drive

        name = {Format.REKORDBOX_USB: "Rekordbox", Format.SERATO: "Serato"}[args.target_format]
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
            print(str(exc), file=sys.stderr)
            _outcome(False, f"NOT CONVERTED: {args.drive} was left (or put back) as it was")
            return 1
        for warning in result.warnings:
            progress(warning)
        progress(f"Backup: {result.backup}")
        _outcome(True, f"CONVERTED {args.drive} to {name}, checked track by track")
        return 0

    if args.command == "restore-drive":
        from .drive_convert import restore_drive

        try:
            for line in restore_drive(Path(args.backup), Path(args.drive), progress):
                progress(line)
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            _outcome(False, f"RESTORE FAILED: {args.drive} doesn't match the backup")
            return 1
        _outcome(True, f"RESTORED {args.drive}: every file matches the backup")
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
