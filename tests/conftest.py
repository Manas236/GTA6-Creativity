"""Pytest fixtures for unit tests with zero network calls."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Generator

import pytest

from pipeline import config, db


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

