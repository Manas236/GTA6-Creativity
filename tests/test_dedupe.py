"""Unit tests for perceptual frame hashing and consecutive frame deduplication."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import db, dedupe


def test_dhash_algorithm_deterministic() -> None:
    # 72 bytes representing 8 rows of 9 pixels
    # Row with alternating high/low bytes
    raw_pattern = bytes([255, 0, 255, 0, 255, 0, 255, 0, 255] * 8)
    h = dedupe.dhash_from_raw_gray_9x8(raw_pattern)
    assert h > 0
    # Same bytes must give identical hash
    assert dedupe.dhash_from_raw_gray_9x8(raw_pattern) == h


def test_hamming_distance() -> None:
    h1 = 0b101010
    h2 = 0b101011  # 1 bit difference
    assert dedupe.hamming_distance(h1, h2) == 1

    h3 = 0b010101  # completely inverted
    assert dedupe.hamming_distance(h1, h3) == 6


def test_consecutive_match_logic() -> None:
    target = [100, 200, 300, 400, 500]

    # Exact match of 3 consecutive frames
    cand_match = [999, 200, 300, 400, 888]
    assert dedupe.check_consecutive_match(cand_match, target, consecutive_n=3, hamming_threshold=0) is True

    # Near match within hamming distance <= 2 bits
    cand_near = [200 ^ 1, 300 ^ 2, 400 ^ 1]
    assert dedupe.check_consecutive_match(cand_near, target, consecutive_n=3, hamming_threshold=2) is True

    # No match
    cand_nomatch = [100, 999, 300, 888, 500]
    assert dedupe.check_consecutive_match(cand_nomatch, target, consecutive_n=3, hamming_threshold=0) is False


def test_extract_hashes_from_synthetic_video(synthetic_video: Path) -> None:
    hashes = dedupe.extract_1fps_hashes(synthetic_video, crop=False)
    # Synthetic video is 3 seconds at 1 fps = 3 frames
    assert len(hashes) == 3
    for sec, h in hashes:
        assert isinstance(sec, int)
        assert isinstance(h, int)


def test_dedupe_download_workflow(synthetic_video: Path, tmp_db: Path) -> None:
    # First item: unique
    is_dup1, match1 = dedupe.dedupe_download("item_orig", synthetic_video, db_path=tmp_db)
    assert is_dup1 is False
    assert match1 is None

    # Stored in DB
    fps = db.get_stored_fingerprints_by_item("download", db_path=tmp_db)
    assert "item_orig" in fps

    # Second item with identical footage: duplicate!
    is_dup2, match2 = dedupe.dedupe_download("item_repost", synthetic_video, db_path=tmp_db)
    assert is_dup2 is True
    assert match2 == "item_orig"
