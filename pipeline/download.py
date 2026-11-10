"""Media downloader using yt-dlp with pull-based buffering, atomic renames, and crash safety."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yt_dlp

from . import config, db, dedupe, rate_limit
from .errors import DownloadError, RateLimitError

log = logging.getLogger(__name__)


def verify_video_file(path: Path | str) -> tuple[bool, float]:
    """Verify video file exists and passes ffprobe. Returns (is_valid, duration)."""
    p = Path(path)
    if not p.exists() or p.stat().st_size < 1024:
        return False, 0.0

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(p),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        has_video = any("video" in line for line in lines)
        duration = 0.0
        for line in lines:
            try:
                duration = float(line)
                break
            except ValueError:
                continue
        if has_video and duration > 0.5:
            return True, duration
    except Exception as e:
        log.warning("ffprobe check failed for %s: %s", path, e)
    return False, 0.0


def format_selector(cfg: dict[str, Any] | None = None) -> str:
    """The yt-dlp format selector: H.264 (avc1) capped at `download.max_height`, never AV1.

    yt-dlp's old `bestvideo[ext=mp4]` picked AV1 1440p60, which the laptop's ffmpeg decodes at
    ~0.4x real time - dedupe and scene detection then ran into their timeouts. The chain, first
    match wins:
      1. H.264 video <= max_height + m4a (AAC) audio   - the normal YouTube case, no re-encode
      2. H.264 video <= max_height + any audio
      3. a single progressive H.264 file <= max_height
      4. any NON-AV1 video <= max_height + audio       - e.g. VP9 when a video has no avc1 at all
      5. a single file <= max_height whose codec is not AV1, unknown height/codec allowed
                                                       - Twitch clips, which report no codec
    There is deliberately no "anything at all" fallback: a source that only exists as AV1 or
    above max_height fails the download instead of being picked.
    `download.format` in config.yaml replaces the whole chain.
    """
    dl = ((cfg or {}).get("download") or {})
    if dl.get("format"):
        return str(dl["format"])
    h = int(dl.get("max_height", 1080))
    return "/".join(
        [
            f"bv[vcodec^=avc1][height<={h}]+ba[ext=m4a]",
            f"bv[vcodec^=avc1][height<={h}]+ba",
            f"b[vcodec^=avc1][height<={h}]",
            f"bv[vcodec!^=av01][height<={h}]+ba",
            f"b[vcodec!^=?av01][height<=?{h}]",
        ]
    )


def probe_video_stream(path: Path | str) -> tuple[str, int]:
    """(codec_name, height) of the first video stream, or ("", 0) when ffprobe cannot tell."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,height", "-of", "csv=p=0", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
        codec, _, height = out.splitlines()[0].partition(",")
        return codec.strip().lower(), int(float(height or 0))
    except Exception as e:  # noqa: BLE001
        log.warning("ffprobe stream check failed for %s: %s", path, e)
        return "", 0


