"""Stage 7: ASS subtitle generation and ffmpeg 9:16 vertical rendering for Variants A and B.

Variant A: cut (end snapped to a pause) + hook + word-highlighted captions, fade-out.
Variant B: freeze-frame format. The cut's FIRST frame is held as a still for
           (VO duration + freeze_pad_s) while ONLY the voiceover plays; then the cut plays
           from its start with its original audio at full level. No ducking anywhere.

On top of that sits the **style layer** (pipeline.style / plan / edits / overlay /
audio_mix): config-toggled edit effects grouped into presets P0-P3. A render is driven by
exactly one `plan.StylePlan`; render.py never re-derives a timestamp, which is what keeps
captions, punch-ins, SFX, emojis and the music bed in sync after the Variant B freeze and
after A3 dead-air removal.

Every output is ffprobe-verified: audio/video stream durations must match within
render.av_duration_tolerance_s and fps / sample rate / channels / resolution must match config.
"""
from __future__ import annotations

import json
import logging
import os
import math
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from . import audio_mix, config, db, edits, hooks, keywords as kwmod, overlay, plan as planning, posting, style, voiceover
from .cut import extract_audio_loudness_profile, snap_end_to_pause
from . import endings
from .download import verify_video_file
from .rendercore import (  # re-exported for callers and tests
    RENDER_DEFAULTS,
    _abs_project_path,
    _clean_caption_word,
    _clean_text,
    _font_metrics,
    _rate,
    cap_height_px,
    chunk_caption_words,
    em_px,
    escape_filter_path,
    has_audio_stream,
    hook_line_centers,
    hook_line_pitch,
    layout_hook,
    media_duration,
    probe_streams,
    render_settings,
    text_width_px,
)

log = logging.getLogger(__name__)

_WHISPER_INSTANCE: Any = None

__all__ = [
    "RENDER_DEFAULTS", "RenderVerificationError", "render_settings", "escape_filter_path",
    "format_ass_timestamp", "transcribe_words", "cached_cut_words", "store_cut_words",
    "cut_speech_words", "em_px", "cap_height_px", "text_width_px",
    "layout_hook", "hook_line_centers", "chunk_caption_words", "build_ass_subtitles",
    "build_variant_b_words", "estimate_words_from_script", "has_audio_stream", "probe_streams",
    "verify_render", "media_duration", "freeze_duration", "render_variant_a", "render_variant_b",
    "snap_render_end", "render_item_variants", "run_render_item", "get_whisper_model",
]


class RenderVerificationError(RuntimeError):
    """Raised / reported when an output fails the ffprobe stream checks."""


def whisper_download_root() -> str | None:
    """None (huggingface_hub's own choice) unless HF_HOME points at a drive or folder root
    that does not exist - then the default ~/.cache/huggingface/hub, where the model was
    cached before. Without this, a stale HF_HOME fails every cut and every hooks run."""
    hf_home = os.environ.get("HF_HOME")
    if not hf_home:
        return None
    anchor = Path(hf_home).anchor or hf_home
    if Path(anchor).exists():
        return None
    fallback = Path.home() / ".cache" / "huggingface" / "hub"
    log.warning("HF_HOME=%s is on a drive that does not exist; loading whisper from %s", hf_home, fallback)
    return str(fallback)


def get_whisper_model(model_name: str = "base.en") -> Any:
    """Return shared WhisperModel singleton on CPU (faster_whisper imported lazily)."""
    global _WHISPER_INSTANCE
    if _WHISPER_INSTANCE is None:
        import faster_whisper

        log.info("Loading faster-whisper model '%s' (device=cpu, compute_type=int8)...", model_name)
        _WHISPER_INSTANCE = faster_whisper.WhisperModel(
            model_name, device="cpu", compute_type="int8", download_root=whisper_download_root()
        )
        log.info("WhisperModel '%s' loaded.", model_name)
    return _WHISPER_INSTANCE


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


def transcribe_words(
    audio_or_video_path: Path | str, model_name: str = "base.en", raise_errors: bool = False
) -> list[dict[str, Any]]:
    """Transcribe audio with word-level timestamps using faster-whisper.

    By default a failure is logged and [] is returned. raise_errors=True re-raises instead,
    so a caller can tell "whisper broke" from "nobody speaks" (the hooks stage needs that).
    """
    path_str = str(audio_or_video_path)
    if not Path(path_str).exists():
        if raise_errors:
            raise FileNotFoundError(f"cannot transcribe, file not found: {path_str}")
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
        if raise_errors:
            raise
        log.warning("Whisper transcription failed for %s: %s", path_str, e)
        return []


# The hooks stage transcribes the cut once (Gemini needs the transcript) and caches the
# words on the item; render reuses them. The cache is keyed on the cut window and the
# whisper model, so a re-cut (new cut_start/cut_end) silently invalidates it.

