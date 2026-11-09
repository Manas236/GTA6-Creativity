"""Stage 7: ASS subtitle generation and ffmpeg 9:16 vertical video rendering for Variants A and B."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import faster_whisper

from . import config, db, hooks, voiceover
from .download import verify_video_file

log = logging.getLogger(__name__)

_WHISPER_INSTANCE: faster_whisper.WhisperModel | None = None


def get_whisper_model(model_name: str = "base.en") -> faster_whisper.WhisperModel:
    """Return shared WhisperModel singleton on CPU."""
    global _WHISPER_INSTANCE
    if _WHISPER_INSTANCE is None:
        log.info("Loading faster-whisper model '%s' (device=cpu, compute_type=int8)...", model_name)
        _WHISPER_INSTANCE = faster_whisper.WhisperModel(model_name, device="cpu", compute_type="int8")
        log.info("WhisperModel '%s' loaded.", model_name)
    return _WHISPER_INSTANCE


def escape_filter_path(p: Path | str) -> str:
    """Format path for ffmpeg filter arguments, escaping drive colon and normalizing slashes."""
    p_str = str(p).replace(os.sep, "/")
    # If path is relative to current directory, prefer relative path
    try:
        rel = os.path.relpath(p).replace(os.sep, "/")
        if not rel.startswith("../") and not rel.startswith("..\\"):
            p_str = rel
    except Exception:
        pass
    return p_str.replace(":", r"\:")


def format_ass_timestamp(seconds: float) -> str:
    """Format float seconds as ASS timestamp H:MM:SS.cs."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def transcribe_words(audio_or_video_path: Path | str, model_name: str = "base.en") -> list[dict[str, Any]]:
    """Transcribe audio with word-level timestamps using faster-whisper."""
    path_str = str(audio_or_video_path)
    if not Path(path_str).exists():
        return []

    try:
        model = get_whisper_model(model_name=model_name)
        segments, _ = model.transcribe(
            path_str,
            word_timestamps=True,
            language="en",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        words: list[dict[str, Any]] = []
        for segment in segments:
            if not segment.words:
                continue
            for w in segment.words:
                word_clean = w.word.strip()
                if word_clean:
                    words.append({
                        "word": word_clean,
                        "start": float(w.start),
                        "end": float(w.end),
                        "prob": float(w.probability),
                    })
        return words
    except Exception as e:
        log.warning("Whisper transcription failed for %s: %s", path_str, e)
        return []


def build_ass_subtitles(
    hook_text: str,
    video_duration: float,
    words: list[dict[str, Any]] | None = None,
    fonts_dir: Path | str | None = None,
) -> str:
    """Generate ASS subtitle file content with top Hook and bottom word-highlighted captions."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,Montserrat,58,&H0000FFFF,&H00000000,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,8,80,80,180,1
Style: Caption,Montserrat,46,&H00FFFFFF,&H00000000,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,2,70,70,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    # 1. Top Hook Event: spans entire clip
    clean_hook = hook_text.strip().upper()
    # If hook text is long (> 5 words), insert line break for balanced 2-line title
    hook_words = clean_hook.split()
    if len(hook_words) > 5:
        mid = len(hook_words) // 2
        clean_hook = " ".join(hook_words[:mid]) + "\\N" + " ".join(hook_words[mid:])

    t_start_hook = format_ass_timestamp(0.0)
    t_end_hook = format_ass_timestamp(video_duration)
    events.append(f"Dialogue: 0,{t_start_hook},{t_end_hook},Hook,,0,0,0,,{clean_hook}")

    # 2. Bottom Word-by-Word Highlighted Captions (if speech words exist)
    if words:
        # Group words into chunks of 3-5 words
        chunks: list[list[dict[str, Any]]] = []
        current_chunk: list[dict[str, Any]] = []

        for w in words:
            if not current_chunk:
                current_chunk.append(w)
            elif len(current_chunk) >= 4 or (w["start"] - current_chunk[-1]["end"]) > 0.6:
                chunks.append(current_chunk)
                current_chunk = [w]
            else:
                current_chunk.append(w)
        if current_chunk:
            chunks.append(current_chunk)

        # For each chunk, create dialogue entries per active word
        for chunk in chunks:
            raw_words = [cw["word"].upper() for cw in chunk]
            for i, active_w in enumerate(chunk):
                w_start = format_ass_timestamp(active_w["start"])
                # Next word's start or current word's end + small hold
                w_end_time = active_w["end"]
                if i + 1 < len(chunk):
                    w_end_time = min(chunk[i + 1]["start"], active_w["end"] + 0.15)
                w_end = format_ass_timestamp(max(active_w["start"] + 0.1, w_end_time))

                # Build line with active word highlighted
                line_parts: list[str] = []
                for j, word_str in enumerate(raw_words):
                    if j == i:
                        line_parts.append(f"{{\\c&H0000FFFF&}}{word_str}{{\\c&H00FFFFFF&}}")
                    else:
                        line_parts.append(word_str)
                caption_text = " ".join(line_parts)
                events.append(f"Dialogue: 1,{w_start},{w_end},Caption,,0,0,0,,{caption_text}")

    return header + "\n".join(events) + "\n"


def has_audio_stream(video_path: Path | str) -> bool:
    """Check if video contains an audio stream using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "json",
        str(video_path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout)
        return len(data.get("streams", [])) > 0
    except Exception:
        return False


def render_variant_a(
    cut_path: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    target_lufs: float = -14.0,
) -> tuple[bool, float]:
    """Render Variant A: 1080x1920 9:16 layout with hook & captions, -14 LUFS loudness.

    Returns (success: bool, elapsed_seconds: float).
    """
    cut_p = Path(cut_path)
    ass_p = Path(ass_path)
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # Use forward slashes in relative or clean path for ass filter
    ass_filter_arg = f"filename='{escape_filter_path(ass_p)}':fontsdir='assets/fonts'"
    has_audio = has_audio_stream(cut_p)

    if has_audio:
        filter_complex = (
            f"[0:v]scale=360:640:force_original_aspect_ratio=increase,crop=360:640,"
            f"boxblur=8:8,scale=1080:1920,eq=brightness=-0.22[bg];"
            f"[0:v]scale=1080:608:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp];"
            f"[comp]ass={ass_filter_arg}[v];"
            f"[0:a]loudnorm=I={target_lufs}:LRA=11:TP=-1.5[a]"
        )
        audio_map = ["-map", "[a]"]
    else:
        # Fallback if cut video is silent: generate silent stereo audio
        filter_complex = (
            f"[0:v]scale=360:640:force_original_aspect_ratio=increase,crop=360:640,"
            f"boxblur=8:8,scale=1080:1920,eq=brightness=-0.22[bg];"
            f"[0:v]scale=1080:608:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp];"
            f"[comp]ass={ass_filter_arg}[v];"
            f"anullsrc=r=44100:cl=stereo[a]"
        )
        audio_map = ["-map", "[a]"]

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i", str(cut_p),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        *audio_map,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_p),
    ]

    t0 = time.time()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        elapsed = time.time() - t0
        if res.returncode != 0:
            log.error("ffmpeg Variant A render error: %s", res.stderr[-500:])
            return False, elapsed
        return True, elapsed
    except Exception as e:
        log.error("ffmpeg Variant A process error: %s", e)
        return False, time.time() - t0


