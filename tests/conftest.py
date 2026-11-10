"""Pytest fixtures for unit tests with zero network calls."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Generator

import pytest

from pipeline import config, db


REAL_MEDIA_ROOT = config.ROOT_DIR / "media"


def media_snapshot(root: Path = REAL_MEDIA_ROOT) -> dict[str, tuple[int, int]]:
    """{relative path: (size, mtime_ns)} of every file under the real media/ folder."""
    if not root.exists():
        return {}
    out: dict[str, tuple[int, int]] = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def pytest_sessionstart(session: pytest.Session) -> None:
    # Taken before any test runs; tests/test_zz_media_isolation.py compares against it last.
    session.config._real_media_before = media_snapshot()  # type: ignore[attr-defined]
    session.config._real_media_existed = REAL_MEDIA_ROOT.exists()  # type: ignore[attr-defined]
    session.config._media_snapshot = media_snapshot  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def isolated_media(tmp_path: Path) -> Generator[Path, None, None]:
    """Every test writes its media under tmp_path, never into the project's real media/ folder."""
    root = tmp_path / "media"
    config.set_media_root(root)
    try:
        yield root
    finally:
        config.set_media_root(None)


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Fixture providing a temporary SQLite database initialized with schema."""
    db_file = tmp_path / "test_pipeline.db"
    db.init_db(db_file)
    return db_file


@pytest.fixture
def test_config() -> dict[str, Any]:
    """Fixture providing a clean test configuration."""
    cfg = config.get_config(reload=True)
    cfg["app"]["dry_run"] = True
    cfg["buffer"]["target_ready_items"] = 3
    return cfg


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    """Generate a valid, deterministic 3-second 320x240 synthetic video for ffmpeg tests."""
    video_path = tmp_path / "synthetic_test.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=duration=3:size=320x240:rate=1",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:duration=3",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        str(video_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return video_path