def cached_cut_words(item: dict[str, Any] | None, model_name: str) -> list[dict[str, Any]] | None:
    """Cut-timeline words cached on the item, or None when absent/stale/corrupt."""
    raw = (item or {}).get("transcript_words")
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        if data.get("model") != model_name:
            return None
        for k in ("cut_start", "cut_end"):
            if item.get(k) is None or abs(float(data[k]) - float(item[k])) > 1e-6:
                return None
        words = data["words"]
        return [dict(w) for w in words] if isinstance(words, list) else None
    except Exception:
        return None


def store_cut_words(
    item_id: str, item: dict[str, Any], words: Sequence[dict[str, Any]], model_name: str, db_path: Any = None
) -> None:
    """Cache the cut transcript on the item (tagged with the cut window it belongs to)."""
    payload = {
        "cut_start": item.get("cut_start"),
        "cut_end": item.get("cut_end"),
        "model": model_name,
        "words": list(words),
    }
    with db.db(db_path) as conn:
        conn.execute(
            "UPDATE items SET transcript_words = :tw, updated_at = :now WHERE id = :id",
            {"id": item_id, "tw": json.dumps(payload), "now": db.now()},
        )


def cut_end_verified(item: dict[str, Any] | None, model_name: str) -> bool:
    """True when the cut stage checked this cut's end against the audio AFTER the cut
    (it transcribed a tail), so render may treat the cut's own end as a sentence end.

    Only the cut stage writes the `ending` key, and only for the window it chose; a cache
    for another window or another model never counts."""
    if cached_cut_words(item, model_name) is None:
        return False
    try:
        raw = item.get("transcript_words")
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data.get("ending") in ("sentence", "no_speech")
    except Exception:
        return False


def cut_speech_words(item: dict[str, Any], cut_path: Path | str, model_name: str) -> list[dict[str, Any]]:
    """Render-side read: the cached cut words when valid, else a fresh transcription.

    Render never writes the cache; the hooks stage owns it.
    """
    cached = cached_cut_words(item, model_name)
    if cached is not None:
        log.info("Reusing %d cached cut words for %s (no re-transcription).", len(cached), item.get("id"))
        return cached
    return transcribe_words(cut_path, model_name=model_name)


# --------------------------------------------------------------------------------------
# ASS generation
# --------------------------------------------------------------------------------------

def _flag(sp: Any, name: str) -> bool:
    return bool(sp.flags.get(name)) if sp is not None else False


def build_ass_subtitles(
    hook_text: str,
    video_duration: float,
    words: list[dict[str, Any]] | None = None,
    fonts_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    sp: Any = None,
) -> str:
    """Generate ASS content: hook in the band above the video for the whole clip, plus
    1-3 word captions with the active word highlighted.

    `words` must already be on the output timeline (Variant B: see build_variant_b_words).
    Passing a `plan.StylePlan` as `sp` adds the enabled style elements (C1 keyword colour,
    C3 progress bar, C4 credit tag, M1 caption pop, M2 hook pop/shake) and uses its
    re-placed safe-area layout. Without `sp` the output is the plain CP2 layout.
    """
    rs = render_settings(cfg)
    ss = sp.ss if sp is not None else style.style_settings(cfg)
    hk, cap = rs["hook"], rs["caption"]
    font = rs["font_name"]
    W, H = int(rs["width"]), int(rs["height"])
    cx = W / 2.0

    if sp is not None and sp.hook_lines:
        hook_lines, hook_size = list(sp.hook_lines), int(sp.hook_font_size)
    else:
        hook_lines, hook_size = layout_hook(hook_text, rs)

    cap_y = float(sp.layout.caption_center_y) if sp is not None else float(cap["center_y"])
    hook_center = float(sp.layout.hook_center_y) if sp is not None else None
    hook_pitch = float(sp.layout.hook_line_pitch) if sp is not None else None

    styles = [
        f"Style: Hook,{font},{hook_size},{hk['color']},&H00000000,&H00000000,&H80000000,0,0,0,0,"
        f"100,100,0,0,1,{hk['outline']},{hk['shadow']},5,{rs['safe_area']['x_min']},"
        f"{W - rs['safe_area']['x_max']},0,1",
        f"Style: Caption,{font},{cap['font_size']},{cap['color']},&H00000000,&H00000000,&H80000000,0,0,0,0,"
        f"100,100,0,0,1,{cap['outline']},{cap['shadow']},5,{rs['safe_area']['x_min']},"
        f"{W - rs['safe_area']['x_max']},0,1",
    ]
    if _flag(sp, "progress_bar"):
        styles.append(overlay.progress_bar_style(rs, ss))
    if _flag(sp, "credit_tag"):
        styles.append(overlay.credit_style(rs, ss))

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {W}\n"
        f"PlayResY: {H}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []

    # --- hook (whole clip), with the M2 pop + shake when enabled --------------------
    t0 = format_ass_timestamp(0.0)
    t1 = format_ass_timestamp(video_duration)
    centers = hook_line_centers(len(hook_lines), hook_size, rs, center=hook_center, pitch=hook_pitch)
    pop = overlay.hook_pop_tags(ss, cx, hook_center if hook_center is not None else
                                (float(hk["band_top_y"]) + float(hk["band_bottom_y"])) / 2.0) \
        if _flag(sp, "hook_pop") else ""
    for line, y in zip(hook_lines, centers):
        events.append(f"Dialogue: 0,{t0},{t1},Hook,,0,0,0,,{{\\an5\\pos({cx:.0f},{y:.0f}){pop}}}{line}")

    # --- captions -------------------------------------------------------------------
    if words:
        timeline = [w for w in words if float(w["start"]) < video_duration - 0.05]
        chunks = chunk_caption_words(timeline, rs)
        hl = cap["highlight_color"]
        base = cap["color"]
        kw_list = list(sp.keyword_list) if sp is not None else []
        kw_on = _flag(sp, "keyword_color")
        pop_tags = overlay.caption_pop_tags(ss) if _flag(sp, "caption_pop") else ""
        for ci, chunk in enumerate(chunks):
            raw = [_clean_caption_word(cw["word"]) for cw in chunk]
            next_chunk_start = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else video_duration
            for i, active in enumerate(chunk):
                start = float(active["start"])
                if i + 1 < len(chunk):
                    end = float(chunk[i + 1]["start"])
                else:
                    end = min(float(active["end"]) + 0.3, float(next_chunk_start))
                end = min(max(end, start + 0.1), video_duration)
                parts: list[str] = []
                for j, w in enumerate(raw):
                    colour = overlay.word_colour(w, j == i, kw_list, rs, ss, kw_on)
                    parts.append(f"{{\\c{colour}&}}{w}{{\\c{base}&}}" if colour != base else w)
                text = f"{{\\an5\\pos({cx:.0f},{cap_y:.0f})" + (pop_tags if i == 0 else "") + "}" + " ".join(parts)
                events.append(
                    f"Dialogue: 1,{format_ass_timestamp(start)},{format_ass_timestamp(end)},Caption,,0,0,0,,{text}"
                )

    # --- C4 credit tag, C3 progress bar ---------------------------------------------
    if _flag(sp, "credit_tag"):
        events.append(overlay.credit_event(sp.channel_name, video_duration, sp.layout, ss, format_ass_timestamp))
    if _flag(sp, "progress_bar"):
        events.append(overlay.progress_bar_event(video_duration, rs, ss, format_ass_timestamp))

    return header + "\n".join(events) + "\n"