def source_is_acceptable(path: Path | str, cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Fail closed if a download still came out as AV1 or above the height cap (the selector's
    last step accepts files whose codec the site does not report)."""
    dl = ((cfg or {}).get("download") or {})
    max_height = int(dl.get("max_height", 1080))
    rejected = {str(c).lower() for c in dl.get("reject_codecs", ["av1"])}
    codec, height = probe_video_stream(path)
    if codec in rejected:
        return False, f"source is {codec} ({height}p); only H.264/VP9 sources are processed"
    # A vertical 1080x1920 clip has height 1920; compare the short side to the cap.
    if height > max_height and not _short_side_within(path, max_height):
        return False, f"source is {height}p, above the {max_height}p cap"
    return True, f"{codec or 'unknown'} {height}p"


def _short_side_within(path: Path | str, max_height: int) -> bool:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
    ]
    try:
        w, _, h = subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip().partition(",")
        return min(int(w), int(h)) <= max_height
    except Exception:  # noqa: BLE001
        return False


def download_single_item(
    item: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str]:
    """Download single item to temporary file, verify via ffprobe, atomically rename, and dedupe.

    Returns (success, message).
    """
    item_id = item["id"]
    source = item["source"]
    source_url = item["source_url"]

    # Check source rate limit
    paused, why = rate_limit.is_source_paused(source, db_path)
    if paused:
        return False, f"Source paused: {why}"

    downloads_dir = config.media_dir("downloads")
    final_path = downloads_dir / f"{item_id}.mp4"
    temp_pattern = downloads_dir / f"tmp_{item_id}.%(ext)s"
    temp_expected = downloads_dir / f"tmp_{item_id}.mp4"

    # Clean up any leftover temporary files from prior attempts
    for leftover in downloads_dir.glob(f"tmp_{item_id}.*"):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass

    ydl_opts = {
        "format": format_selector(cfg or config.get_config()),
        "outtmpl": str(temp_pattern),
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "merge_output_format": "mp4",
        "overwrites": True,
        "retries": 3,
    }

    t0 = time.time()
    db.log_stage(item_id, "download", "start", message="Starting yt-dlp download", db_path=db_path)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([source_url])
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg or "Too Many Requests" in err_msg:
            rate_limit.record_error(source, "HTTP 429 during download", pause_hours=3.0, db_path=db_path)
            db.mark_failed(item_id, "download", f"Rate limited: {err_msg}", db_path=db_path)
            return False, f"Rate limited: {err_msg}"
        db.mark_failed(item_id, "download", f"Download failed: {err_msg}", db_path=db_path)
        return False, f"Download failed: {err_msg}"

    # Locate the actual downloaded temp file (could be .mp4, .mkv, etc. before merge)
    actual_temp = None
    if temp_expected.exists():
        actual_temp = temp_expected
    else:
        candidates = list(downloads_dir.glob(f"tmp_{item_id}.*"))
        if candidates:
            actual_temp = candidates[0]

    if not actual_temp or not actual_temp.exists():
        db.mark_failed(item_id, "download", "Downloaded file not found on disk", db_path=db_path)
        return False, "File not found"

    # Verify integrity via ffprobe
    is_valid, duration = verify_video_file(actual_temp)
    if not is_valid:
        actual_temp.unlink(missing_ok=True)
        db.mark_failed(item_id, "download", "ffprobe validation failed (corrupt or unreadable media)", db_path=db_path)
        return False, "ffprobe validation failed"

    acceptable, why = source_is_acceptable(actual_temp, cfg or config.get_config())
    if not acceptable:
        actual_temp.unlink(missing_ok=True)
        db.mark_failed(item_id, "download", f"Unsupported source format: {why}", db_path=db_path)
        return False, f"Unsupported source format: {why}"

    # Atomically rename to final file
    try:
        os.replace(str(actual_temp), str(final_path))
    except Exception as e:
        db.mark_failed(item_id, "download", f"Atomic rename failed: {e}", db_path=db_path)
        return False, f"Atomic rename failed: {e}"

    duration_s = time.time() - t0

    # Run Level-2 Perceptual Deduplication against all prior downloads (coverage-based)
    is_dup, matching_id, cov = dedupe.dedupe_download(item_id, final_path, cfg=cfg, db_path=db_path)
    if is_dup:
        db.mark_rejected(
            item_id,
            f"duplicate_download: matches {matching_id} (coverage {cov*100:.1f}%)",
            is_duplicate=True,
            db_path=db_path,
        )
        db.log_stage(
            item_id,
            "download",
            "rejected",
            message=f"Duplicate of {matching_id} ({cov*100:.1f}% coverage)",
            duration_s=duration_s,
            db_path=db_path,
        )
        return False, f"Rejected: Duplicate of {matching_id} ({cov*100:.1f}%)"

    # Transition item to 'downloaded' stage
    db.transition_stage(
        item_id,
        "downloaded",
        extra_fields={
            "download_path": str(final_path),
            "downloaded_at": db.now(),
            "original_duration": duration,
        },
        db_path=db_path,
    )
    db.log_stage(item_id, "download", "success", message=f"Downloaded {final_path.name} ({duration:.1f}s)", duration_s=duration_s, db_path=db_path)
    rate_limit.record_success(source, db_path=db_path)
    return True, f"Downloaded successfully ({duration:.1f}s)"


def run_download_pipeline(
    cfg: dict[str, Any] | None = None,
    max_items: int | None = None,
    db_path: Any = None,
) -> list[dict[str, Any]]:
    """Pull top-scoring discovered items up to buffer capacity and process downloads."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    # Resume check for items already marked 'downloaded': ensure file still exists and passes ffprobe
    existing_downloaded = db.get_items_by_stage("downloaded", db_path=db_path)
    for item in existing_downloaded:
        d_path = item.get("download_path")
        if not d_path or not verify_video_file(d_path)[0]:
            log.warning("Item %s marked downloaded but file missing or invalid. Re-queuing to discovered.", item["id"])
            db.transition_stage(item["id"], "discovered", db_path=db_path)

    buffer_cfg = cfg.get("buffer", {})
    target_buffer = int(buffer_cfg.get("target_ready_items", 6))
    current_ready = db.get_ready_buffer_count(db_path=db_path)

    needed = max(0, target_buffer - current_ready)
    if max_items is not None:
        needed = min(needed, max_items)

    if needed <= 0:
        log.info("Buffer target satisfied (%d ready items). Skipping download pull.", current_ready)
        return []

    # Pull top discovered items ordered by discovery_score DESC
    candidates = db.get_items_by_stage("discovered", limit=needed, order_by_score=True, db_path=db_path)
    if not candidates:
        log.info("No candidates in 'discovered' stage waiting for download.")
        return []

    downloaded: list[dict[str, Any]] = []
    for item in candidates:
        ok, msg = download_single_item(item, cfg=cfg, db_path=db_path)
        log.info("Download for %s (%s): %s", item["id"], item["source"], msg)
        if ok:
            updated = db.get_item(item["id"], db_path=db_path)
            if updated:
                downloaded.append(updated)

    return downloaded
