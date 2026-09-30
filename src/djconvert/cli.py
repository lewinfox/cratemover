"""Command line: ``djconvert inspect``, ``djconvert convert``, ``djconvert serve``."""

from __future__ import annotations

import argparse
import json
import sys

from .convert import (
    SOURCE_FORMATS,
    TARGET_FORMATS,
    ReadOptions,
    WriteOptions,
    read_library,
    write_library,
)
from .keys import KeyNotation
from .offsets import MP3_DECODERS
from .paths import parse_rules


def _add_read_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="source_format", required=True, choices=SOURCE_FORMATS)
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
        "--mp3-decoder", choices=MP3_DECODERS, default="MAD", help="Mixxx's MP3 decoder"
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djconvert", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="summarise a library")
    _add_read_args(inspect)

    convert = sub.add_parser("convert", help="convert a library")
    _add_read_args(convert)
    convert.add_argument("--to", dest="target_format", required=True, choices=TARGET_FORMATS)
    convert.add_argument("output", help="output folder")
    convert.add_argument(
        "--path",
        action="append",
        default=[],
        metavar="FROM=>TO",
        help="rewrite track paths for the target machine (repeatable)",
    )
    convert.add_argument("--key-notation", choices=[k.value for k in KeyNotation])
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

    serve = sub.add_parser("serve", help="run the web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("djconvert.web.server:app", host=args.host, port=args.port)
        return 0

    def progress(message: str) -> None:
        print(message, file=sys.stderr)

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
        key_notation=KeyNotation(args.key_notation) if args.key_notation else None,
        mp3_decoder=args.mp3_decoder,
        memory_cues_to_hot_cues=not args.no_memory_to_hot,
        serato_root=args.target_serato_root,
        serato_write_tags=args.write_serato_tags,
        serato_max_hot_cues=args.serato_max_hot_cues,
        serato_base_database=args.serato_base_database,
        mixxx_base_database=args.mixxx_base_database,
        mixxx_playlists_as_crates=args.mixxx_playlists_as_crates,
        playlists=args.playlist,
    )
    result = write_library(library, write, read.access_rules, progress)
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
