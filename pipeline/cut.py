"""Stage 4: Audio loudness and visual scene-cut window selection with cut-level deduplication.

The END of a cut must follow a completed sentence (pipeline.endings): each candidate
window is transcribed with faster-whisper (plus a tail, so the gap after the last word is
known), and a window whose allowed end range has no sentence break is skipped for the
next-best scored window. No window with a clean ending -> the item fails with
``no_clean_ending``. Windows with no speech keep the loudness-trough end.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from . import binding, config, db, dedupe, endings, sentence_windows, transcribe
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


@dataclass
class CutWindow:
    start: float
    end: float           # proposed end (start of the window + target duration)
    score: float


@dataclass
class CutScan:
    """Everything window scoring learned about a source, probed once."""
    total_duration: float
    loudness: list[tuple[float, float]]
    scene_cuts: list[float]
    windows: list[CutWindow]          # best score first
    min_dur: float
    max_dur: float
    target_dur: float


def _cut_bounds(cfg: dict[str, Any]) -> tuple[float, float, float]:
    cut_cfg = cfg.get("cut", {}) or {}
    min_dur = float(cut_cfg.get("min_duration", cut_cfg.get("min_duration_seconds", 18.0)))
    max_dur = float(cut_cfg.get("max_duration", cut_cfg.get("max_duration_seconds", 22.0)))
    target_dur = float(cut_cfg.get("target_duration", cut_cfg.get("target_duration_seconds", 20.0)))
    return min_dur, max_dur, target_dur


def scan_cut_windows(video_path: Path | str, cfg: dict[str, Any] | None = None) -> CutScan | None:
    """Score every window of the source (audio energy + scene density), best first.

    Returns None if the source is too short, invalid, or fails the static-source floor.
    """
    cfg = cfg or config.get_config()
    cut_cfg = cfg.get("cut", {})
    min_dur, max_dur, target_dur = _cut_bounds(cfg)
    static_floor = float(cut_cfg.get("min_scene_change_density", 0.05))

    audio_weight = float(cut_cfg.get("weights", {}).get("audio_energy", 0.75))
    scene_weight = float(cut_cfg.get("weights", {}).get("visual_interest", 0.25))

    is_valid, total_duration = verify_video_file(video_path)
    if not is_valid or total_duration < min_dur:
        log.warning("Video too short or invalid for cut: %s (duration %.1fs)", video_path, total_duration)
        return None

    # For videos shorter than or equal to target_dur, take full video
    if total_duration <= target_dur:
        loud = extract_audio_loudness_profile(video_path)
        return CutScan(total_duration, loud, [], [CutWindow(0.0, total_duration, 1.0)],
                       min_dur, max_dur, target_dur)

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

    windows: list[CutWindow] = []
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

        windows.append(CutWindow(round(snapped_start, 2), round(t_end, 2), score))

    # Best score first; a stable sort keeps the earlier window on a tie. Snapping starts to
    # scene cuts can give several windows the same start: keep only the best of each, so the
    # next-best window really is a different one.
    windows.sort(key=lambda w: w.score, reverse=True)
    seen_starts: set[float] = set()
    windows = [w for w in windows if not (w.start in seen_starts or seen_starts.add(w.start))]
    if not windows:
        windows = [CutWindow(0.0, min(total_duration, target_dur), 0.5)]
    return CutScan(total_duration, loudness_profile, scene_cuts, windows, min_dur, max_dur, target_dur)


def select_best_cut_window(
    video_path: Path | str,
    cfg: dict[str, Any] | None = None,
) -> tuple[float, float, float] | None:
    """The best-scoring window [start_s, end_s, score] with a loudness-trough end.

    Kept for callers that only need the audio/scene choice; the cut stage itself uses
    `choose_cut`, which also demands a speech-safe end.
    """
    scan = scan_cut_windows(video_path, cfg)
    if scan is None:
        return None
    best = scan.windows[0]
    snapped_end = snap_end_to_pause(
        best.start,
        best.end,
        loudness=scan.loudness,
        min_dur=scan.min_dur,
        max_dur=scan.max_dur,
        hard_max_end=scan.total_duration,
    )
    return best.start, round(snapped_end, 2), best.score


# --------------------------------------------------------------------------------------
# Speech-safe end per window
# --------------------------------------------------------------------------------------

# (video_path, span_start_s, span_end_s) -> whisper words with ABSOLUTE source times.
Transcriber = Callable[[Path, float, float], list[dict[str, Any]]]
# (video_path, span_start_s, span_end_s) -> the audio level envelope on the SOURCE timeline.
EnvelopeFn = Callable[[Path, float, float], "endings.Envelope | None"]


def audio_envelope(video_path: Path | str, start_s: float, end_s: float,
                   hop_s: float = 0.01, win_s: float = 0.03) -> endings.Envelope | None:
    """dB level every `hop_s` (RMS over `win_s`) of [start_s, end_s] of the file's audio, with
    times on the file's timeline. None if the audio cannot be read."""
    import numpy as np

    sr = 16000
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{max(0.0, start_s):.3f}",
           "-t", f"{max(0.1, end_s - start_s):.3f}", "-i", str(video_path), "-vn", "-ac", "1",
           "-ar", str(sr), "-f", "s16le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    except Exception as e:  # noqa: BLE001
        log.warning("audio envelope failed for %s: %s", video_path, e)
        return None
    if len(raw) < sr // 5:
        return None
    x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    win = max(1, int(sr * win_s))
    power = np.convolve(x * x, np.ones(win, dtype=np.float32) / win, mode="same")
    hop = max(1, int(sr * hop_s))
    db_vals = 10.0 * np.log10(power[::hop] + 1e-12)
    return endings.Envelope(max(0.0, start_s), hop / sr, [round(float(v), 2) for v in db_vals])


