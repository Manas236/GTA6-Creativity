"""Perceptual video fingerprinting with black-bar cropping, 1 fps dHash, and coverage-based deduplication."""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from . import config, db

log = logging.getLogger(__name__)


def detect_crop(video_path: Path | str) -> str:
    """Run ffmpeg cropdetect to find active picture area (removing black bars)."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-ss", "2",
        "-i", str(video_path),
        "-vframes", "10",
        "-vf", "cropdetect=24:16:0",
        "-f", "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        matches = re.findall(r"crop=(\d+:\d+:\d+:\d+)", res.stderr)
        if matches:
            return f"crop={matches[-1]}"
    except Exception as e:
        log.warning("Crop detection failed for %s: %s", video_path, e)
    return ""


def dhash_from_raw_gray_9x8(raw_bytes: bytes) -> int:
    """Compute 64-bit difference hash (dHash) from 72 bytes of 9x8 grayscale image."""
    if len(raw_bytes) < 72:
        return 0
    h = 0
    for y in range(8):
        row_offset = y * 9
        for x in range(8):
            left = raw_bytes[row_offset + x]
            right = raw_bytes[row_offset + x + 1]
            if left > right:
                h |= 1 << (y * 8 + x)
    return h


def extract_1fps_hashes(video_path: Path | str, crop: bool = True) -> list[tuple[int, int]]:
    """Extract 1 fps dHashes from video. Returns list of (second_offset, uint64_hash)."""
    filters: list[str] = []
    if crop:
        crop_filter = detect_crop(video_path)
        if crop_filter:
            filters.append(crop_filter)
    filters.extend(["fps=1", "scale=9:8"])
    vf_arg = ",".join(filters)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(video_path),
        "-vf", vf_arg,
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=90)
        raw_data = proc.stdout
    except Exception as e:
        log.error("Failed to extract frames from %s: %s", video_path, e)
        return []

    hashes: list[tuple[int, int]] = []
    frame_len = 72
    num_frames = len(raw_data) // frame_len

    for sec in range(num_frames):
        chunk = raw_data[sec * frame_len : (sec + 1) * frame_len]
        h = dhash_from_raw_gray_9x8(chunk)
        hashes.append((sec, h))

    return hashes


def hamming_distance(h1: int, h2: int) -> int:
    """Return bit difference count between two 64-bit integers."""
    return (h1 ^ h2).bit_count()


def compute_overlap_coverage(
    candidate_hashes: list[int],
    target_hashes: list[int],
    hamming_threshold: int = 8,
) -> tuple[float, int, int]:
    """Compute maximum temporal alignment overlap coverage between candidate and target video.

    Returns (coverage_ratio, max_matching_frames, min_length).
    """
    len_c = len(candidate_hashes)
    len_t = len(target_hashes)
    min_len = min(len_c, len_t)
    if min_len == 0:
        return 0.0, 0, 0

    max_matches = 0
    # Search all relative offsets delta = t_idx - c_idx
    # When delta >= 0, target starts at delta relative to candidate start
    # When delta < 0, candidate starts at -delta relative to target start
    for delta in range(-(len_c - 1), len_t):
        matches = 0
        c_start = max(0, -delta)
        c_end = min(len_c, len_t - delta)
        for c_idx in range(c_start, c_end):
            t_idx = c_idx + delta
            if hamming_distance(candidate_hashes[c_idx], target_hashes[t_idx]) <= hamming_threshold:
                matches += 1
        if matches > max_matches:
            max_matches = matches

    coverage = max_matches / float(min_len)
    return coverage, max_matches, min_len


def check_consecutive_match(
    candidate_hashes: list[int],
    target_hashes: list[int],
    consecutive_n: int = 3,
    hamming_threshold: int = 8,
) -> bool:
    """Check if candidate contains consecutive_n frames matching target (used at cut-level in CP2)."""
    cand_len = len(candidate_hashes)
    targ_len = len(target_hashes)

    if cand_len < consecutive_n or targ_len < consecutive_n:
        return False

    for i in range(cand_len - consecutive_n + 1):
        for j in range(targ_len - consecutive_n + 1):
            matched = True
            for k in range(consecutive_n):
                dist = hamming_distance(candidate_hashes[i + k], target_hashes[j + k])
                if dist > hamming_threshold:
                    matched = False
                    break
            if matched:
                return True
    return False


def dedupe_download(
    item_id: str,
    video_path: Path | str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str | None, float]:
    """Fingerprint downloaded video and check against all prior download fingerprints using coverage.

    Returns (is_duplicate, matching_item_id, max_coverage).
    """
    cfg = cfg or config.get_config()
    dedupe_cfg = cfg.get("dedupe", {})
    crop_bars = dedupe_cfg.get("crop_black_bars", True)
    coverage_thresh = float(dedupe_cfg.get("download_coverage_threshold", 0.65))
    hamming_thresh = int(dedupe_cfg.get("hamming_threshold", 8))

    hashes = extract_1fps_hashes(video_path, crop=crop_bars)
    if not hashes:
        log.warning("No hashes extracted for item %s", item_id)
        return False, None, 0.0

    candidate_hash_vals = [h for _, h in hashes]

    # Query all stored download fingerprints grouped by item
    stored_by_item = db.get_stored_fingerprints_by_item("download", exclude_item_id=item_id, db_path=db_path)

    highest_coverage = 0.0
    best_matching_id = None

    for prior_item_id, prior_tuples in stored_by_item.items():
        prior_hash_vals = [h for _, h in prior_tuples]
        cov, match_count, min_len = compute_overlap_coverage(
            candidate_hash_vals,
            prior_hash_vals,
            hamming_threshold=hamming_thresh,
        )
        if cov > highest_coverage:
            highest_coverage = cov
            best_matching_id = prior_item_id

        if cov >= coverage_thresh:
            log.info(
                "Item %s is duplicate of %s (coverage %.1f%% [%d/%ds] >= %.1f%%)",
                item_id,
                prior_item_id,
                cov * 100,
                match_count,
                min_len,
                coverage_thresh * 100,
            )
            return True, prior_item_id, cov

    # If not duplicate, save candidate fingerprints
    db.save_fingerprints(item_id, "download", hashes, db_path=db_path)
    return False, None, highest_coverage
