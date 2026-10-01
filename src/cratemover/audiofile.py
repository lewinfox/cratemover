"""Technical details of an audio file, from its headers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mutagen


@dataclass(frozen=True)
class AudioInfo:
    sample_rate: int
    duration_s: float
    bitrate: int  # kbps
    channels: int
    file_size: int


def probe(path: Path) -> AudioInfo | None:
    try:
        audio = mutagen.File(path)
    except Exception:
        return None
    if audio is None or audio.info is None:
        return None
    info = audio.info
    return AudioInfo(
        sample_rate=int(getattr(info, "sample_rate", 0) or 0),
        duration_s=float(getattr(info, "length", 0) or 0),
        bitrate=int((getattr(info, "bitrate", 0) or 0) / 1000),
        channels=int(getattr(info, "channels", 0) or 0),
        file_size=path.stat().st_size,
    )