def whisper_model_name(cfg: dict[str, Any] | None = None) -> str:
    return transcribe.model_name(cfg)


def transcribe_source_span(
    video_path: Path | str, start_s: float, end_s: float, model_name: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """faster-whisper words (configured model + GTA glossary, as everywhere) for
    [start_s, end_s] of the source, on the SOURCE timeline. Raises if whisper itself fails:
    an end that cannot be checked must not be shipped."""
    from . import render  # lazy: render imports this module

    with tempfile.TemporaryDirectory(prefix="gta6_cut_") as tmp:
        wav = Path(tmp) / "span.wav"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-ss", f"{max(0.0, start_s):.3f}", "-t", f"{max(0.1, end_s - start_s):.3f}",
            "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=120)
        if res.returncode != 0 or not wav.exists():
            raise RuntimeError(f"could not extract audio {start_s:.2f}-{end_s:.2f}s for transcription")
        words = render.transcribe_words(wav, model_name=model_name, raise_errors=True, cfg=cfg)
    return [{**w, "start": round(float(w["start"]) + start_s, 3), "end": round(float(w["end"]) + start_s, 3)}
            for w in words]


@dataclass
class EndChoice:
    start: float
    end: float
    kind: str                                           # "sentence" | "no_speech"
    words: list[dict[str, Any]] = field(default_factory=list)   # CUT timeline (0 = start)


def clean_end_for_window(
    win: CutWindow,
    scan: CutScan,
    words_abs: Sequence[dict[str, Any]],
    audio_end: float,
    es: dict[str, Any],
    env: endings.Envelope | None = None,
) -> EndChoice | None:
    """Pick this window's end, or None when speech runs through its whole end range.

    `words_abs` cover [win.start, audio_end] on the source timeline. With speech, the end
    is the sentence break nearest the window's proposed end (ties: the earlier one), inside
    [start + min_dur, start + max_dur]. Without speech, the loudness-trough snap is kept.
    """
    lo = win.start + scan.min_dur
    hi = min(win.start + scan.max_dur, scan.total_duration)
    if hi < lo:                                          # a source shorter than min_dur
        lo = hi
    in_window = [w for w in words_abs if float(w["end"]) > win.start and float(w["start"]) < audio_end]

    if not in_window:
        end = snap_end_to_pause(
            win.start, win.end, loudness=scan.loudness, min_dur=scan.min_dur, max_dur=scan.max_dur,
            hard_max_end=scan.total_duration,
        )
        return EndChoice(win.start, round(end, 2), "no_speech", [])

    spans = endings.safe_spans(in_window, lo, hi, audio_end, es, env=env)
    target = min(max(win.end, lo), hi)
    end = endings.choose_end(spans, target, es)
    if end is None:
        return None
    span = endings.span_at(end, spans)
    end = endings.snap_inside(end, span, 0.01) if span else round(end, 2)   # DB keeps 2 decimals
    end = round(end, 2)
    cut_words = [
        {**w, "start": round(max(0.0, float(w["start"]) - win.start), 3),
         "end": round(float(w["end"]) - win.start, 3)}
        # Words that FINISH before the end. After a measured pause (punct_audio) whisper often
        # starts the next word inside the pause; that word is not in the cut.
        for w in in_window if float(w["end"]) <= end + 1e-6
    ]
    return EndChoice(win.start, end, "sentence", cut_words)


def transcript_payload(choice: EndChoice, key: str, selection: dict[str, Any] | None = None) -> str:
    """The items.transcript_words value for a cut: keyed on the window and on
    transcribe.cache_key() (model + glossary), so hooks and render reuse it and a changed
    window, model or glossary can never pass for it."""
    return json.dumps({
        "cut_start": choice.start,
        "cut_end": choice.end,
        "model": key.split("|", 1)[0],
        "key": key,
        "words": choice.words,
        "ending": choice.kind,          # the cut stage checked this end: render may trust it
        "ending_rule": endings.RULE_VERSION,
        "source": "cut",
        "selection": selection or {},     # how the window was chosen (candidates, timing, ...)
    })


# --------------------------------------------------------------------------------------
# Stage
# --------------------------------------------------------------------------------------

def _extract_cut(download_path: Path, start_s: float, end_s: float, cut_path: Path) -> None:
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
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)


