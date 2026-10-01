from __future__ import annotations

import pytest

from cratemover.cli import KEY_STYLES, keys_strip, keys_table, main
from cratemover.keys import parse_key


def test_keys_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keys"]) == 0
    out = capsys.readouterr().out
    assert "8A  Am" in out and "8B  C " in out and "Abm/G#m" in out


def test_keys_highlights_a_key_and_its_neighbours() -> None:
    table = keys_table(parse_key("Am"))
    styles = {
        str(cell).split()[0]: cell.style
        for column in table.columns
        for cell in column._cells
        if cell.style
    }
    assert styles == {
        "8A": KEY_STYLES["key"],
        "7A": KEY_STYLES["next"], "9A": KEY_STYLES["next"], "8B": KEY_STYLES["next"],
        "6A": KEY_STYLES["further"], "10A": KEY_STYLES["further"],
        "7B": KEY_STYLES["further"], "9B": KEY_STYLES["further"],
    }  # fmt: skip


def test_keys_rejects_nonsense(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["keys", "nope"]) == 1


def test_keys_strip_is_a_slice_of_the_wheel() -> None:
    table = keys_strip(parse_key("Am"))  # type: ignore[arg-type]
    rows = [[str(column._cells[r]).split("\n")[1] for column in table.columns] for r in (0, 1)]
    assert rows == [["6A", "7A", "8A", "9A", "10A"], ["6B", "7B", "8B", "9B", "10B"]]
    style = {str(c).split("\n")[1]: c.style for column in table.columns for c in column._cells}
    assert style["8A"] == KEY_STYLES["key"]
    assert {k for k, v in style.items() if v == KEY_STYLES["next"]} == {"7A", "9A", "8B"}
    assert {k for k, v in style.items() if v == KEY_STYLES["further"]} == {"6A", "10A", "7B", "9B"}
    assert style["6B"] == style["10B"] == "dim"  # on the slice, but don't mix with Am
