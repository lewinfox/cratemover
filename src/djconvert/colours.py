"""Track and cue colour palettes, and the mapping between them.

Palettes are from Mixxx's ``src/util/color/predefinedcolorpalettes.cpp`` and
the Rekordbox XML format specification.
"""

from __future__ import annotations

from collections.abc import Sequence


def rgb(colour: int) -> tuple[int, int, int]:
    return (colour >> 16) & 0xFF, (colour >> 8) & 0xFF, colour & 0xFF


def nearest(colour: int, palette: Sequence[int]) -> int:
    r, g, b = rgb(colour)

    def distance(candidate: int) -> int:
        cr, cg, cb = rgb(candidate)
        # Weighted Euclidean distance: a cheap approximation of perceived difference.
        return 2 * (r - cr) ** 2 + 4 * (g - cg) ** 2 + 3 * (b - cb) ** 2

    return min(palette, key=distance)


# The only track colours Rekordbox recognises in XML (value, name in the UI).
REKORDBOX_TRACK_COLOURS: dict[int, str] = {
    0xFF007F: "Pink",
    0xFF0000: "Red",
    0xFFA500: "Orange",
    0xFFFF00: "Yellow",
    0x00FF00: "Green",
    0x25FDE9: "Aqua",
    0x0000FF: "Blue",
    0x660099: "Purple",
}

# Rekordbox's hot cue colour picker.
REKORDBOX_CUE_COLOURS = [
    0xDE44CF, 0xB432FF, 0xAA42FF, 0x6473FF, 0x305AFF, 0x50B4FF, 0x00E0FF, 0x1FA382,
    0x10B176, 0x28E214, 0xA5E116, 0xB4BE04, 0xC3AF04, 0xE0641B, 0xE62828, 0xFF127B,
]  # fmt: skip
REKORDBOX_DEFAULT_CUE = 0x28E214

# Serato stores hot cue colours from an old palette in the file tags, and shows
# the colour at the same index of its DJ Pro palette.
SERATO_STORED_CUE_COLOURS = [
    0xCC0000, 0xCC4400, 0xCC8800, 0xCCCC00, 0x88CC00, 0x44CC00, 0x00CC00, 0x00CC44, 0x00CC88,
    0x00CCCC, 0x0088CC, 0x0044CC, 0x0000CC, 0x4400CC, 0x8800CC, 0xCC00CC, 0xCC0088, 0xCC0044,
]  # fmt: skip
SERATO_DISPLAYED_CUE_COLOURS = [
    0xC02626, 0xDB4E27, 0xF8821A, 0xFAC313, 0x4EB648, 0x006838, 0x1FAD26, 0x8DC63F, 0x2B3673,
    0x1DBEBD, 0x0F88CA, 0x16308B, 0x173BA2, 0x5C3F97, 0x6823B6, 0xCE359E, 0xDC1D49, 0xC71136,
]  # fmt: skip
# Serato's default colours for hot cues 1-8, as indices into the palettes above.
SERATO_DEFAULT_CUE_INDICES = [0, 2, 12, 3, 6, 15, 9, 14]
SERATO_LOOP_COLOUR = 0x27AAE1  # saved loops always have this colour
SERATO_NO_COLOUR = 0xFFFFFF  # stored value for "no colour"

SERATO_TRACK_COLOURS = [
    0x333333, 0x555555, 0x993399, 0x993377, 0x993355, 0x993333, 0x995533, 0x997733, 0x999933,
    0x779933, 0x559933, 0x339933, 0x339955, 0x339977, 0x339999, 0x337799, 0x335599, 0x333399,
    0x553399, 0x773399,
]  # fmt: skip

MIXXX_CUE_COLOURS = [0xC50A08, 0x32BE44, 0x42D4F4, 0xF8D200, 0x0044FF, 0xAF00CC, 0xFCA6D7, 0xF2F2FF]


def serato_cue_to_display(stored: int) -> int | None:
    if stored == SERATO_NO_COLOUR:
        return None
    if stored in SERATO_STORED_CUE_COLOURS:
        return SERATO_DISPLAYED_CUE_COLOURS[SERATO_STORED_CUE_COLOURS.index(stored)]
    return stored


def serato_cue_from_display(colour: int | None, slot: int = 0) -> int:
    """The value to store for a hot cue colour: the nearest Serato palette entry."""
    if colour is None:
        index = SERATO_DEFAULT_CUE_INDICES[slot % len(SERATO_DEFAULT_CUE_INDICES)]
        return SERATO_STORED_CUE_COLOURS[index]
    shown = nearest(colour, SERATO_DISPLAYED_CUE_COLOURS)
    return SERATO_STORED_CUE_COLOURS[SERATO_DISPLAYED_CUE_COLOURS.index(shown)]


def serato_track_to_display(stored: int) -> int | None:
    """Serato stores track colours shifted by 0x666666 (Mixxx ``SeratoStoredTrackColor``)."""
    if stored == SERATO_NO_COLOUR:
        return None
    if stored == 0x999999:
        return 0x090909
    if stored == 0x000000:
        return 0x333333
    return stored + 0x99999A if stored < 0x666666 else stored - 0x666666


def serato_track_from_display(colour: int | None) -> int:
    if colour is None:
        return SERATO_NO_COLOUR
    shown = nearest(colour, SERATO_TRACK_COLOURS)
    if shown == 0x333333:
        return 0x000000
    return shown + 0x666666 if shown < 0x99999A else shown - 0x99999A


def rekordbox_track_colour(colour: int | None) -> int | None:
    if colour is None or not 0 <= colour <= 0xFFFFFF:
        return None
    return nearest(colour, list(REKORDBOX_TRACK_COLOURS))
