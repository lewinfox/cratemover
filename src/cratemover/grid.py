"""Beat grid helpers shared by the readers and writers."""

from __future__ import annotations

from .model import TempoMarker


def simplify(grid: list[TempoMarker], tolerance_ms: float = 2.0) -> list[TempoMarker]:
    """Drop markers that only continue the previous tempo section.

    Rekordbox writes one TEMPO per beat once a track has any tempo change; a
    marker that falls on the previous section's grid at the same tempo, with
    the beat number that grid predicts, adds nothing.
    """
    result: list[TempoMarker] = []
    for marker in sorted(grid, key=lambda m: m.position_ms):
        if result:
            prev = result[-1]
            beat_ms = 60000.0 / prev.bpm
            beats = (marker.position_ms - prev.position_ms) / beat_ms
            on_grid = abs(beats - round(beats)) * beat_ms <= tolerance_ms
            same_tempo = abs(marker.bpm - prev.bpm) <= 0.01
            expected_beat = (prev.beat - 1 + round(beats)) % 4 + 1
            if on_grid and same_tempo and marker.beat == expected_beat:
                continue
        result.append(marker)
    return result


def first_downbeat_ms(marker: TempoMarker) -> float:
    """Position of the first downbeat at or after the track start on this marker's grid."""
    beat_ms = 60000.0 / marker.bpm
    downbeat = marker.position_ms - ((marker.beat - 1) % 4) * beat_ms
    bar_ms = 4 * beat_ms
    return downbeat - (downbeat // bar_ms) * bar_ms if downbeat >= 0 else downbeat % bar_ms


def beat_positions(grid: list[TempoMarker], end_ms: float) -> list[tuple[float, int]]:
    """Every beat (position, beat-in-bar) from the first marker's downbeat to ``end_ms``."""
    beats: list[tuple[float, int]] = []
    grid = sorted(grid, key=lambda m: m.position_ms)
    for i, marker in enumerate(grid):
        beat_ms = 60000.0 / marker.bpm
        stop = grid[i + 1].position_ms - beat_ms / 2 if i + 1 < len(grid) else end_ms
        position, beat = marker.position_ms, marker.beat
        if i == 0:
            # Extend the first section back to the start of the track.
            while position - beat_ms >= 0:
                position -= beat_ms
                beat = (beat - 2) % 4 + 1
        while position < stop:
            beats.append((position, beat))
            position += beat_ms
            beat = beat % 4 + 1
    return beats


def sections_from_beats(
    beats: list[float], tolerance_ms: float = 2.0, first_beat: int = 1
) -> list[TempoMarker]:
    """Group explicit beat positions into constant-tempo sections."""
    if len(beats) < 2:
        return []
    sections: list[TempoMarker] = []
    i = 0
    while i < len(beats) - 1:
        interval = beats[i + 1] - beats[i]
        j = i + 1
        while j + 1 < len(beats) and abs((beats[j + 1] - beats[j]) - interval) <= tolerance_ms:
            j += 1
        bpm = 60000.0 * (j - i) / (beats[j] - beats[i])
        sections.append(TempoMarker(beats[i], bpm, (first_beat - 1 + i) % 4 + 1))
        i = j
    return sections


# Rekordbox stores beat times in whole milliseconds, so even a perfect grid scatters about a
# straight line by up to 0.5 ms (RMS about 0.29 ms). A section is one straight line while its
# beats stay this close to it: RMS for the bulk, and a cap on any single beat.
FIT_RMS_MS = 0.5
FIT_MAX_MS = 2.0


def _line(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Least-squares intercept and slope of ys over xs."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx else 0.0
    return my - slope * mx, slope


def _fits(xs: list[float], ys: list[float], intercept: float, slope: float) -> bool:
    errors = [y - (intercept + slope * x) for x, y in zip(xs, ys, strict=True)]
    rms = (sum(e * e for e in errors) / len(errors)) ** 0.5
    return rms <= FIT_RMS_MS and max(abs(e) for e in errors) <= FIT_MAX_MS


def fit_sections(beats: list[tuple[float, int, float]]) -> list[TempoMarker]:
    """Tempo sections that best fit a per-beat grid.

    ``beats`` are (position in ms, beat-in-bar, labelled BPM), one per beat, as Rekordbox
    stores them: positions in whole milliseconds, BPM labels rounded to 0.01. The label isn't
    precise enough to rebuild the grid (a beat labelled 128.00 can really be 127.995 and drift
    a few ms over a minute), so each section is the least-squares line through its beats. A
    section grows while the line fits (``FIT_RMS_MS``, ``FIT_MAX_MS``); a new one starts where
    the label changes or the beats leave the line. When the labelled BPM fits as well, it's
    used, so a grid rekordbox wrote at exactly 128.00 stays 128.00.
    """
    markers: list[TempoMarker] = []
    i = 0
    while i < len(beats):
        label = beats[i][2]
        j = i
        # Grow in doubling steps, then narrow down to the longest stretch that still fits.
        good, step = i, 1
        while True:
            k = min(j + step, len(beats) - 1)
            while k > good and beats[k][2] != label:
                k -= 1
            if k <= good:
                break
            xs = [float(n - i) for n in range(i, k + 1)]
            ys = [beats[n][0] for n in range(i, k + 1)]
            if _fits(xs, ys, *_line(xs, ys)):
                good, j, step = k, k, step * 2
                if k == len(beats) - 1:
                    break
            elif step == 1:
                break
            else:
                step = max(1, step // 2)
        xs = [float(n - i) for n in range(i, good + 1)]
        ys = [beats[n][0] for n in range(i, good + 1)]
        intercept, beat_ms = _line(xs, ys) if good > i else (ys[0], 60000.0 / label)
        labelled = 60000.0 / label
        label_start = sum(y - x * labelled for x, y in zip(xs, ys, strict=True)) / len(xs)
        if good > i and _fits(xs, ys, label_start, labelled):
            intercept, beat_ms = label_start, labelled
        # Sit the marker on the stored first beat when that's only rounding away from the line
        # (it always should be), so the grid starts exactly where rekordbox put it.
        start = ys[0] if abs(ys[0] - intercept) <= 0.5 else intercept
        markers.append(TempoMarker(start, 60000.0 / beat_ms, beats[i][1]))
        i = good + 1
    return markers


def shift(grid: list[TempoMarker], ms: float) -> list[TempoMarker]:
    return [TempoMarker(m.position_ms + ms, m.bpm, m.beat) for m in grid]
