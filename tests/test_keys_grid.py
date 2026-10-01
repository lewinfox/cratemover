from __future__ import annotations

import pytest

from cratemover.grid import beat_positions, first_downbeat_ms, sections_from_beats, simplify
from cratemover.keys import KeyNotation, format_key, key_from_mixxx_id, mixxx_key_id, parse_key
from cratemover.model import Key, TempoMarker


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("8A", Key(9, True)), ("8B", Key(0, False)), ("12A", Key(1, True)), ("01B", Key(11, False)),
        ("1m", Key(9, True)), ("1d", Key(0, False)), ("Am", Key(9, True)), ("F#min", Key(6, True)),
        ("Bb", Key(10, False)), ("Ebm", Key(3, True)), ("C#", Key(1, False)), ("A minor", Key(9, True)),
    ],
)  # fmt: skip
def test_parse_key(text: str, key: Key) -> None:
    assert parse_key(text) == key


def test_key_notations_round_trip() -> None:
    for tonic in range(12):
        for minor in (False, True):
            key = Key(tonic, minor)
            for notation in KeyNotation:
                assert parse_key(format_key(key, notation)) == key
            assert key_from_mixxx_id(mixxx_key_id(key)) == key
    assert format_key(Key(9, True), KeyNotation.OPEN_KEY) == "1m"
    assert mixxx_key_id(Key(9, True)) == 22


def test_simplify_drops_per_beat_markers() -> None:
    # Rekordbox writes a TEMPO per beat after a tempo change.
    grid = [TempoMarker(25, 120, 1)] + [
        TempoMarker(48026 + 500 * i, 120, i % 4 + 1) for i in range(9)
    ]
    assert simplify(grid) == [TempoMarker(25, 120, 1)]
    changed = [TempoMarker(0, 120, 1), TempoMarker(2000, 130, 1)]
    assert simplify(changed) == changed


def test_first_downbeat() -> None:
    assert first_downbeat_ms(TempoMarker(2250, 120, 3)) == pytest.approx(1250 % 2000)
    assert first_downbeat_ms(TempoMarker(100, 120, 2)) == pytest.approx(1600)


def test_beats_and_sections_round_trip() -> None:
    grid = [TempoMarker(500, 100, 1), TempoMarker(3500, 120, 2)]
    beats = beat_positions(grid, 8000)
    assert beats[0] == (500, 1)
    sections = sections_from_beats([b for b, _ in beats])
    assert [(round(s.position_ms), round(s.bpm, 3), s.beat) for s in sections] == [
        (500, 100, 1),
        (3500, 120, 2),
    ]
