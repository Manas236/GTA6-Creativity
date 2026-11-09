"""Render primitives shared by the style planner and the renderer.

Render settings, libass font metrics, path escaping and the ffprobe readers live here
(rather than in pipeline.render) so that pipeline.plan can measure text and probe media
without importing the renderer, which imports the planner. pipeline.render re-exports
everything in this module, so `render.probe_streams`, `render.cap_height_px` and the rest
keep working.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)

RENDER_DEFAULTS: dict[str, Any] = {
    "width": 1080,
    "height": 1920,
    "fps": 30,
    "audio_sample_rate": 48000,
    "audio_channels": 2,
    "audio_bitrate": "192k",
    "fonts_dir": "assets/fonts",
    "font_path": "assets/fonts/Montserrat-Black.ttf",
    "font_name": "Montserrat Black",
    "whisper_model": "base.en",
    "target_lufs": -14.0,
    "freeze_pad_s": 0.3,
    "end_fade_s": 0.25,
    "av_duration_tolerance_s": 0.1,
    "safe_area": {"x_min": 90, "x_max": 990, "y_min": 250, "y_max": 1440},
    "caption": {
        "font_size": 138,
        "outline": 7,
        "shadow": 3,
        "max_words": 3,
        "max_gap_s": 0.6,
        "center_y": 1300,
        "color": "&H00FFFFFF",
        "highlight_color": "&H0000FFFF",
    },
    "hook": {
        "font_size": 150,
        "min_font_size": 96,
        "outline": 8,
        "shadow": 3,
        "max_lines": 2,
        "band_top_y": 260,
        "band_bottom_y": 500,
        "line_spacing": 1.75,
        "min_line_gap_px": 18,
        "color": "&H0000FFFF",
    },
}


def render_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the render config merged over RENDER_DEFAULTS (nested dicts merged one level)."""
    cfg = cfg if cfg is not None else config.get_config()
    user = dict(cfg.get("render", {}) or {})
    out: dict[str, Any] = {}
    for k, v in RENDER_DEFAULTS.items():
        if isinstance(v, dict):
            merged = dict(v)
            merged.update(user.get(k) or {})
            out[k] = merged
        else:
            out[k] = user.get(k, v)
    for k, v in user.items():
        out.setdefault(k, v)
    return out


def _abs_project_path(p: str | Path) -> Path:
    path = Path(p)
    return path if path.is_absolute() else config.project_root() / path


def escape_filter_path(p: Path | str) -> str:
    """Format path for ffmpeg filter arguments, escaping drive colon and normalizing slashes."""
    p_str = str(p).replace(os.sep, "/")
    try:
        rel = os.path.relpath(p).replace(os.sep, "/")
        if not rel.startswith("../") and not rel.startswith("..\\"):
            p_str = rel
    except Exception:
        pass
    return p_str.replace(":", r"\:")


# --------------------------------------------------------------------------------------
# Font metrics (libass sizing)
# --------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _font_metrics(font_path: str) -> dict[str, float]:
    """Return metrics libass uses: it scales the face so usWinAscent + usWinDescent == Fontsize."""
    from fontTools.ttLib import TTFont

    t = TTFont(font_path, lazy=True)
    os2 = t["OS/2"]
    upm = float(t["head"].unitsPerEm)
    real_dim = float(os2.usWinAscent + os2.usWinDescent) or upm
    return {
        "upm": upm,
        "real_dim": real_dim,
        "cap_height": float(getattr(os2, "sCapHeight", 0) or 0.7 * upm),
        "ascent": float(getattr(os2, "sTypoAscender", 0) or 0.97 * upm),
        "descent": abs(float(getattr(os2, "sTypoDescender", 0) or 0.21 * upm)),
        "weight": float(os2.usWeightClass),
    }


def em_px(font_size: float, font_path: str | Path) -> float:
    m = _font_metrics(str(_abs_project_path(font_path)))
    return float(font_size) * m["upm"] / m["real_dim"]


