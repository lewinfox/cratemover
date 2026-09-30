"""Path rewrite rules and finding a library's files on this machine."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .model import normalise_path


def apply_rules(path: str, rules: list[tuple[str, str]]) -> str:
    """Replace the longest matching prefix. Matching ignores slash direction and case."""
    norm = normalise_path(path)
    best: tuple[str, str] | None = None
    for old, new in rules:
        old_n = normalise_path(old).rstrip("/")
        if not old_n:
            continue
        if (norm.lower() == old_n.lower() or norm.lower().startswith(old_n.lower() + "/")) and (
            best is None or len(old_n) > len(normalise_path(best[0]).rstrip("/"))
        ):
            best = (old, new)
    if best is None:
        return path
    old_n = normalise_path(best[0]).rstrip("/")
    new = best[1].rstrip("/\\")
    rest = norm[len(old_n) :]
    result = new + rest
    return result.replace("/", "\\") if "\\" in best[1] else result


def parse_rules(text: str | list[Any] | None) -> list[tuple[str, str]]:
    """Rules as ``[[from, to], ...]`` or lines of ``from => to``."""
    if not text:
        return []
    if isinstance(text, list):
        return [(str(a), str(b)) for a, b in text if str(a).strip()]
    rules = []
    for line in text.splitlines():
        if "=>" in line:
            old, new = line.split("=>", 1)
            if old.strip():
                rules.append((old.strip(), new.strip()))
    return rules


def make_resolver(access_rules: list[tuple[str, str]]) -> Callable[[str], Path | None]:
    """Library location -> an existing local file, applying the file-access rules."""

    def resolve(location: str) -> Path | None:
        candidate = apply_rules(location, access_rules)
        if len(candidate) >= 2 and candidate[1] == ":" and os.name != "nt":
            return None  # a Windows path we have no mapping for
        path = Path(candidate)
        return path if path.is_file() else None

    return resolve
