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
    """Extract the momentary LUFS profile [(timestamp_s, lufs)] every 100 ms via ebur128.

    `ebur128` emits one measurement per 100 ms block; `M` is the momentary loudness of the
    400 ms window ENDING at `t` (callers that need sample-accurate boundaries re-centre it,
    see style.dead_air.loudness_lag_s).

    NOTE: this used to pass `meter=100`, but ebur128's `meter` is the visual meter's LU
    range and only accepts 9-18, so the filter failed to initialise and the profile came
    back EMPTY on every call - silently making the cut window's audio-energy term a
    constant and disabling every loudness-trough fallback. Both the log lines and the
    frame metadata are parsed now, and an empty result is logged.

    Digital silence makes ebur128 report `nan` / `-inf`; those are mapped to the -70 LUFS
    floor rather than skipped, because dropping them would punch a hole in the profile at
    exactly the moments dead-air detection cares about.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-loglevel", "info",
        "-i", str(video_path),
        "-vn",
        "-af", "ebur128=metadata=1,ametadata=print:key=lavfi.r128.M",
        "-f", "null",
        "-",
    ]
    num = r"(-?(?:\d+\.?\d*|inf|nan))"

    def clamp(raw: str) -> float:
        try:
            val = float(raw)
        except ValueError:
            return -70.0
        if val != val or val == float("-inf"):   # nan / -inf == digital silence
            return -70.0
        return max(-70.0, min(0.0, val))

    points: list[tuple[float, float]] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        pending: float | None = None
        for line in res.stderr.splitlines():
            # ebur128's own log line: "t: 0.1  TARGET:-23 LUFS  M: -24.5  S: ..."
            if "t:" in line and " M:" in line:
                m_time = re.search(r"t:\s*(\d+\.?\d*)", line)
                m_lufs = re.search(rf"M:\s*{num}", line)
                if m_time and m_lufs:
                    points.append((float(m_time.group(1)), clamp(m_lufs.group(1))))
                continue
            # ametadata fallback: "pts_time:5.1" then "lavfi.r128.M=-43.810"
            m = re.search(r"pts_time:(-?\d+\.?\d*)", line)
            if m:
                pending = float(m.group(1))
                continue
            m = re.search(rf"lavfi\.r128\.M={num}", line)
            if m and pending is not None:
                points.append((pending, clamp(m.group(1))))
                pending = None
    except Exception as e:
        log.warning("Audio loudness extraction error for %s: %s", video_path, e)
    if not points:
        log.warning("ebur128 returned no loudness samples for %s", video_path)
    # Both parsers may fire on the same run; de-duplicate on the timestamp.
    seen: dict[float, float] = {}
    for t, v in points:
        seen.setdefault(round(t, 3), v)
    return sorted(seen.items())


def _word_pauses(words: list[dict[str, Any]], min_gap: float) -> list[tuple[float, float]]:
    """Return speech pauses (gap_start, gap_end) of at least min_gap seconds between words.

    The region after the last word is an open-ended pause (gap_end = inf).
    """
    ws = sorted(words, key=lambda w: float(w["start"]))
    pauses: list[tuple[float, float]] = []
    for a, b in zip(ws, ws[1:]):
        gs, ge = float(a["end"]), float(b["start"])
        if ge - gs >= min_gap:
            pauses.append((gs, ge))
    if ws:
        pauses.append((float(ws[-1]["end"]), math.inf))
    return pauses


def snap_end_to_pause(
    start_s: float,
    proposed_end_s: float,
    *,
    words: list[dict[str, Any]] | None = None,
    loudness: list[tuple[float, float]] | None = None,
    min_dur: float = 18.0,
    max_dur: float = 22.0,
    min_gap: float = 0.25,
    hard_max_end: float | None = None,
) -> float:
    """Snap a cut END to a speech pause or loudness trough within [start+min_dur, start+max_dur].

    Priority:
      1. Whisper word gap >= min_gap (end placed inside the gap, closest to proposed end;
         never less than 0.1 s after the last spoken word).
      2. ebur128 momentary-loudness trough (3-point smoothed minimum; ties within 1 LU go
         to the point closest to the proposed end).
      3. The proposed end, clamped to the bounds.
    Times are in the same timeline as `words` / `loudness` (absolute in the source).
    """
    lo = start_s + min_dur
    hi = start_s + max_dur
    if hard_max_end is not None:
        hi = min(hi, hard_max_end)
    if hi < lo:
        return round(min(proposed_end_s, hi), 3)
    target = min(max(proposed_end_s, lo), hi)

    if words:
        best: tuple[float, float] | None = None  # (distance, point)
        for gs, ge in _word_pauses(words, min_gap):
            f_lo = max(lo, gs + 0.1)
            f_hi = min(hi, ge - 0.05)
            if f_lo > f_hi:
                continue
            mid = gs + min((ge - gs) / 2.0, 0.4)  # sit a little after the word ends
            point = min(max(target, f_lo), f_hi) if ge == math.inf else min(max(mid, f_lo), f_hi)
            dist = abs(point - target)
            if best is None or dist < best[0]:
                best = (dist, point)
        if best is not None:
            return round(best[1], 3)

    if loudness:
        pts = sorted(loudness)
        smoothed: list[tuple[float, float]] = []
        for i, (t, _) in enumerate(pts):
            window = [v for _, v in pts[max(0, i - 1): i + 2]]
            smoothed.append((t, sum(window) / len(window)))
        in_bounds = [(t, v) for t, v in smoothed if lo <= t <= hi]
        if in_bounds:
            v_min = min(v for _, v in in_bounds)
            near_min = [t for t, v in in_bounds if v <= v_min + 1.0]
            return round(min(near_min, key=lambda t: abs(t - target)), 3)

    return round(target, 3)


def select_best_cut_window(
    video_path: Path | str,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, float, float] | None:
    """Find the optimal cut window [start_s, end_s, score] using audio energy and visual scene density.

    Returns (start_s, end_s, score) or None if source fails static floor check.
    """
    cfg = cfg or config.get_config()
    cut_cfg = cfg.get("cut", {})
    min_dur = float(cut_cfg.get("min_duration", cut_cfg.get("min_duration_seconds", 18.0)))
    max_dur = float(cut_cfg.get("max_duration", cut_cfg.get("max_duration_seconds", 22.0)))
    target_dur = float(cut_cfg.get("target_duration", cut_cfg.get("target_duration_seconds", 20.0)))
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
    # Snap the END to a loudness trough inside the 18-22 s bounds so the clip does not
    # stop mid-sound (whisper words are not available at this stage; the render stage
    # refines the end with word gaps).
    snapped_end = snap_end_to_pause(
        best["start"],
        best["end"],
        loudness=loudness_profile,
        min_dur=min_dur,
        max_dur=max_dur,
        hard_max_end=total_duration,
    )
    return best["start"], round(snapped_end, 2), best["score"]


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