def cap_height_px(font_size: float, font_path: str | Path) -> float:
    """Predicted rendered cap height in pixels for an ASS Fontsize at PlayRes 1080x1920."""
    m = _font_metrics(str(_abs_project_path(font_path)))
    return float(font_size) * m["cap_height"] / m["real_dim"]


def descent_px(font_size: float, font_path: str | Path) -> float:
    """Depth below the baseline, for mixed-case text (the credit tag's "via ...")."""
    m = _font_metrics(str(_abs_project_path(font_path)))
    return float(font_size) * m["descent"] / m["real_dim"]


def ascent_px(font_size: float, font_path: str | Path) -> float:
    """Height above the baseline. Lower-case ascenders ('l', 'b', the dot on 'i') reach
    well above the cap height, so mixed-case text must be measured with this, not with
    cap_height_px."""
    m = _font_metrics(str(_abs_project_path(font_path)))
    return float(font_size) * m["ascent"] / m["real_dim"]


@functools.lru_cache(maxsize=4096)
def _text_width_cached(text: str, font_size: float, font_path: str) -> float:
    from PIL import ImageFont

    size = em_px(font_size, font_path)
    font = ImageFont.truetype(font_path, size=max(1.0, size))
    return float(font.getlength(text))


def text_width_px(text: str, font_size: float, font_path: str | Path) -> float:
    """Advance width of `text` in canvas pixels as libass would lay it out (no outline)."""
    return _text_width_cached(text, float(font_size), str(_abs_project_path(font_path)))


# --------------------------------------------------------------------------------------
# ffprobe readers
# --------------------------------------------------------------------------------------

