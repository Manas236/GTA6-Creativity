"""Stage 4: Audio loudness and visual scene-cut window selection with cut-level deduplication."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from . import config, db, dedupe
from .download import verify_video_file

log = logging.getLogger(__name__)


def extract_scene_cuts(video_path: Path | str, threshold: float = 0.3) -> list[float]:
    """Extract scene change timestamps (seconds) using ffmpeg select='gt(scene,threshold)'."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(video_path),
        "-an",
        "-vf", f"fps=10,scale=320:180,select=gt(scene\\,{threshold}),showinfo",
        "-f", "null",
        "-",
    ]
    cuts: list[float] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        # Parse showinfo pts_time
        for line in res.stderr.splitlines():
            if "showinfo" in line:
                m = re.search(r"pts_time:(\d+\.?\d*)", line)
                if m:
                    cuts.append(float(m.group(1)))
    except Exception as e:
        log.warning("Scene cut detection error for %s: %s", video_path, e)
    return cuts


def extract_audio_loudness_profile(video_path: Path | str) -> list[tuple[float, float]]:
    """Extract momentary LUFS energy profile (timestamp_s, lufs) using ffmpeg ebur128."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i", str(video_path),
        "-vn",
        "-af", "ebur128=meter=100",
        "-f", "null",
        "-",
    ]
    points: list[tuple[float, float]] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        # Output lines look like: [Parsed_ebur128_0 @ ...] t: 0.1     TARGET:... M: -24.5 ...
        for line in res.stderr.splitlines():
            if "t:" in line and "M:" in line:
                m_time = re.search(r"t:\s*(\d+\.?\d*)", line)
                m_lufs = re.search(r"M:\s*(-?\d+\.?\d*)", line)
                if m_time and m_lufs:
                    t = float(m_time.group(1))
                    val = float(m_lufs.group(1))
                    # Clamp LUFS to [-70.0, 0.0]
                    val = max(-70.0, min(0.0, val))
                    points.append((t, val))
    except Exception as e:
        log.warning("Audio loudness extraction error for %s: %s", video_path, e)
    return points


def select_best_cut_window(
    video_path: Path | str,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, float, float] | None:
    """Find the optimal cut window [start_s, end_s, score] using audio energy and visual scene density.

    Returns (start_s, end_s, score) or None if source fails static floor check.
    """
    cfg = cfg or config.get_config()
    cut_cfg = cfg.get("cut", {})
    min_dur = float(cut_cfg.get("min_duration_seconds", 18.0))
    max_dur = float(cut_cfg.get("max_duration_seconds", 22.0))
    target_dur = float(cut_cfg.get("target_duration_seconds", 20.0))
    static_floor = float(cut_cfg.get("min_scene_change_density", 0.05))

    audio_weight = float(cut_cfg.get("weights", {}).get("audio_energy", 0.75))
    scene_weight = float(cut_cfg.get("weights", {}).get("visual_interest", 0.25))

    # Get total video duration
    is_valid, total_duration = verify_video_file(video_path)
    if not is_valid or total_duration < min_dur:
        log.warning("Video too short or invalid for cut: %s (duration %.1fs)", video_path, total_duration)
        return None

    # For videos shorter than or equal to target_dur, take full video
    if total_duration <= target_dur:
        return 0.0, total_duration, 1.0

    # 1. Visual scene cuts
    scene_cuts = extract_scene_cuts(video_path, threshold=0.3)
    scene_density = len(scene_cuts) / total_duration if total_duration > 0 else 0.0

    # Static-source floor check (e.g. cover art reveal / static podcasts)
    if scene_density < static_floor and len(scene_cuts) < 2:
        log.warning(
            "Source rejected by static-source floor: scene density %.3f/s < %.3f/s (cuts=%d)",
            scene_density, static_floor, len(scene_cuts)
        )
        return None

    # 2. Audio energy profile
    loudness_profile = extract_audio_loudness_profile(video_path)

    # 3. Sliding window evaluation
    step = 1.0
    w_dur = target_dur
    num_windows = max(1, int((total_duration - w_dur) / step) + 1)

    candidate_windows: list[dict[str, Any]] = []

    for i in range(num_windows):
        t_start = i * step
        t_end = t_start + w_dur
        if t_end > total_duration:
            t_end = total_duration
            t_start = max(0.0, t_end - w_dur)

        # Calculate scene cut count in this window
        cuts_in_window = sum(1 for c in scene_cuts if t_start <= c <= t_end)
        # Normalize scene score (e.g. 5 cuts in 20s = 1.0)
        norm_scene = min(1.0, cuts_in_window / 5.0)

        # Calculate mean audio loudness in this window
        window_lufs = [lufs for t, lufs in loudness_profile if t_start <= t <= t_end]
        if window_lufs:
            mean_lufs = sum(window_lufs) / len(window_lufs)
            # Map [-50, -10] to [0.0, 1.0]
            norm_audio = max(0.0, min(1.0, (mean_lufs + 50.0) / 40.0))
        else:
            norm_audio = 0.5  # fallback if no audio data

        # Intro/Outro 5% penalty
        penalty = 0.0
        if t_start < 0.05 * total_duration:
            penalty += 0.05
        if t_end > 0.95 * total_duration:
            penalty += 0.05

        score = (audio_weight * norm_audio) + (scene_weight * norm_scene) - penalty

        # Snap to nearest preceding scene cut within 1.0s
        snapped_start = t_start
        for c in scene_cuts:
            if abs(c - t_start) <= 1.0 and (t_end - c) >= min_dur and (t_end - c) <= max_dur:
                snapped_start = c
                break

        snapped_dur = t_end - snapped_start
        candidate_windows.append({
            "start": round(snapped_start, 2),
            "end": round(t_end, 2),
            "duration": round(snapped_dur, 2),
            "score": score,
        })

    # Sort candidates by score descending
    candidate_windows.sort(key=lambda w: w["score"], reverse=True)
    if not candidate_windows:
        return 0.0, min(total_duration, target_dur), 0.5

    best = candidate_windows[0]
    return best["start"], best["end"], best["score"]


def run_cut_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str]:
    """Execute cut stage for a downloaded item, with cut-level deduplication."""
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found"

    download_path_str = item.get("download_path")
    if not download_path_str or not Path(download_path_str).exists():
        db.mark_failed(item_id, "cut", "Download path missing on disk", db_path=db_path)
        return False, "Download file not found"

    download_path = Path(download_path_str)
    cut_cfg = cfg.get("cut", {})
    min_dur = float(cut_cfg.get("min_duration_seconds", 18.0))
    max_dur = float(cut_cfg.get("max_duration_seconds", 22.0))
    consecutive_n = int(cfg.get("dedupe", {}).get("consecutive_frames", 3))
    hamming_thresh = int(cfg.get("dedupe", {}).get("hamming_threshold", 8))

    # Evaluate cut window
    result = select_best_cut_window(download_path, cfg=cfg)
    if not result:
        # Static source or duration floor failed
        db.mark_rejected(item_id, "rejected: static_source (low scene density)", db_path=db_path)
        return False, "Rejected: static_source"

    start_s, end_s, score = result
    duration_s = end_s - start_s

    # Destination paths
    cuts_dir = config.media_dir("cuts")
    cut_path = cuts_dir / f"{item_id}_cut.mp4"

    # Extract cut via ffmpeg
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-ss", f"{start_s:.2f}",
        "-to", f"{end_s:.2f}",
        "-i", str(download_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        str(cut_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=60)
    except Exception as e:
        db.mark_failed(item_id, "cut", f"ffmpeg cut extraction failed: {e}", db_path=db_path)
        return False, f"ffmpeg cut extraction failed: {e}"

    # Verify cut clip
    is_valid, actual_dur = verify_video_file(cut_path)
    if not is_valid:
        cut_path.unlink(missing_ok=True)
        db.mark_failed(item_id, "cut", "Cut file failed ffprobe validation", db_path=db_path)
        return False, "Cut validation failed"

    # Cut-level deduplication: extract 1fps hashes of the cut
    cut_hashes = dedupe.extract_1fps_hashes(cut_path, crop=cfg.get("dedupe", {}).get("crop_black_bars", True))
    cand_hashes_only = [h for _, h in cut_hashes]

    # Compare against prior cut-level fingerprints
    stored_cuts = db.get_stored_fingerprints_by_item("cut", exclude_item_id=item_id, db_path=db_path)
    for prior_id, prior_tuples in stored_cuts.items():
        prior_hashes_only = [h for _, h in prior_tuples]
        if dedupe.check_consecutive_match(
            cand_hashes_only,
            prior_hashes_only,
            consecutive_n=consecutive_n,
            hamming_threshold=hamming_thresh,
        ):
            cut_path.unlink(missing_ok=True)
            db.mark_rejected(
                item_id,
                f"duplicate_cut: {consecutive_n} matching consecutive frames with {prior_id}",
                is_duplicate=True,
                db_path=db_path,
            )
            return False, f"Rejected: duplicate_cut of {prior_id}"

    # Save cut fingerprints to database
    db.save_fingerprints(item_id, "cut", cut_hashes, db_path=db_path)

    # Transition to 'cut' stage
    db.transition_stage(
        item_id,
        "cut",
        extra_fields={
            "cut_start": start_s,
            "cut_end": end_s,
            "cut_duration": actual_dur,
            "cut_path": str(cut_path),
        },
        db_path=db_path,
    )
    db.log_stage(
        item_id,
        "cut",
        "success",
        message=f"Cut window [{start_s:.1f}s - {end_s:.1f}s] extracted ({actual_dur:.1f}s)",
        db_path=db_path,
    )
    return True, f"Cut extracted successfully ({actual_dur:.1f}s)"


def run_cut_pipeline(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[str]:
    """Process all items currently in 'downloaded' stage into 'cut' stage."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    items = db.get_items_by_stage("downloaded", db_path=db_path)
    cut_ids: list[str] = []

    for item in items:
        item_id = item["id"]
        log.info("Processing cut for item %s (%s)", item_id, item.get("original_title"))
        ok, msg = run_cut_item(item_id, cfg=cfg, db_path=db_path)
        if ok:
            cut_ids.append(item_id)
        else:
            log.warning("Cut failed or rejected for item %s: %s", item_id, msg)

    return cut_ids