def render_variant_b(
    cut_path: Path | str,
    voiceover_wav_path: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    target_lufs: float = -14.0,
    ducking_attenuation_db: float = -14.0,
) -> tuple[bool, float]:
    """Render Variant B: 1080x1920 9:16 layout with hook, captions, and VO ducking graph.

    Audio ducking ducks original audio only while 3-6s VO line plays, returning to full.
    Returns (success: bool, elapsed_seconds: float).
    """
    cut_p = Path(cut_path)
    vo_p = Path(voiceover_wav_path)
    ass_p = Path(ass_path)
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    ass_filter_arg = f"filename='{escape_filter_path(ass_p)}':fontsdir='assets/fonts'"
    has_audio = has_audio_stream(cut_p)

    # Audio ducking filter graph: asplit VO stream, duck original, mix, loudnorm
    if has_audio:
        audio_prep = "[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a0];"
    else:
        audio_prep = "anullsrc=r=44100:cl=stereo[a0];"

    filter_complex = (
        f"[0:v]scale=360:640:force_original_aspect_ratio=increase,crop=360:640,"
        f"boxblur=8:8,scale=1080:1920,eq=brightness=-0.22[bg];"
        f"[0:v]scale=1080:608:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[comp];"
        f"[comp]ass={ass_filter_arg}[v];"
        f"{audio_prep}"
        f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,adelay=500|500[vo_del];"
        f"[vo_del]asplit=2[vo_side][vo_mix];"
        f"[a0][vo_side]sidechaincompress=threshold=0.08:ratio=10:attack=40:release=300[ducked];"
        f"[ducked][vo_mix]amix=inputs=2:duration=first:dropout_transition=2[a_mix];"
        f"[a_mix]loudnorm=I={target_lufs}:LRA=11:TP=-1.5[a]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-nostats",
        "-i", str(cut_p),
        "-i", str(vo_p),
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "[a]",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_p),
    ]

    t0 = time.time()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        elapsed = time.time() - t0
        if res.returncode != 0:
            log.error("ffmpeg Variant B render error: %s", res.stderr[-500:])
            return False, elapsed
        return True, elapsed
    except Exception as e:
        log.error("ffmpeg Variant B process error: %s", e)
        return False, time.time() - t0


