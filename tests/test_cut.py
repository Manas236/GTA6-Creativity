"""Unit tests for Stage 4: Cut window selection and cut-level deduplication."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import config, cut, db, dedupe


@pytest.fixture
def temp_synthetic_video(tmp_path: Path) -> Path:
    """Generate a 28s synthetic MP4 with multiple distinct scene cuts and audio."""
    out_path = tmp_path / "test_cut_source.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=7",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=7",
        "-f", "lavfi", "-i", "color=c=green:s=320x240:d=7",
        "-f", "lavfi", "-i", "color=c=yellow:s=320x240:d=7",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=28",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v]",
        "-map", "[v]",
        "-map", "4:a",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-t", "28",
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out_path


@pytest.fixture
def temp_static_video(tmp_path: Path) -> Path:
    """Generate a 25s synthetic static video with 0 scene changes (solid black)."""
    out_path = tmp_path / "test_static.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=320x240:d=25",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=25",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-c:a", "aac",
        "-t", "25",
        str(out_path),
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    return out_path


def test_select_best_cut_window_within_bounds(temp_synthetic_video: Path):
    res = cut.select_best_cut_window(temp_synthetic_video)
    assert res is not None
    start_s, end_s, score = res
    dur = end_s - start_s
    assert 18.0 <= dur <= 22.5
    assert start_s >= 0.0
    assert end_s <= 28.0


def test_static_source_floor_rejection(temp_static_video: Path):
    """Static source with no scene cuts (< 0.05 cuts/sec) must be rejected."""
    res = cut.select_best_cut_window(temp_static_video)
    assert res is None


def test_cut_level_dedupe_detects_duplicate(tmp_db: Path):
    db.init_db(tmp_db)
    item1_id = "test_item_1"
    item2_id = "test_item_2"

    db.insert_discovered_item(
        {
            "id": item1_id,
            "source": "youtube",
            "source_id": "vid1",
            "source_url": "https://youtube.com/watch?v=vid1",
            "original_title": "Video 1",
            "stage": "cut",
        },
        db_path=tmp_db,
    )
    db.insert_discovered_item(
        {
            "id": item2_id,
            "source": "youtube",
            "source_id": "vid2",
            "source_url": "https://youtube.com/watch?v=vid2",
            "original_title": "Video 2",
            "stage": "downloaded",
        },
        db_path=tmp_db,
    )

    # Save identical 20-frame hashes for item1
    sample_hashes = [(i, 1234567890 + i) for i in range(20)]
    db.save_fingerprints(item1_id, "cut", sample_hashes, db_path=tmp_db)

    # Candidate has identical subsequence of 5 consecutive frames
    cand_hashes = [h for _, h in sample_hashes[5:10]]
    prior = db.get_stored_fingerprints_by_item("cut", exclude_item_id=item2_id, db_path=tmp_db)
    prior_hashes = [h for _, h in prior[item1_id]]

    is_dup = dedupe.check_consecutive_match(cand_hashes, prior_hashes, consecutive_n=3, hamming_threshold=8)
    assert is_dup is True
