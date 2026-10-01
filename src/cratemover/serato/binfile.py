"""Serato's tag-length-value container, used by ``database V2`` and ``.crate`` files.

Each field is a 4-byte ASCII tag, a big-endian u32 length, then the payload.
The tag's first letter gives the payload type::

    o, r  nested fields            t, p  UTF-16BE text / path
    u     u32                      s     u16
    b     one byte, boolean

``vrsn`` is the exception (text). Documented in Holzhaus/serato-tags and
bvandercar-vt/serato-tools; Mixxx reads it in ``src/library/serato/seratofeature.cpp``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

Value = str | int | bool | bytes | list["Field"]

DATABASE_VERSION = "2.0/Serato Scratch LIVE Database"
CRATE_VERSION = "1.0/Serato ScratchLive Crate"


class SeratoFormatError(ValueError):
    pass


@dataclass
class Field:
    tag: str
    value: Value

    def get(self, tag: str) -> Value | None:
        """First child with ``tag`` (nested fields only)."""
        if isinstance(self.value, list):
            for child in self.value:
                if child.tag == tag:
                    return child.value
        return None


def _kind(tag: str) -> str:
    return "t" if tag == "vrsn" else tag[0]


def parse(data: bytes) -> list[Field]:
    fields: list[Field] = []
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise SeratoFormatError(f"truncated field header at byte {pos}")
        raw_tag, length = struct.unpack_from(">4sI", data, pos)
        pos += 8
        payload = data[pos : pos + length]
        if len(payload) != length:
            raise SeratoFormatError(f"truncated field {raw_tag!r} at byte {pos}")
        pos += length
        try:
            tag = raw_tag.decode("ascii")
        except UnicodeDecodeError as exc:
            raise SeratoFormatError(f"bad field tag {raw_tag!r}") from exc
        fields.append(Field(tag, _decode(tag, payload)))
    return fields


def _decode(tag: str, payload: bytes) -> Value:
    kind = _kind(tag)
    try:
        if kind in ("o", "r"):
            return parse(payload)
        if kind in ("t", "p"):
            return payload.decode("utf-16-be").rstrip("\x00")
        if kind == "u" and len(payload) == 4:
            return int(struct.unpack(">I", payload)[0])
        if kind == "s" and len(payload) == 2:
            return int(struct.unpack(">H", payload)[0])
        if kind == "b" and len(payload) == 1:
            return payload != b"\x00"
    except (UnicodeDecodeError, SeratoFormatError):
        pass
    return payload  # unknown layout: keep the bytes so they survive a round trip


def _encode(field: Field) -> bytes:
    value, kind = field.value, _kind(field.tag)
    if isinstance(value, bytes):
        return value
    if isinstance(value, list):
        return dump(value)
    if isinstance(value, bool):
        return b"\x01" if value else b"\x00"
    if isinstance(value, int):
        return struct.pack(">H" if kind == "s" else ">I", value)
    return value.encode("utf-16-be")


def dump(fields: list[Field]) -> bytes:
    out = bytearray()
    for field in fields:
        payload = _encode(field)
        out += struct.pack(">4sI", field.tag.encode("ascii"), len(payload)) + payload
    return bytes(out)
