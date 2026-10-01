"""Waveforms for Rekordbox analysis files, measured with ffmpeg.

ffmpeg decodes, downmixes and band-filters the audio (a C filter graph is far
faster than filtering per sample in Python); Python then takes per-column
peaks at Rekordbox's 150 columns per second. The mapping from measurements to
bytes follows M-Igashi/baken ``anlz/generate/waveform.rs`` (MIT), which was
fitted against 1,070 Rekordbox 7 tracks. Players only check the structure, so
approximate colours are fine.

Without ffmpeg, :func:`placeholder` gives flat waveforms of the right shape.
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from .anlz import Section, detail_section, preview_section

COLUMNS_PER_SECOND = 150
RATE = 11025  # analysis sample rate: enough for the 2.5 kHz high band
CHANNELS = 5  # full, <150 Hz (whiteness), <300 Hz, 250-1500 Hz, >2500 Hz
RELEASE = (0.97, 0.93, 0.90)
GAIN_TARGET = (78.0, 105.0, 115.0)
GAIN_FLOOR = (80, 80, 95)
GAIN_CAP = (267, 300, 500)
BAND_SCALE = (1.25, 1.15, 0.6)
PWV6_SCALE = (0.56, 0.71, 2.0)
PWV4_SCALE = (2.4, 1.6, 1.1)

_FILTER = (
    f"[0:a:0]aresample={RATE},aformat=sample_fmts=flt:channel_layouts=mono,asplit=5[m][a][b][c][d];"
    "[a]lowpass=f=150[a1];[b]lowpass=f=300[b1];[c]highpass=f=250,lowpass=f=1500[c1];"
    "[d]highpass=f=2500[d1];[m][a1][b1][c1][d1]amerge=inputs=5[out]"
)


@dataclass
class Measured:
    frames: int = 0
    peak: list[float] = field(default_factory=list)
    white: list[float] = field(default_factory=list)
    bands: list[tuple[float, float, float]] = field(default_factory=list)
    sumsq: list[float] = field(default_factory=list)
    samples: list[int] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return self.frames * 1000.0 / RATE


def ffmpeg() -> str | None:
    """The ffmpeg on PATH, else the one bundled with imageio-ffmpeg (installed outside Docker)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError:
        return None


def have_ffmpeg() -> bool:
    return ffmpeg() is not None