def build_variant_b_words(
    vo_words: list[dict[str, Any]],
    speech_words: list[dict[str, Any]],
    freeze_s: float,
) -> list[dict[str, Any]]:
    """Variant B caption timeline: VO words during the freeze, then the cut's speech
    shifted by the freeze duration."""
    out: list[dict[str, Any]] = []
    for w in vo_words:
        if float(w["start"]) >= freeze_s:
            continue
        out.append({**w, "end": min(float(w["end"]), freeze_s)})
    for w in speech_words:
        out.append({**w, "start": round(float(w["start"]) + freeze_s, 3), "end": round(float(w["end"]) + freeze_s, 3)})
    return out


def estimate_words_from_script(script: str, duration_s: float) -> list[dict[str, Any]]:
    """Fallback VO word timings when whisper is unavailable: spread words by character count."""
    toks = [t for t in (script or "").split() if t.strip()]
    if not toks or duration_s <= 0:
        return []
    total = sum(len(t) + 1 for t in toks)
    t = 0.0
    out = []
    for tok in toks:
        d = duration_s * (len(tok) + 1) / total
        out.append({"word": tok, "start": round(t, 3), "end": round(t + d, 3), "prob": 1.0})
        t += d
    return out


# --------------------------------------------------------------------------------------
# Output verification
# --------------------------------------------------------------------------------------