def run_render_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str, dict[str, float]]:
    """Execute complete render stage for an item in 'cut' stage.

    Generates Variant A and Variant B, validates both, purges download, and updates DB.
    Returns (success: bool, message: str, timing_stats: dict).
    """
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found", {}

    cut_path_str = item.get("cut_path")
    if not cut_path_str or not Path(cut_path_str).exists():
        db.mark_failed(item_id, "render", "Cut file not found on disk", db_path=db_path)
        return False, "Cut file not found", {}

    cut_path = Path(cut_path_str)
    is_valid, cut_dur = verify_video_file(cut_path)
    if not is_valid:
        db.mark_failed(item_id, "render", "Cut file invalid via ffprobe", db_path=db_path)
        return False, "Cut file invalid", {}

    # Ensure hooks generated
    hook_text = item.get("hook_text")
    if not hook_text:
        log.info("Generating hook for item %s...", item_id)
        ok, msg = hooks.run_hooks_for_item(item_id, cfg=cfg, db_path=db_path)
        if not ok:
            db.mark_failed(item_id, "render", f"Hook generation failed: {msg}", db_path=db_path)
            return False, f"Hook generation failed: {msg}", {}
        item = db.get_item(item_id, db_path=db_path)
        hook_text = item.get("hook_text") or "IS THIS REALLY GTA 6?"

    # Ensure voiceover generated
    vo_path_str = item.get("voiceover_audio_path")
    if not vo_path_str or not Path(vo_path_str).exists():
        log.info("Generating voiceover for item %s...", item_id)
        ok, msg = voiceover.run_voiceover_for_item(item_id, cfg=cfg, db_path=db_path)
        if not ok:
            db.mark_failed(item_id, "render", f"Voiceover synthesis failed: {msg}", db_path=db_path)
            return False, f"Voiceover synthesis failed: {msg}", {}
        item = db.get_item(item_id, db_path=db_path)
        vo_path_str = item.get("voiceover_audio_path")

    vo_path = Path(vo_path_str) if vo_path_str else None

    # Transcribe speech in cut clip
    whisper_model = cfg.get("render", {}).get("whisper_model", "base.en")
    log.info("Transcribing cut clip speech for item %s...", item_id)
    speech_words = transcribe_words(cut_path, model_name=whisper_model)
    log.info("Transcribed %d speech words from cut clip.", len(speech_words))

    # Transcribe VO line for Variant B
    vo_words: list[dict[str, Any]] = []
    if vo_path and vo_path.exists():
        raw_vo_words = transcribe_words(vo_path, model_name=whisper_model)
        # Apply 0.5s delay to VO words to align with adelay=500|500
        for w in raw_vo_words:
            vo_words.append({
                "word": w["word"],
                "start": round(w["start"] + 0.5, 2),
                "end": round(w["end"] + 0.5, 2),
                "prob": w["prob"],
            })

    # Variant B captions = VO words followed by original speech that occurs after VO ends
    vo_end_time = vo_words[-1]["end"] if vo_words else 0.0
    subsequent_speech = [w for w in speech_words if w["start"] >= (vo_end_time + 0.2)]
    variant_b_words = vo_words + subsequent_speech

    # Build ASS files
    subtitles_dir = config.media_dir("subtitles")
    ass_a_path = subtitles_dir / f"{item_id}_A.ass"
    ass_b_path = subtitles_dir / f"{item_id}_B.ass"

    ass_a_content = build_ass_subtitles(hook_text, cut_dur, speech_words)
    ass_b_content = build_ass_subtitles(hook_text, cut_dur, variant_b_words)

    ass_a_path.write_text(ass_a_content, encoding="utf-8")
    ass_b_path.write_text(ass_b_content, encoding="utf-8")

    # Render destinations
    renders_dir = config.media_dir("renders")
    render_a_path = renders_dir / f"{item_id}_A.mp4"
    render_b_path = renders_dir / f"{item_id}_B.mp4"

    target_lufs = float(cfg.get("render", {}).get("target_lufs", -14.0))
    ducking_db = float(cfg.get("render", {}).get("ducking_attenuation_db", -14.0))

    # Render Variant A
    log.info("Rendering Variant A for %s...", item_id)
    ok_a, elapsed_a = render_variant_a(cut_path, ass_a_path, render_a_path, target_lufs=target_lufs)
    if not ok_a:
        db.mark_failed(item_id, "render", "Variant A render failed", db_path=db_path)
        return False, "Variant A render failed", {}

    # Render Variant B
    log.info("Rendering Variant B for %s...", item_id)
    ok_b, elapsed_b = render_variant_b(
        cut_path,
        vo_path,
        ass_b_path,
        render_b_path,
        target_lufs=target_lufs,
        ducking_attenuation_db=ducking_db,
    )
    if not ok_b:
        db.mark_failed(item_id, "render", "Variant B render failed", db_path=db_path)
        return False, "Variant B render failed", {}

    # Verify both renders with ffprobe
    val_a, dur_a = verify_video_file(render_a_path)
    val_b, dur_b = verify_video_file(render_b_path)
    if not (val_a and val_b):
        db.mark_failed(item_id, "render", f"Render verification failed: A_valid={val_a}, B_valid={val_b}", db_path=db_path)
        return False, "Render ffprobe validation failed", {}

    # Generate Creator Credit Social Caption
    author = item.get("author_name") or "Creator"
    caption_text = f"{hook_text}\n\nCredit: {author}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames"

    # Purge raw source download immediately per project retention rule
    download_path_str = item.get("download_path")
    if download_path_str:
        Path(download_path_str).unlink(missing_ok=True)
        log.info("Purged raw source download: %s", download_path_str)

    # Transition item to 'rendered' stage
    db.transition_stage(
        item_id,
        "rendered",
        extra_fields={
            "render_a_path": str(render_a_path),
            "render_b_path": str(render_b_path),
            "caption_text": caption_text,
        },
        db_path=db_path,
    )
    db.log_stage(
        item_id,
        "render",
        "success",
        message=f"Renders completed: A ({elapsed_a:.1f}s, {dur_a:.1f}s), B ({elapsed_b:.1f}s, {dur_b:.1f}s)",
        db_path=db_path,
    )

    timing_stats = {
        "variant_a_render_s": round(elapsed_a, 2),
        "variant_b_render_s": round(elapsed_b, 2),
        "total_render_s": round(elapsed_a + elapsed_b, 2),
        "duration_a_s": round(dur_a, 2),
        "duration_b_s": round(dur_b, 2),
    }

    return True, f"Renders complete for {item_id}", timing_stats
