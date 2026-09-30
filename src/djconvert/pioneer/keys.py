"""The public SQLCipher keys of Rekordbox databases, stored obfuscated as pyrekordbox does.

From dylanljones/pyrekordbox (MIT, Copyright (c) 2022-2025 Dylan Jones):
``utils.deobfuscate`` and the ``BLOB`` constants of ``masterdb`` and ``devicelib_plus``.
"""

from __future__ import annotations

import base64
import zlib
from pathlib import Path
from typing import Any

_BLOB_KEY = b"657f48f84c437cc1"
# Rekordbox 6/7's master.db.
MASTER_DB = b"PN_Pq^*N>(JYe*u^8;Yg76HuZ<mR13S?=>)b9;DpoTXV(6ItkU`}8*m6tx_I{Solh_N#dfe{v="
# OneLibrary (Device Library Plus): PIONEER/rekordbox/exportLibrary.db on sticks.
ONE_LIBRARY = (
    b"PN_1dH8$oLJY)16j_RvM6qphWw`476>;C1cWmI#se(PG`j}~xAjlufj?`#0i{;=glh(SkW)y0>n?YEiD`l%t("
)


def deobfuscate(blob: bytes) -> str:
    data = base64.b85decode(blob)
    return zlib.decompress(
        bytes(b ^ _BLOB_KEY[i % len(_BLOB_KEY)] for i, b in enumerate(data))
    ).decode()


def open_encrypted(path: Path, blob: bytes) -> Any:
    """Open an SQLCipher 4 database (default settings) with one of the keys above."""
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError as exc:  # pragma: no cover - depends on the extra
        raise RuntimeError(
            "reading encrypted Rekordbox databases needs the 'rekordbox' extra (sqlcipher3)"
        ) from exc
    conn = sqlcipher.connect(str(path))
    conn.execute(f"PRAGMA key = '{deobfuscate(blob)}'")
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()  # fails if the key is wrong
    return conn