def verify_render(
    path: Path | str,
    cfg: dict[str, Any] | None = None,
    expected_duration: float | None = None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Fail-closed output check. Returns (ok, probe_info, errors)."""
    rs = render_settings(cfg)
    tol = float(rs["av_duration_tolerance_s"])
    errors: list[str] = []
    try:
        info = probe_streams(path)
    except Exception as e:  # pragma: no cover - ffprobe missing/corrupt
        return False, {}, [f"ffprobe failed: {e}"]
    if "video_duration" not in info:
        errors.append("no video stream")
    if "audio_duration" not in info:
        errors.append("no audio stream")
    if errors:
        return False, info, errors
    vd, ad = info["video_duration"], info["audio_duration"]
    if abs(vd - ad) > tol:
        errors.append(f"audio/video stream durations differ: video={vd:.3f}s audio={ad:.3f}s (tol {tol}s)")
    if abs(info["fps"] - float(rs["fps"])) > 0.01:
        errors.append(f"fps {info['fps']} != {rs['fps']}")
    if info["sample_rate"] != int(rs["audio_sample_rate"]):
        errors.append(f"sample rate {info['sample_rate']} != {rs['audio_sample_rate']}")
    if info["channels"] != int(rs["audio_channels"]):
        errors.append(f"channels {info['channels']} != {rs['audio_channels']}")
    if (info["width"], info["height"]) != (int(rs["width"]), int(rs["height"])):
        errors.append(f"resolution {info['width']}x{info['height']} != {rs['width']}x{rs['height']}")
    if expected_duration is not None and abs(vd - expected_duration) > tol:
        errors.append(f"video duration {vd:.3f}s != expected {expected_duration:.3f}s")
    return not errors, info, errors


def freeze_duration(vo_duration_s: float, cfg: dict[str, Any] | None = None) -> float:
    """Variant B still-frame length: VO duration + pad, rounded UP to a whole frame."""
    rs = render_settings(cfg)
    fps = int(rs["fps"])
    return math.ceil((float(vo_duration_s) + float(rs["freeze_pad_s"])) * fps - 1e-6) / fps


# --------------------------------------------------------------------------------------
# Filtergraph construction
# --------------------------------------------------------------------------------------

def _even(x: float) -> int:
    return max(2, int(round(float(x) / 2.0) * 2))


def _ass_arg(ass_path: Path | str, rs: dict[str, Any]) -> str:
    fonts_dir = _abs_project_path(rs["fonts_dir"])
    return f"filename='{escape_filter_path(ass_path)}':fontsdir='{escape_filter_path(fonts_dir)}'"


def _splice_inputs(chain: str, count: int, first_index: int, kind: str) -> str:
    """Replace the {IDXn} placeholders a fragment emits with real ffmpeg input indices."""
    for i in range(count):
        chain = chain.replace(f"{{IDX{i}}}", f"{first_index + i}:{kind}")
    return chain


def _spans_chain(spans: Sequence[tuple[float, float]], src: str, out: str, audio: bool) -> str:
    """A3: keep only `spans` of a stream by trim/concat (frame- and sample-exact)."""
    n = len(spans)
    if n <= 1:
        return f"[{src}]{'anull' if audio else 'null'}[{out}]"
    sp, tr, setp = ("asplit", "atrim", "asetpts") if audio else ("split", "trim", "setpts")
    labels = [f"{out}_p{i}" for i in range(n)]
    parts = [f"[{src}]{sp}={n}" + "".join(f"[{out}_s{i}]" for i in range(n))]
    for i, (a, b) in enumerate(spans):
        parts.append(f"[{out}_s{i}]{tr}=start={a:.4f}:end={b:.4f},{setp}=PTS-STARTPTS[{labels[i]}]")
    v, a = (0, 1) if audio else (1, 0)
    parts.append("".join(f"[{l}]" for l in labels) + f"concat=n={n}:v={v}:a={a}[{out}]")
    return ";".join(parts)


def _fg_chain(rs: dict[str, Any], sp: Any, src: str, out: str) -> str:
    """Foreground layer. Without F1 this is the CP2 full-width fit (1080x608).

    With F1 the source sits on a supersampled transparent canvas whose zoom is driven by
    the plan: zoom 1.0 is the full-width fit, zoom geo.fill_zoom fills the band with the
    sides cropped. A constant zoom skips zoompan and uses crop+scale instead (cheaper and
    the common case for P0), an animated zoom uses zoompan with `it` as the clock.
    """
    W = int(rs["width"])
    if not _flag(sp, "zoom_fill"):
        fg_h = _even(W * 9 / 16)
        return f"[{src}]scale={W}:{fg_h}:force_original_aspect_ratio=decrease[{out}]"
    geo, fps = sp.geo, int(rs["fps"])
    pre = (
        f"[{src}]scale={geo.canvas_w}:{geo.content_h}:force_original_aspect_ratio=decrease,"
        f"format=yuva420p,pad={geo.canvas_w}:{geo.canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black@0"
    )
    if sp.zoom.is_constant:
        z = max(1.0, sp.zoom.constant_value)
        return f"{pre},crop={_even(geo.canvas_w / z)}:{_even(geo.canvas_h / z)},scale={W}:{geo.fill_h}[{out}]"
    expr = edits.zoom_expr(sp.zoom, var="it")
    return (
        f"{pre},zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={W}x{geo.fill_h}:fps={fps}[{out}]"
    )


def _video_chain(
    rs: dict[str, Any],
    ass_arg: str,
    n_cut_frames: int,
    freeze_frames: int = 0,
    sp: Any = None,
    emoji_chain: str = "",
) -> str:
    """Build the 9:16 composite: blurred cover background + foreground + ASS + overlays."""
    W, H, fps = int(rs["width"]), int(rs["height"]), int(rs["fps"])
    total = n_cut_frames + freeze_frames
    parts = [f"[0:v]fps={fps},setpts=PTS-STARTPTS[vsrc]"]
    cur = "vsrc"
    if _flag(sp, "dead_air") and len(sp.timeline.kept) > 1:
        parts.append(_spans_chain(sp.timeline.kept, cur, "vkept", audio=False))
        cur = "vkept"
    freeze = f",tpad=start={freeze_frames}:start_mode=clone" if freeze_frames > 0 else ""
    parts.append(
        f"[{cur}]tpad=stop=-1:stop_mode=clone:stop_duration=2,trim=end_frame={n_cut_frames},"
        f"setpts=PTS-STARTPTS{freeze},split=2[vs1][vs2]"
    )
    parts.append(
        f"[vs1]scale={W // 3}:{H // 3}:force_original_aspect_ratio=increase,crop={W // 3}:{H // 3},"
        f"boxblur=8:8,scale={W}:{H},eq=brightness=-0.22[bg]"
    )
    parts.append(_fg_chain(rs, sp, "vs2", "fg"))
    parts.append(f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,ass={ass_arg}[vass]")
    last = "vass"
    if emoji_chain:
        parts.append(emoji_chain)
        last = "vov"
    parts.append(f"[{last}]format=yuv420p,trim=end_frame={total},setpts=PTS-STARTPTS[v]")
    return ";".join(parts)


def _audio_tail(
    rs: dict[str, Any],
    target_lufs: float,
    total_s: float,
    normalize: bool = True,
    fade_s: float | None = None,
    in_label: str = "amix",
) -> str:
    """loudnorm -> 48 kHz -> exact length -> end fade-out.

    normalize=False skips loudnorm (digital silence, where loudnorm makes the AAC encoder fail).
    """
    sr = int(rs["audio_sample_rate"])
    ns = int(round(total_s * sr))
    fade = float(rs["end_fade_s"] if fade_s is None else fade_s)
    fade_st = max(0.0, total_s - fade)
    norm = f"loudnorm=I={target_lufs}:LRA=11:TP=-1.5," if normalize else ""
    return (
        f"[{in_label}]{norm}aresample={sr},"
        f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo,"
        f"apad=whole_len={ns},atrim=end_sample={ns},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st={fade_st:.3f}:d={fade:.3f}[a]"
    )


def _style_audio_chain(
    sp: Any,
    rs: dict[str, Any],
    total_s: float,
    in_label: str,
    next_index: int,
) -> tuple[list[str], str, str, int]:
    """A1 + A2: (extra inputs, filter chain, final label, next free input index)."""
    sr = int(rs["audio_sample_rate"])
    inputs: list[str] = []
    parts: list[str] = []
    cur = in_label
    if _flag(sp, "sfx") and sp.sfx_cues:
        sfx_in, chain = audio_mix.sfx_chain(sp.sfx_cues, sp.sfx_gains, sr, cur, "a_sfx")
        n = len(sfx_in) // 2
        parts.append(_splice_inputs(chain, n, next_index, "a"))
        inputs += sfx_in
        next_index += n
        cur = "a_sfx"
    if _flag(sp, "music_bed") and sp.music_file:
        mus_in, chain = audio_mix.music_chain(
            total_s, sp.speech_spans, sp.ss, sr, cur, "a_mus", path=sp.music_file
        )
        n = len(mus_in) // 2
        if n:
            parts.append(_splice_inputs(chain, n, next_index, "a"))
            inputs += mus_in
            next_index += n
            cur = "a_mus"
    return inputs, ";".join(parts), cur, next_index


def _emoji_chain(sp: Any, rs: dict[str, Any], next_index: int) -> tuple[list[str], str, int]:
    if not (_flag(sp, "keyword_emoji") and sp.emoji_cues):
        return [], "", next_index
    em_in, chain = overlay.emoji_filter(sp.emoji_cues, sp.layout, sp.ss, rs, "vass", "vov")
    n = len(em_in) // 2
    return em_in, _splice_inputs(chain, n, next_index, "v"), next_index + n


def _run_ffmpeg(cmd: list[str], label: str) -> tuple[bool, float]:
    t0 = time.time()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        elapsed = time.time() - t0
        if res.returncode != 0:
            log.error("ffmpeg %s render error: %s", label, res.stderr[-1500:])
            return False, elapsed
        return True, elapsed
    except Exception as e:
        log.error("ffmpeg %s process error: %s", label, e)
        return False, time.time() - t0


def _encode_args(rs: dict[str, Any], out_p: Path) -> list[str]:
    return [
        "-r", str(rs["fps"]),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", str(rs["audio_bitrate"]),
        "-ar", str(rs["audio_sample_rate"]), "-ac", str(rs["audio_channels"]),
        "-movflags", "+faststart",
        str(out_p),
    ]


def _frames(duration_s: float, fps: int) -> int:
    return max(1, int(math.floor(float(duration_s) * fps + 1e-6)))


# --------------------------------------------------------------------------------------
# Variant renders
# --------------------------------------------------------------------------------------

def render_variant_a(
    cut_path: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    target_lufs: float = -14.0,
    *,
    duration_s: float | None = None,
    cfg: dict[str, Any] | None = None,
    sp: Any = None,
) -> tuple[bool, float]:
    """Render Variant A (1080x1920, 30 fps, 48 kHz stereo AAC, -14 LUFS, end fade).

    duration_s trims the cut (end-snapped length); defaults to the full cut. `sp` is a
    plan.StylePlan: with one, A3 trims, F1/M3/M4 drive the zoom, C2 overlays the emojis and
    A1/A2 mix the SFX and music bed. The output is ffprobe-verified.
    """
    rs = render_settings(cfg)
    cut_p, out_p = Path(cut_path), Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    fps, sr = int(rs["fps"]), int(rs["audio_sample_rate"])
    dur = float(duration_s) if duration_s else (sp.content_s if sp is not None else media_duration(cut_p))
    n = _frames(dur, fps)
    total_s = n / fps
    fade = sp.audio_fade_s if sp is not None else None

    em_inputs, emoji_chain, next_idx = _emoji_chain(sp, rs, 1)
    video = _video_chain(rs, _ass_arg(ass_path, rs), n, 0, sp, emoji_chain)

    cut_has_audio = has_audio_stream(cut_p)
    audio_parts: list[str] = []
    if cut_has_audio:
        audio_parts.append(
            f"[0:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[a_in]"
        )
        src = "a_in"
        if _flag(sp, "dead_air") and len(sp.timeline.kept) > 1:
            audio_parts.append(_spans_chain(sp.timeline.kept, src, "a_kept", audio=True))
            src = "a_kept"
        audio_parts.append(f"[{src}]atrim=end={total_s:.4f},asetpts=PTS-STARTPTS[a_main]")
    else:
        audio_parts.append(f"anullsrc=r={sr}:cl=stereo,atrim=end={total_s:.4f}[a_main]")

    style_inputs, style_chain, last, next_idx = _style_audio_chain(sp, rs, total_s, "a_main", next_idx)
    if style_chain:
        audio_parts.append(style_chain)
    audio_parts.append(_audio_tail(rs, target_lufs, total_s, cut_has_audio, fade, in_label=last))

    filter_complex = video + ";" + ";".join(audio_parts)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(cut_p),
        *em_inputs, *style_inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *_encode_args(rs, out_p),
    ]
    ok, elapsed = _run_ffmpeg(cmd, "Variant A")
    if not ok:
        return False, elapsed
    v_ok, info, errors = verify_render(out_p, cfg, expected_duration=total_s)
    if not v_ok:
        log.error("Variant A verification FAILED for %s: %s (%s)", out_p, "; ".join(errors), info)
        return False, elapsed
    return True, elapsed


def render_variant_b(
    cut_path: Path | str,
    voiceover_wav_path: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    target_lufs: float = -14.0,
    ducking_attenuation_db: float | None = None,  # unused: kept for call compatibility (no ducking)
    *,
    duration_s: float | None = None,
    freeze_s: float | None = None,
    cfg: dict[str, Any] | None = None,
    sp: Any = None,
) -> tuple[bool, float]:
    """Render Variant B in the freeze-frame format.

    Video: first frame of the cut held for freeze_s (default VO duration + freeze_pad_s),
           then the cut plays from its start.
    Audio: VO only during the freeze, then the cut's original audio at full level.
    Total = freeze_s + cut duration. The ASS file must use the same timeline
    (see build_variant_b_words). Output is ffprobe-verified.
    """
    rs = render_settings(cfg)
    cut_p, vo_p, out_p = Path(cut_path), Path(voiceover_wav_path), Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    fps, sr = int(rs["fps"]), int(rs["audio_sample_rate"])
    dur = float(duration_s) if duration_s else (sp.content_s if sp is not None else media_duration(cut_p))
    n_cut = _frames(dur, fps)
    cut_s = n_cut / fps
    if freeze_s is None:
        freeze_s = sp.freeze_s if sp is not None else freeze_duration(media_duration(vo_p), cfg)
    n_freeze = max(1, int(round(float(freeze_s) * fps)))
    freeze_s = n_freeze / fps
    total_s = (n_freeze + n_cut) / fps
    freeze_samples = int(round(freeze_s * sr))
    fade = sp.audio_fade_s if sp is not None else None

    em_inputs, emoji_chain, next_idx = _emoji_chain(sp, rs, 2)
    video = _video_chain(rs, _ass_arg(ass_path, rs), n_cut, n_freeze, sp, emoji_chain)

    audio_parts = [
        f"[1:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo,"
        f"apad=whole_len={freeze_samples},atrim=end_sample={freeze_samples},asetpts=PTS-STARTPTS[vo]"
    ]
    if has_audio_stream(cut_p):
        audio_parts.append(f"[0:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[a_in]")
        src = "a_in"
        if _flag(sp, "dead_air") and len(sp.timeline.kept) > 1:
            audio_parts.append(_spans_chain(sp.timeline.kept, src, "a_kept", audio=True))
            src = "a_kept"
        audio_parts.append(f"[{src}]atrim=end={cut_s:.4f},asetpts=PTS-STARTPTS[a0]")
    else:
        audio_parts.append(f"anullsrc=r={sr}:cl=stereo,atrim=end={cut_s:.4f}[a0]")
    audio_parts.append("[vo][a0]concat=n=2:v=0:a=1[a_main]")

    style_inputs, style_chain, last, next_idx = _style_audio_chain(sp, rs, total_s, "a_main", next_idx)
    if style_chain:
        audio_parts.append(style_chain)
    audio_parts.append(_audio_tail(rs, target_lufs, total_s, True, fade, in_label=last))

    filter_complex = video + ";" + ";".join(audio_parts)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(cut_p),
        "-i", str(vo_p),
        *em_inputs, *style_inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        *_encode_args(rs, out_p),
    ]
    ok, elapsed = _run_ffmpeg(cmd, "Variant B")
    if not ok:
        return False, elapsed
    v_ok, info, errors = verify_render(out_p, cfg, expected_duration=total_s)
    if not v_ok:
        log.error("Variant B verification FAILED for %s: %s (%s)", out_p, "; ".join(errors), info)
        return False, elapsed
    return True, elapsed


def snap_render_end(
    cut_path: Path | str,
    cut_duration: float,
    speech_words: list[dict[str, Any]] | None,
    cfg: dict[str, Any] | None = None,
    clean_cut_end: bool = False,
) -> float:
    """Pick the Variant A/B content length inside the existing cut.

    With speech: the sentence break nearest the cut's end inside
    [min_duration, min(max_duration, cut_duration)] (pipeline.endings), so it never cuts
    earlier than the last completed sentence in the cut and never inside a word;
    `clean_cut_end` lets the cut's own end count when the cut stage verified it. Raises
    endings.NoCleanEnding when speech runs through every allowed end.
    Without speech: the ebur128 trough, as before."""
    cfg = cfg if cfg is not None else config.get_config()
    cut_cfg = cfg.get("cut", {})
    min_dur = float(cut_cfg.get("min_duration", 18.0))
    max_dur = float(cut_cfg.get("max_duration", 22.0))
    if speech_words:
        end, _spans = endings.content_end(
            speech_words, cut_duration, min_dur, max_dur, endings.ending_settings(cfg),
            trusted_end=cut_duration if clean_cut_end else None,
        )
        return end
    if cut_duration <= min_dur:
        return cut_duration
    return snap_end_to_pause(
        0.0,
        cut_duration,
        loudness=extract_audio_loudness_profile(cut_path),
        min_dur=min_dur,
        max_dur=max_dur,
        hard_max_end=cut_duration,
    )


def render_item_variants(
    cut_path: Path | str,
    vo_path: Path | str,
    hook_text: str,
    speech_words: list[dict[str, Any]],
    vo_words: list[dict[str, Any]],
    out_a: Path | str,
    out_b: Path | str,
    subtitles_dir: Path | str,
    cfg: dict[str, Any] | None = None,
    name: str = "item",
    *,
    preset: str | None = None,
    channel_name: str = "",
    keyword_list: Sequence[str] | None = None,
    clean_cut_end: bool = False,
) -> dict[str, Any]:
    """Analyze the cut, plan the style for both variants, render A and B, verify both.

    speech_words are on the cut timeline (0 = cut start); vo_words on the VO timeline.
    `preset` defaults to `style.preset` in config (P0). `clean_cut_end`: the cut stage
    verified the cut's own end (see cut_end_verified).
    Raises RenderVerificationError if either output fails verification, and
    endings.NoCleanEnding if no speech-safe end exists.
    """
    cfg = cfg if cfg is not None else config.get_config()
    rs = render_settings(cfg)
    fps = int(rs["fps"])
    target_lufs = float(rs["target_lufs"])
    preset_name = preset or style.default_preset(cfg)

    cut_dur = media_duration(cut_path)
    analysis = planning.analyze_clip(cut_path, speech_words, cfg=cfg, preset=preset_name, duration=cut_dur,
                                     clean_cut_end=clean_cut_end)
    vo_dur = media_duration(vo_path)
    freeze_s = freeze_duration(vo_dur, cfg)

    plan_a = planning.build_style_plan(
        analysis, hook_text, variant="A", freeze_s=0.0, cfg=cfg, preset=preset_name,
        channel_name=channel_name, keyword_list=keyword_list,
    )
    plan_b = planning.build_style_plan(
        analysis, hook_text, variant="B", freeze_s=freeze_s, vo_words=vo_words, cfg=cfg,
        preset=preset_name, channel_name=channel_name, keyword_list=keyword_list,
    )
    content_s = plan_a.content_s

    subs = Path(subtitles_dir)
    subs.mkdir(parents=True, exist_ok=True)
    ass_a, ass_b = subs / f"{name}_A.ass", subs / f"{name}_B.ass"
    use_style = preset_name is not None
    ass_a.write_text(
        build_ass_subtitles(hook_text, plan_a.total_s, plan_a.words, cfg=cfg, sp=plan_a if use_style else None),
        encoding="utf-8",
    )
    ass_b.write_text(
        build_ass_subtitles(hook_text, plan_b.total_s, plan_b.words, cfg=cfg, sp=plan_b if use_style else None),
        encoding="utf-8",
    )

    ok_a, el_a = render_variant_a(
        cut_path, ass_a, out_a, target_lufs, duration_s=content_s, cfg=cfg, sp=plan_a
    )
    if not ok_a:
        raise RenderVerificationError(f"Variant A render/verification failed: {out_a}")
    ok_b, el_b = render_variant_b(
        cut_path, vo_path, ass_b, out_b, target_lufs,
        duration_s=content_s, freeze_s=freeze_s, cfg=cfg, sp=plan_b,
    )
    if not ok_b:
        raise RenderVerificationError(f"Variant B render/verification failed: {out_b}")
    _, probe_a, _ = verify_render(out_a, cfg)
    _, probe_b, _ = verify_render(out_b, cfg)
    return {
        "preset": preset_name,
        "cut_duration_s": round(cut_dur, 3),
        "end_s": round(content_s, 3),
        "source_end_s": round(analysis.end_s, 3),
        "vo_duration_s": round(vo_dur, 3),
        "freeze_s": round(freeze_s, 3),
        "elapsed_a_s": round(el_a, 2),
        "elapsed_b_s": round(el_b, 2),
        "probe_a": probe_a,
        "probe_b": probe_b,
        "ass_a": str(ass_a),
        "ass_b": str(ass_b),
        "style_a": plan_a.summary(),
        "style_b": plan_b.summary(),
    }


def run_render_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    preset: str | None = None,
) -> tuple[bool, str, dict[str, float]]:
    """Execute complete render stage for an item in 'cut' stage.

    Generates Variant A and Variant B, validates both, purges download, and updates DB.
    Returns (success: bool, message: str, timing_stats: dict).
    """
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found", {}

    if not db.is_releasable(item):
        # A preview_only item can never enter 'rendered' (SQLite trigger). Skip it up front
        # instead of rendering for minutes and then being refused; its state is left untouched.
        return False, "preview_only item: not rendered by the pipeline", {}

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
    if not vo_path or not vo_path.exists():
        db.mark_failed(item_id, "render", "Voiceover audio missing on disk", db_path=db_path)
        return False, "Voiceover audio missing", {}

    rs = render_settings(cfg)
    whisper_model = rs["whisper_model"]
    speech_words = cut_speech_words(item, cut_path, whisper_model)
    log.info("%d speech words on the cut timeline.", len(speech_words))

    # VO words on the VO timeline (VO starts at 0 in Variant B's freeze).
    vo_words = transcribe_words(vo_path, model_name=whisper_model)
    if not vo_words:
        vo_words = estimate_words_from_script(item.get("voiceover_script") or "", media_duration(vo_path))

    # C1: Gemini keywords cached on the item, else the rule fallback.
    keyword_list = kwmod.keywords_for_item(item, speech_words, cfg=cfg, db_path=db_path, item_id=item_id)

    renders_dir = config.media_dir("renders")
    render_a_path = renders_dir / f"{item_id}_A.mp4"
    render_b_path = renders_dir / f"{item_id}_B.mp4"

    log.info("Rendering Variants A and B for %s (preset %s)...", item_id, preset or style.default_preset(cfg))
    try:
        result = render_item_variants(
            cut_path,
            vo_path,
            hook_text,
            speech_words,
            vo_words,
            render_a_path,
            render_b_path,
            config.media_dir("subtitles"),
            cfg=cfg,
            name=item_id,
            preset=preset,
            channel_name=item.get("author_name") or "",
            keyword_list=keyword_list,
            clean_cut_end=cut_end_verified(item, whisper_model),
        )
    except (RenderVerificationError, endings.NoCleanEnding) as e:
        db.mark_failed(item_id, "render", str(e), db_path=db_path)
        return False, str(e), {}

    elapsed_a, elapsed_b = result["elapsed_a_s"], result["elapsed_b_s"]
    dur_a = result["probe_a"]["video_duration"]
    dur_b = result["probe_b"]["video_duration"]

    # Generate Creator Credit Social Caption
    author = item.get("author_name") or "Creator"
    caption_text = posting.build_caption(hook_text, author, item.get("source") or "youtube", cfg=cfg)

    # Purge raw source download immediately per project retention rule
    download_path_str = item.get("download_path")
    if download_path_str:
        Path(download_path_str).unlink(missing_ok=True)
        log.info("Purged raw source download: %s", download_path_str)

    # Transition item to 'rendered' stage
    try:
        db.transition_stage(
            item_id,
            "rendered",
            extra_fields={
                "render_a_path": str(render_a_path),
                "render_b_path": str(render_b_path),
                "caption_text": caption_text,
                "style_preset": result["preset"],
            },
            db_path=db_path,
        )
    except sqlite3.IntegrityError as e:
        # The preview_only trigger refused: fail this one item, never crash the runner on it.
        db.mark_failed(item_id, "render", f"refused by the database: {e}", db_path=db_path)
        return False, f"refused by the database: {e}", {}
    db.log_stage(
        item_id,
        "render",
        "success",
        message=(
            f"Renders completed [{result['preset']}]: A ({elapsed_a:.1f}s, {dur_a:.1f}s), "
            f"B ({elapsed_b:.1f}s, {dur_b:.1f}s)"
        ),
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