def measure(path: Path) -> Measured:
    """Decode ``path`` with ffmpeg and measure every 1/150 s column."""
    proc = subprocess.run(
        [ffmpeg() or "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-filter_complex", _FILTER,
         "-map", "[out]", "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    )  # fmt: skip
    data = array("f")
    data.frombytes(proc.stdout[: len(proc.stdout) // (4 * CHANNELS) * 4 * CHANNELS])
    frames = len(data) // CHANNELS
    m = Measured(frames=frames)
    n = max(1, math.ceil(frames * COLUMNS_PER_SECOND / RATE))
    for col in range(n):
        a = round(col * RATE / COLUMNS_PER_SECOND)
        b = min(frames, round((col + 1) * RATE / COLUMNS_PER_SECOND))
        if b <= a:
            m.peak.append(0.0)
            m.white.append(0.0)
            m.bands.append((0.0, 0.0, 0.0))
            m.sumsq.append(0.0)
            m.samples.append(0)
            continue
        levels = []
        for ch in range(CHANNELS):
            seg = data[a * CHANNELS + ch : b * CHANNELS : CHANNELS]
            levels.append(max(max(seg), -min(seg)))
        full = data[a * CHANNELS : b * CHANNELS : CHANNELS]
        m.peak.append(levels[0])
        m.white.append(levels[1])
        m.bands.append((levels[2], levels[3], levels[4]))
        m.sumsq.append(math.fsum(map(float.__mul__, full, full)))
        m.samples.append(b - a)
    return m


def _ranges(n: int, parts: int) -> list[range]:
    out = []
    for k in range(parts):
        a = min(k * n // parts, n - 1)
        b = min(max((k + 1) * n // parts, a + 1), n)
        out.append(range(a, b))
    return out


@dataclass
class Waveforms:
    pwav: bytes  # 400-column preview (DAT)
    pwv2: bytes  # 100-column tiny preview (DAT)
    pwv3: bytes  # 150 columns/s detail (EXT)
    pwv4: bytes  # 1200 x 6 colour preview (EXT)
    pwv5: bytes  # 150 columns/s colour detail (EXT)
    pwv6: bytes  # 1200 x 3 three-band preview (2EX)
    pwv7: bytes  # 150 columns/s three-band detail (2EX)
    gains: tuple[int, int, int]

    def dat_sections(self) -> list[Section]:
        return [preview_section("PWAV", self.pwav), preview_section("PWV2", self.pwv2)]

    def pwv3_section(self) -> Section:
        return detail_section("PWV3", 1, 0x00960000, self.pwv3)

    def pwv5_section(self) -> Section:
        return detail_section("PWV5", 2, 0x00960305, self.pwv5)

    def pwv4_section(self) -> Section:
        return detail_section("PWV4", 6, 0, self.pwv4)

    def two_ex_sections(self) -> list[Section]:
        pwv6 = struct.pack(">II", 3, len(self.pwv6) // 3) + self.pwv6
        pwvc = struct.pack(">HHHH", 0, *self.gains)
        return [
            detail_section("PWV7", 3, 0x00960000, self.pwv7),
            Section.build("PWV6", 0x14, pwv6),
            Section.build("PWVC", 0x0E, pwvc),
        ]


def analyze(m: Measured) -> Waveforms:
    n = len(m.peak)
    track_peak = max(m.peak, default=0.0)

    def height(p: float) -> int:
        return min(31, math.floor(31.5 * (p / track_peak) ** 2)) if track_peak > 0 else 0

    def whiteness(low: float, p: float) -> int:
        return 7 if p <= 0 else 7 - min(7, math.floor(8 * min(1.0, max(0.0, low / p))))

    heights = [height(p) for p in m.peak]
    whites = [whiteness(w, p) for w, p in zip(m.white, m.peak, strict=True)]
    pwv3 = bytes((w << 5) | h for w, h in zip(whites, heights, strict=True))

    env: list[list[float]] = []
    for i, band in enumerate(m.bands):
        env.append([max(band[j], env[i - 1][j] * RELEASE[j] if i else 0.0) for j in range(3)])
    gains = []
    for j in range(3):
        top = max((e[j] for e in env), default=0.0)
        gains.append(
            min(GAIN_CAP[j], max(GAIN_FLOOR[j], round(GAIN_TARGET[j] / top)))
            if top > 0
            else GAIN_FLOOR[j]
        )
    pwv7_cols = [[min(127, round(BAND_SCALE[j] * gains[j] * e[j])) for j in range(3)] for e in env]
    pwv7 = bytes(v for col in pwv7_cols for v in col)

    pwv5 = bytearray()
    for band, h in zip(m.bands, heights, strict=True):
        top = max(band)

        def level(x: float, top: float = top) -> int:
            return 0 if top <= 0 else min(7, round(7 * math.sqrt(x / top)))

        pwv5 += struct.pack(
            ">H", level(band[0]) << 13 | level(band[1]) << 10 | level(band[2]) << 7 | h << 2
        )

    def rms(r: range) -> float:
        total = sum(m.samples[i] for i in r)
        return math.sqrt(sum(m.sumsq[i] for i in r) / total) if total else 0.0

    rms400 = [rms(r) for r in _ranges(n, 400)]
    top400 = max(rms400, default=0.0)
    white200 = [round(sum(whites[i] for i in r) / len(r)) for r in _ranges(n, 200)]
    pwav = bytes(
        (min(7, white200[k // 2]) << 5) | min(31, round(24 * v / top400) if top400 else 0)
        for k, v in enumerate(rms400)
    )
    rms100 = [rms(r) for r in _ranges(n, 100)]
    top100 = max(rms100, default=0.0)
    pwv2 = bytes(min(15, round(15 * math.sqrt(v / top100))) if top100 else 0 for v in rms100)

    pwv6_cols = [
        [
            min(127, round(PWV6_SCALE[j] * sum(pwv7_cols[i][j] for i in r) / len(r)))
            for j in range(3)
        ]
        for r in _ranges(n, 1200)
    ]
    pwv6 = bytes(v for col in pwv6_cols for v in col)
    pwv4 = bytearray()
    for col in pwv6_cols:
        b = [0, 0, 0] + [min(127, round(PWV4_SCALE[j] * col[j])) for j in range(3)]
        r, g, bl = b[3], b[4], b[5]
        if max(r, g, bl) > 0:
            b[2] = min(127, round(0.91 * math.sqrt(r * r + g * g + bl * bl)))
            b[0] = min(127, round(0.88 * max(r, g, bl) + 38))
            b[1] = min(255, max(0, round(203 - 0.49 * b[0])))
        pwv4 += bytes(b)
    return Waveforms(
        pwav, pwv2, pwv3, bytes(pwv4), bytes(pwv5), pwv6, pwv7, (gains[0], gains[1], gains[2])
    )


def placeholder(duration_s: float) -> Waveforms:
    """Flat, mid-height waveforms of the right sizes, for when the audio can't be decoded."""
    n = max(1, math.ceil(duration_s * COLUMNS_PER_SECOND))
    return Waveforms(
        pwav=bytes([(4 << 5) | 12] * 400),
        pwv2=bytes([8] * 100),
        pwv3=bytes([(4 << 5) | 12] * n),
        pwv4=bytes([80, 164, 80, 40, 60, 40] * 1200),
        pwv5=struct.pack(">H", 3 << 13 | 3 << 10 | 3 << 7 | 12 << 2) * n,
        pwv6=bytes([30, 30, 30] * 1200),
        pwv7=bytes([40, 40, 40] * n),
        gains=GAIN_FLOOR,
    )
