"""Perceptual video fingerprinting with black-bar cropping, 1 fps dHash, and consecutive matching."""
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
        # Match crop=w:h:x:y
        matches = re.findall(r"crop=(\d+:\d+:\d+:\d+)", res.stderr)
        if matches:
            # Return the most frequent crop
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
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
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


def check_consecutive_match(
    candidate_hashes: list[int],
    target_hashes: list[int],
    consecutive_n: int = 3,
    hamming_threshold: int = 8,
) -> bool:
    """Check if candidate contains consecutive_n frames matching target within hamming_threshold."""
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
) -> tuple[bool, str | None]:
    """Fingerprint downloaded video and check against all prior download fingerprints.

    Returns (is_duplicate, matching_item_id).
    """
    cfg = cfg or config.get_config()
    dedupe_cfg = cfg.get("dedupe", {})
    crop_bars = dedupe_cfg.get("crop_black_bars", True)
    consecutive_n = int(dedupe_cfg.get("consecutive_frames", 3))
    threshold = int(dedupe_cfg.get("hamming_threshold", 8))

    hashes = extract_1fps_hashes(video_path, crop=crop_bars)
    if not hashes:
        log.warning("No hashes extracted for item %s", item_id)
        return False, None

    candidate_hash_vals = [h for _, h in hashes]

    # Query all stored download fingerprints grouped by item
    stored_by_item = db.get_stored_fingerprints_by_item("download", exclude_item_id=item_id, db_path=db_path)

    for prior_item_id, prior_tuples in stored_by_item.items():
        prior_hash_vals = [h for _, h in prior_tuples]
        if check_consecutive_match(
            candidate_hash_vals,
            prior_hash_vals,
            consecutive_n=consecutive_n,
            hamming_threshold=threshold,
        ):
            log.info("Item %s is duplicate of item %s (%d consecutive frames match)", item_id, prior_item_id, consecutive_n)
            return True, prior_item_id

    # If not a duplicate, save candidate fingerprints
    db.save_fingerprints(item_id, "download", hashes, db_path=db_path)
    return False, None
