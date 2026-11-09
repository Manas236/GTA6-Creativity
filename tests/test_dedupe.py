"""Unit tests for perceptual frame hashing and coverage-based deduplication."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import db, dedupe


def test_dhash_algorithm_deterministic() -> None:
    raw_pattern = bytes([255, 0, 255, 0, 255, 0, 255, 0, 255] * 8)
    h = dedupe.dhash_from_raw_gray_9x8(raw_pattern)
    assert h > 0
    assert dedupe.dhash_from_raw_gray_9x8(raw_pattern) == h


def test_hamming_distance() -> None:
    h1 = 0b101010
    h2 = 0b101011
    assert dedupe.hamming_distance(h1, h2) == 1
    h3 = 0b010101
    assert dedupe.hamming_distance(h1, h3) == 6


def test_coverage_based_dedupe_same_video_caught() -> None:
    # 20-second video with distinct 64-bit hashes
    base_hashes = [(i * 0x5555555555555555) & 0xFFFFFFFFFFFFFFFF for i in range(20)]

    # Exact duplicate (20/20 frames match = 100% coverage)
    cov, matches, min_len = dedupe.compute_overlap_coverage(base_hashes, base_hashes)
    assert cov == 1.0
    assert matches == 20
    assert min_len == 20

    # Slight edit with 2 second shift and 18 matching frames (18/20 = 90% coverage)
    shifted_hashes = [0x1122334455667788, 0x99AABBCCDDEEFF00] + base_hashes
    cov_shifted, matches_s, min_len_s = dedupe.compute_overlap_coverage(shifted_hashes, base_hashes)
    assert pytest.approx(cov_shifted, 0.01) == 1.0  # All 20 base frames match the shorter video!


def test_shared_3s_segment_not_flagged_as_duplicate() -> None:
    # Generate realistic pseudo-random 64-bit hashes with ~32 bits set
    import hashlib
    def make_hash(label: str) -> int:
        return int(hashlib.sha256(label.encode()).hexdigest()[:16], 16)

    # Reaction video (30 seconds) that includes 3 seconds of a 20-second trailer
    trailer_hashes = [make_hash(f"trailer_sec_{i}") for i in range(20)]
    reaction_hashes = (
        [make_hash(f"reaction_intro_{i}") for i in range(15)]
        + trailer_hashes[5:8]
        + [make_hash(f"reaction_outro_{i}") for i in range(12)]
    )

    # Length of shorter video is 20s. Overlap is exactly 3s.
    cov, matches, min_len = dedupe.compute_overlap_coverage(reaction_hashes, trailer_hashes)
    assert matches == 3
    assert min_len == 20
    assert pytest.approx(cov, 0.01) == 3 / 20  # 15% coverage << 65% threshold
    assert cov < 0.65  # NOT flagged as download-level duplicate!


def test_extract_hashes_from_synthetic_video(synthetic_video: Path) -> None:
    hashes = dedupe.extract_1fps_hashes(synthetic_video, crop=False)
    assert len(hashes) == 3
    for sec, h in hashes:
        assert isinstance(sec, int)
        assert isinstance(h, int)


def test_dedupe_download_workflow(synthetic_video: Path, tmp_db: Path) -> None:
    is_dup1, match1, cov1 = dedupe.dedupe_download("item_orig", synthetic_video, db_path=tmp_db)
    assert is_dup1 is False
    assert match1 is None

    fps = db.get_stored_fingerprints_by_item("download", db_path=tmp_db)
    assert "item_orig" in fps

    is_dup2, match2, cov2 = dedupe.dedupe_download("item_repost", synthetic_video, db_path=tmp_db)
    assert is_dup2 is True
    assert match2 == "item_orig"
    assert cov2 == 1.0


def test_letterboxed_synthetic_video_caught_as_duplicate(tmp_path: Path, tmp_db: Path) -> None:
    import subprocess
    base_mp4 = tmp_path / "base_16_9.mp4"
    padded_9_16 = tmp_path / "padded_9_16.mp4"
    padded_4_3 = tmp_path / "padded_4_3.mp4"

    # Generate 5-second 16:9 dynamic synthetic video (mandelbrot so it has non-black visual content across frame)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "mandelbrot=size=320x180:rate=10", "-t", "5", "-c:v", "libx264", str(base_mp4)],
        capture_output=True,
        check=True,
    )
    # Pad to 9:16 (320x568) with black bars
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(base_mp4), "-vf", "pad=320:568:0:(568-180)/2:black", "-c:v", "libx264", str(padded_9_16)],
        capture_output=True,
        check=True,
    )
    # Pad to 4:3 (320x240) with black bars
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(base_mp4), "-vf", "pad=320:240:0:(240-180)/2:black", "-c:v", "libx264", str(padded_4_3)],
        capture_output=True,
        check=True,
    )

    # 1. Download base video into DB
    is_dup_base, _, _ = dedupe.dedupe_download("item_base", base_mp4, db_path=tmp_db)
    assert is_dup_base is False

    # 2. Test 9:16 padded copy - smart cropdetect must remove black bars and flag duplicate
    is_dup_9_16, match_id_9_16, cov_9_16 = dedupe.dedupe_download("item_padded_9_16", padded_9_16, db_path=tmp_db)
    assert is_dup_9_16 is True
    assert match_id_9_16 == "item_base"
    assert cov_9_16 >= 0.65

    # 3. Test 4:3 padded copy - smart cropdetect must remove black bars and flag duplicate
    is_dup_4_3, match_id_4_3, cov_4_3 = dedupe.dedupe_download("item_padded_4_3", padded_4_3, db_path=tmp_db)
    assert is_dup_4_3 is True
    assert match_id_4_3 == "item_base"
    assert cov_4_3 >= 0.65
