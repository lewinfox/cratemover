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


def fit_sections(
    beats: list[tuple[float, int, float]], tolerance_ms: float = 1.0
) -> list[TempoMarker]:
    """Tempo sections that put every beat within ``tolerance_ms`` of where it was.

    ``beats`` are (position in ms, beat-in-bar, labelled BPM), one per beat, as Rekordbox
    stores them: positions in whole milliseconds, BPM labels rounded to 0.01. The label isn't
    precise enough to rebuild the grid (a beat labelled 128.00 can really be 127.995 and drift
    a few ms over a minute), so each section's tempo comes from the beat positions: the
    section runs as long as one straight line through its beats fits them all, and a new one
    starts where the label changes or the beats leave the line.
    """
    markers: list[TempoMarker] = []
    i = 0
    while i < len(beats):
        start, beat_in_bar, label = beats[i]
        lo, hi = 0.0, float("inf")  # beat lengths (ms) that keep every beat so far on the line
        j = i
        while j + 1 < len(beats) and beats[j + 1][2] == label:
            n = j + 1 - i
            offset = beats[j + 1][0] - start
            new_lo, new_hi = (
                max(lo, (offset - tolerance_ms) / n),
                min(hi, (offset + tolerance_ms) / n),
            )
            if new_lo > new_hi:
                break
            lo, hi, j = new_lo, new_hi, j + 1
        # The label when it fits every beat (it usually does), else the middle of what fits.
        beat_ms = 60000.0 / label
        if j > i and not lo <= beat_ms <= hi:
            beat_ms = (lo + hi) / 2
        markers.append(TempoMarker(start, 60000.0 / beat_ms, beat_in_bar))
        i = j + 1
    return markers


def shift(grid: list[TempoMarker], ms: float) -> list[TempoMarker]:
    return [TempoMarker(m.position_ms + ms, m.bpm, m.beat) for m in grid]