def has_audio_stream(video_path: Path | str) -> bool:
    """Check if video contains an audio stream using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
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


def _rate(r: str | None) -> float:
    if not r or r in ("0/0", "N/A"):
        return 0.0
    if "/" in r:
        n, d = r.split("/", 1)
        return float(n) / float(d) if float(d) else 0.0
    return float(r)


def probe_streams(path: Path | str) -> dict[str, Any]:
    """ffprobe the first video and audio streams. Durations are STREAM durations."""
    cmd = ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    data = json.loads(res.stdout or "{}")
    info: dict[str, Any] = {"path": str(path), "format_duration": float(data.get("format", {}).get("duration", 0) or 0)}
    for st in data.get("streams", []):
        kind = st.get("codec_type")
        if kind == "video" and "video_duration" not in info:
            info.update({
                "video_duration": float(st.get("duration", 0) or 0),
                "fps": round(_rate(st.get("avg_frame_rate")), 3),
                "width": int(st.get("width", 0)),
                "height": int(st.get("height", 0)),
                "video_frames": int(st.get("nb_frames", 0) or 0),
            })
        elif kind == "audio" and "audio_duration" not in info:
            info.update({
                "audio_duration": float(st.get("duration", 0) or 0),
                "sample_rate": int(st.get("sample_rate", 0) or 0),
                "channels": int(st.get("channels", 0) or 0),
            })
    return info


def media_duration(path: Path | str) -> float:
    """Duration of a media file (video stream if present, else audio, else container)."""
    info = probe_streams(path)
    return float(info.get("video_duration") or info.get("audio_duration") or info.get("format_duration") or 0.0)


# --------------------------------------------------------------------------------------
# Text layout (used by both the planner and the ASS builder)
# --------------------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    """Upper-case and strip characters that would be read as ASS override/escape syntax."""
    t = str(text).replace("{", "").replace("}", "").replace("\\", "").strip()
    return t.upper()


def _clean_caption_word(word: str) -> str:
    return _clean_text(word).strip(",.;:\"“”")


def layout_hook(hook_text: str, rs: dict[str, Any]) -> tuple[list[str], int]:
    """Wrap the hook into <= max_lines lines that fit the safe-area width.

    Returns (lines, font_size). Shrinks the font only if 2 lines cannot fit.
    """
    import math

    hk = rs["hook"]
    sa = rs["safe_area"]
    font_path = rs["font_path"]
    max_w = (sa["x_max"] - sa["x_min"]) - 2 * (hk["outline"] + hk["shadow"])
    words = _clean_text(hook_text).split()
    if not words:
        return [], int(hk["font_size"])
    max_lines = max(1, int(hk.get("max_lines", 2)))
    size = float(hk["font_size"])

    def best_split(sz: float) -> tuple[list[str], float]:
        one = " ".join(words)
        candidates: list[tuple[float, list[str]]] = [(text_width_px(one, sz, font_path), [one])]
        if max_lines >= 2 and len(words) >= 2:
            for i in range(1, len(words)):
                lines = [" ".join(words[:i]), " ".join(words[i:])]
                w = max(text_width_px(l, sz, font_path) for l in lines)
                candidates.append((w, lines))
        fitting = [c for c in candidates if c[0] <= max_w]
        if fitting:
            # Prefer fewest lines, then most balanced.
            fitting.sort(key=lambda c: (len(c[1]), c[0]))
            if len(fitting[0][1]) == 1:
                return fitting[0][1], fitting[0][0]
            return min(fitting, key=lambda c: c[0])[1], min(c[0] for c in fitting)
        widest = min(candidates, key=lambda c: c[0])
        return widest[1], widest[0]

    for _ in range(20):
        lines, width = best_split(size)
        if width <= max_w:
            break
        size = math.floor(size * max_w / width) - 1
    if size < float(hk.get("min_font_size", 0)):
        log.warning("Hook %r needed font size %.0f (< min %s) to fit 2 lines", hook_text, size, hk.get("min_font_size"))
    return lines, int(size)


def hook_line_pitch(font_size: float, rs: dict[str, Any]) -> float:
    """Distance between hook lines.

    `line_spacing` is a multiple of the cap height, but the outline and shadow are
    ABSOLUTE pixels, so on a hook that had to shrink to fit they eat the whole gap (a
    68 px hook at 1.75 left 3 px between the outlines). The pitch therefore never drops
    below one line's full ink height plus `min_line_gap_px`.
    """
    hk = rs["hook"]
    cap = cap_height_px(font_size, rs["font_path"])
    line_ink = cap + 2.0 * float(hk["outline"]) + float(hk["shadow"])
    return max(cap * float(hk.get("line_spacing", 1.75)), line_ink + float(hk.get("min_line_gap_px", 18)))


def hook_line_centers(
    n_lines: int,
    font_size: float,
    rs: dict[str, Any],
    center: float | None = None,
    pitch: float | None = None,
) -> list[float]:
    """Vertical centres of each hook line, centred in the band above the video.

    `center` / `pitch` override the config band (the style layer re-places the band so
    the hook clears the zoom-to-fill video edge).
    """
    hk = rs["hook"]
    if center is None:
        center = (float(hk["band_top_y"]) + float(hk["band_bottom_y"])) / 2.0
    if pitch is None:
        pitch = hook_line_pitch(font_size, rs)
    return [float(center) + (i - (n_lines - 1) / 2.0) * float(pitch) for i in range(n_lines)]


def chunk_caption_words(words: list[dict[str, Any]], rs: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """Group words into 1-3 word chunks that fit the safe-area width on one line."""
    cap = rs["caption"]
    sa = rs["safe_area"]
    max_w = (sa["x_max"] - sa["x_min"]) - 2 * (cap["outline"] + cap["shadow"])
    max_words = max(1, int(cap.get("max_words", 3)))
    max_gap = float(cap.get("max_gap_s", 0.6))
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for w in words:
        if not _clean_caption_word(w["word"]):
            continue
        if cur:
            trial = " ".join(_clean_caption_word(x["word"]) for x in cur + [w])
            too_wide = text_width_px(trial, cap["font_size"], rs["font_path"]) > max_w
            if len(cur) >= max_words or (w["start"] - cur[-1]["end"]) > max_gap or too_wide:
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks
