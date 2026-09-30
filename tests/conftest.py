from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

FIXTURES = Path(__file__).parent / "fixtures"

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


@pytest.fixture(scope="session")
def mixxx_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A Mixxx library with generated audio; copy it before modifying."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    from fakelib import build_mixxx_library

    root = tmp_path_factory.mktemp("lib")
    build_mixxx_library(root)
    return root


@pytest.fixture
def library_copy(mixxx_root: Path, tmp_path: Path) -> Path:
    """A private copy whose audio files a test may modify, with paths rewritten in its db."""
    import sqlite3

    root = tmp_path / "lib"
    shutil.copytree(mixxx_root, root)
    db = sqlite3.connect(root / "mixxx" / "mixxxdb.sqlite")
    for table, column in (("track_locations", "location"), ("track_locations", "directory")):
        db.execute(
            f"UPDATE {table} SET {column} = replace({column}, ?, ?)", (str(mixxx_root), str(root))
        )
    db.commit()
    db.close()
    return root