def source_transcript_key(item: dict[str, Any], path: Path, regions: Sequence[tuple[float, float]], tkey: str) -> str:
    """The whole-source transcript is valid for this source file (identity + size), this
    transcription key (model + glossary) and these regions only."""
    size = path.stat().st_size if path.exists() else 0
    blob = f"{item.get('source')}:{item.get('source_id')}:{size}:{tkey}:{list(regions)}"
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def load_source_transcript(
    item: dict[str, Any], path: Path, regions: Sequence[tuple[float, float]], tkey: str,
    transcriber: "Transcriber", db_path: Any = None,
) -> tuple[list[dict[str, Any]], float, bool]:
    """(words on the source timeline, wall seconds the transcription took, came from cache).

    Cached in items.source_transcript, so a re-cut (dedupe, a preset re-render, a config
    change elsewhere) never transcribes the same source twice."""
    key = source_transcript_key(item, path, regions, tkey)
    raw = item.get("source_transcript")
    if raw:
        try:
            data = json.loads(raw)
            if data.get("key") == key:
                return [dict(w) for w in data.get("words") or []], float(data.get("wall_s") or 0.0), True
        except Exception:  # noqa: BLE001 - a corrupt cache is simply rebuilt
            pass
    t0 = time.perf_counter()
    words: list[dict[str, Any]] = []
    for a, b in regions:
        words += transcriber(path, a, b)
    wall = time.perf_counter() - t0
    covered = sum(b - a for a, b in regions)
    log.info("Transcribed %.0f s of %s in %.1f s (%s, %d region(s), %d words)",
             covered, path.name, wall, tkey, len(regions), len(words))
    with db.db(db_path) as conn:
        conn.execute("UPDATE items SET source_transcript = ?, updated_at = ? WHERE id = ?", (
            json.dumps({"key": key, "transcribe_key": tkey, "regions": [list(r) for r in regions],
                        "wall_s": round(wall, 1), "words": words}),
            db.now(), item["id"]))
    return words, wall, False


def _outcome(item_id: str, status: str, message: str, db_path: Any) -> None:
    """Every cut attempt leaves exactly one stage_log row (success / failure / rejected), which is
    what the no_clean_ending rate in `runner status` and the alerts counts."""
    db.log_stage(item_id, "cut", status, message=message[:1000], db_path=db_path)


def run_cut_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    transcriber: Transcriber | None = None,
    envelope: EnvelopeFn | None = None,
) -> tuple[bool, str]:
    """Cut stage for a downloaded item: sentence-aligned window selection, then cut-level dedupe.

    The source is transcribed once (pipeline.sentence_windows); every clean sentence end that
    admits a min..max window gives a candidate, scored with the audio/scene formula plus a soft
    sentence-start bonus. Candidates are cut best first; one that duplicates an earlier item's
    cut is skipped (as is any candidate overlapping it by half or more). A source with no speech
    keeps the old loudness-trough windows. No candidate at all -> failed, `no_clean_ending`.
    """
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found"

    download_path_str = item.get("download_path")
    if not download_path_str or not Path(download_path_str).exists():
        db.mark_failed(item_id, "cut", "Download path missing on disk", db_path=db_path)
        _outcome(item_id, "failure", "Download path missing on disk", db_path)
        return False, "Download file not found"

    download_path = Path(download_path_str)
    consecutive_n = int(cfg.get("dedupe", {}).get("consecutive_frames", 3))
    hamming_thresh = int(cfg.get("dedupe", {}).get("hamming_threshold", 8))
    es = endings.ending_settings(cfg)
    sw = sentence_windows.settings(cfg)
    tkey = transcribe.cache_key(cfg)
    if transcriber is None:
        def transcriber(path: Path, a: float, b: float) -> list[dict[str, Any]]:
            return transcribe_source_span(path, a, b, cfg=cfg)
    envelope = envelope or audio_envelope

    scan = scan_cut_windows(download_path, cfg=cfg)
    if not scan:
        # Static source or duration floor failed
        db.mark_rejected(item_id, "rejected: static_source (low scene density)", db_path=db_path)
        _outcome(item_id, "rejected", "static_source (low scene density) or too short", db_path)
        return False, "Rejected: static_source"

    tail = float(es["tail_s"])
    max_windows = max(1, int(es["max_windows"]))
    spans = sentence_windows.transcribe_regions(scan.total_duration, scan.windows, scan.min_dur,
                                                scan.max_dur, tail, sw)
    try:
        words_all, wall, cached = load_source_transcript(item, download_path, spans, tkey, transcriber, db_path)
    except Exception as e:
        reason = f"Whisper transcription failed at cut: {type(e).__name__}: {e}"
        log.error("%s (item %s)", reason, item_id)
        db.mark_failed(item_id, "cut", reason, db_path=db_path)
        _outcome(item_id, "failure", reason, db_path)
        return False, reason

    selection: dict[str, Any] = {
        "transcribed": [list(s) for s in spans], "transcribe_s": round(wall, 1), "transcript_cached": cached,
        "source_s": round(scan.total_duration, 2),
    }
    choices: list[tuple[EndChoice, dict[str, Any]]] = []
    if not words_all:
        # Nobody speaks: the old audio/scene windows with a loudness-trough end.
        for rank, win in enumerate(scan.windows[:max_windows], start=1):
            ch = clean_end_for_window(win, scan, [], min(scan.total_duration, win.start + scan.max_dur), es)
            if ch:
                choices.append((ch, {"rank": rank, "score": round(win.score, 4), "mode": "no_speech"}))
        selection.update(clean_ends=0, candidates=len(choices))
    else:
        regions = []
        for a, b in spans:
            rw = [w for w in words_all if a - 1e-6 <= float(w["start"]) < b]
            regions.append(sentence_windows.Region(a, b, rw, envelope(download_path, a, b) if rw else None))
        scorer = sentence_windows.Scorer(scan.total_duration, scan.loudness, scan.scene_cuts, cfg)
        cands, n_ends = sentence_windows.candidates(regions, scorer, scan.min_dur, scan.max_dur, es, sw)
        selection.update(clean_ends=n_ends, candidates=len(cands))
        for rank, c in enumerate(cands, start=1):
            cut_words = [
                {**w, "start": round(max(0.0, float(w["start"]) - c.start), 3),
                 "end": round(float(w["end"]) - c.start, 3)}
                # words that finish inside the window (at a measured pause whisper may start the
                # next word early; it is not in the cut)
                for w in words_all if float(w["end"]) > c.start and float(w["end"]) <= c.end + 1e-6
            ]
            choices.append((EndChoice(c.start, round(c.end, 2), "sentence", cut_words),
                            {"rank": rank, "score": c.score, "at_sentence_start": c.at_sentence_start,
                             "end_rule": c.reason, "mode": "sentence"}))
        if not cands:
            reason = (f"no_clean_ending: {n_ends} clean sentence end(s) in {sum(b - a for a, b in spans):.0f} s "
                      f"transcribed, none with a {scan.min_dur:.0f}-{scan.max_dur:.0f} s window "
                      f"({len(words_all)} words, transcription {wall:.1f} s)")
            db.mark_failed(item_id, "cut", reason, db_path=db_path)
            _outcome(item_id, "failure", reason, db_path)
            _alert_no_clean_ending_rate(item_id, cfg, db_path)
            return False, reason

    cuts_dir = config.media_dir("cuts")
    cut_path = cuts_dir / f"{item_id}_cut.mp4"
    duplicates: list[tuple[str, float, float]] = []      # (prior item, start, end)
    tried = 0
    for choice, info in choices:
        if tried >= max_windows:
            break
        # A candidate overlapping a duplicate window by half or more shows the same footage.
        if any(min(choice.end, e) - max(choice.start, s) >= 0.5 * (choice.end - choice.start)
               for _, s, e in duplicates):
            continue
        tried += 1
        try:
            _extract_cut(download_path, choice.start, choice.end, cut_path)
        except Exception as e:
            db.mark_failed(item_id, "cut", f"ffmpeg cut extraction failed: {e}", db_path=db_path)
            _outcome(item_id, "failure", f"ffmpeg cut extraction failed: {e}", db_path)
            return False, f"ffmpeg cut extraction failed: {e}"

        is_valid, actual_dur = verify_video_file(cut_path)
        if not is_valid:
            cut_path.unlink(missing_ok=True)
            db.mark_failed(item_id, "cut", "Cut file failed ffprobe validation", db_path=db_path)
            _outcome(item_id, "failure", "Cut file failed ffprobe validation", db_path)
            return False, "Cut validation failed"

        # Cut-level deduplication: extract 1fps hashes of the cut
        cut_hashes = dedupe.extract_1fps_hashes(cut_path, crop=cfg.get("dedupe", {}).get("crop_black_bars", True))
        cand_hashes_only = [h for _, h in cut_hashes]
        stored_cuts = db.get_stored_fingerprints_by_item("cut", exclude_item_id=item_id, db_path=db_path)
        dup_of = next(
            (prior_id for prior_id, prior_tuples in stored_cuts.items()
             if dedupe.check_consecutive_match(
                 cand_hashes_only, [h for _, h in prior_tuples],
                 consecutive_n=consecutive_n, hamming_threshold=hamming_thresh)),
            None,
        )
        if dup_of:
            cut_path.unlink(missing_ok=True)
            duplicates.append((dup_of, choice.start, choice.end))
            log.info("Cut candidate #%d [%.2f-%.2f s] duplicates %s; trying the next",
                     info["rank"], choice.start, choice.end, dup_of)
            continue

        # Replace (never append to) this item's cut fingerprints: a re-cut moves the window.
        db.delete_fingerprints(item_id, "cut", db_path=db_path)
        db.save_fingerprints(item_id, "cut", cut_hashes, db_path=db_path)
        selection.update(info, start=choice.start, end=choice.end)
        # A moved window invalidates everything written for the old one: the hook, voiceover,
        # keywords and renders described a different part of the source (the wrong-caption bug).
        stale = binding.reset_for_new_segment(item, choice.start, choice.end)
        if stale:
            log.info("Cut window for %s moved to %.2f-%.2f s: clearing the hook/voiceover/renders of "
                     "the old window (%s-%s s)", item_id, choice.start, choice.end,
                     item.get("cut_start"), item.get("cut_end"))
        db.transition_stage(
            item_id,
            "cut",
            extra_fields={
                **stale,
                "cut_start": choice.start,
                "cut_end": choice.end,
                "cut_duration": actual_dur,
                "cut_path": str(cut_path),
                # Always rewritten, so a stale transcript of an older window is gone too.
                "transcript_words": transcript_payload(choice, tkey, selection),
            },
            db_path=db_path,
        )
        msg = (f"Cut candidate #{info['rank']} of {selection['candidates']} "
               f"[{choice.start:.2f}s - {choice.end:.2f}s] extracted ({actual_dur:.1f}s, end: {choice.kind}"
               f"{', starts at a sentence' if info.get('at_sentence_start') else ''}; "
               f"{selection['clean_ends']} clean sentence ends; transcription {wall:.1f}s"
               f"{' cached' if cached else ''})")
        _outcome(item_id, "success", msg, db_path)
        return True, msg

    prior = duplicates[0][0] if duplicates else "?"
    if duplicates:
        db.mark_rejected(
            item_id,
            f"duplicate_cut: {consecutive_n} matching consecutive frames with {prior}",
            is_duplicate=True,
            db_path=db_path,
        )
        _outcome(item_id, "rejected", f"duplicate_cut of {prior} ({len(duplicates)} candidate(s))", db_path)
        return False, f"Rejected: duplicate_cut of {prior}"
    reason = f"no_clean_ending: no usable window ({selection.get('candidates', 0)} candidates)"
    db.mark_failed(item_id, "cut", reason, db_path=db_path)
    _outcome(item_id, "failure", reason, db_path)
    _alert_no_clean_ending_rate(item_id, cfg, db_path)
    return False, reason


def _alert_no_clean_ending_rate(item_id: str, cfg: dict[str, Any], db_path: Any) -> None:
    """A failure alert, but only once the 24 h no_clean_ending rate is past its threshold
    (telegram_bot.send_alert adds the rate line itself)."""
    from . import telegram_bot   # lazy: keep the cut stage importable without the bot

    if telegram_bot.no_clean_ending_line(cfg, db_path):
        telegram_bot.send_alert(f"Cut stage: {item_id} failed with no_clean_ending.",
                                key="cut_no_clean_ending_rate", cfg=cfg, db_path=db_path,
                                repeat_gap_s=12 * 3600)


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
