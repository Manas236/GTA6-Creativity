"""ASS tags and overlay filter fragments for the style layer.

  C1  word_colour_tag     green keywords, yellow stays the spoken word
  C2  plan_emoji_cues     keyword -> Twemoji PNG, max one per min_gap_s
      emoji_filter        the overlay chain (libass cannot draw colour emoji)
  C3  progress_bar_event  a 10 px yellow ASS rectangle scaled by \\t across the clip
  C4  credit_event        "via <channel>" at 80% opacity, bottom-left of the video band
  M1  caption_pop_tags    80% -> 115% -> 100%
  M2  hook_pop_tags       scale-in plus a decaying \\frz shake

Nothing here runs ffmpeg; `emoji_filter` returns input arguments and a filter string for
pipeline.render to splice into its graph.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config, keywords as kw

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# C1 keyword colour
# --------------------------------------------------------------------------------------

def word_colour(
    token: str,
    active: bool,
    keyword_list: Iterable[str],
    rs: dict[str, Any],
    ss: dict[str, Any],
    keyword_color: bool,
) -> str:
    """ASS primary colour for one caption word.

    Yellow always wins for the word being spoken; green marks a keyword; white otherwise.
    """
    if active:
        return rs["caption"]["highlight_color"]
    if keyword_color and kw.is_keyword(token, keyword_list):
        return ss["keyword_color"]["color"]
    return rs["caption"]["color"]


# --------------------------------------------------------------------------------------
# M1 / M2 motion tags
# --------------------------------------------------------------------------------------

def caption_pop_tags(ss: dict[str, Any]) -> str:
    """M1: scale 80% -> 115% -> 100% over ~160 ms (times are ms from the event start)."""
    cp = ss["caption_pop"]
    f, pk = int(cp["from_scale"]), int(cp["peak_scale"])
    t1, t2 = int(cp["peak_ms"]), int(cp["settle_ms"])
    return (
        f"\\fscx{f}\\fscy{f}"
        f"\\t(0,{t1},\\fscx{pk}\\fscy{pk})"
        f"\\t({t1},{t2},\\fscx100\\fscy100)"
    )


def hook_pop_tags(ss: dict[str, Any], org_x: float, org_y: float) -> str:
    """M2: scale in within scale_ms, plus a decaying rotation shake over shake_ms."""
    hp = ss["hook_pop"]
    f = int(hp["from_scale"])
    scale_ms = int(hp["scale_ms"])
    shake_ms = max(1, int(hp["shake_ms"]))
    steps = max(1, int(hp["shake_steps"]))
    amp = float(hp["shake_deg"])
    tags = [f"\\org({org_x:.0f},{org_y:.0f})", f"\\fscx{f}\\fscy{f}", f"\\t(0,{scale_ms},\\fscx100\\fscy100)"]
    step_ms = shake_ms // steps
    for i in range(steps):
        a = 0.0 if i == steps - 1 else round(amp * (1.0 - i / steps) * (1 if i % 2 == 0 else -1), 2)
        tags.append(f"\\t({i * step_ms},{(i + 1) * step_ms},\\frz{a})")
    return "".join(tags)


# --------------------------------------------------------------------------------------
# C3 progress bar (an ASS rectangle, because drawbox has no time variable)
# --------------------------------------------------------------------------------------

def progress_bar_style(rs: dict[str, Any], ss: dict[str, Any]) -> str:
    pb = ss["progress_bar"]
    return (
        f"Style: Bar,{rs['font_name']},100,{pb['color']},&H00000000,&H00000000,&H00000000,"
        f"0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"
    )


def progress_bar_event(duration: float, rs: dict[str, Any], ss: dict[str, Any], timestamp: Any, y: int = 0) -> str:
    """One event: a W x height_px rectangle anchored top-left at (0, y), \\fscx 0 -> 100 over the clip.
    `y` is 0 (the canvas top edge) unless source text sits there (style.Layout.progress_y)."""
    pb = ss["progress_bar"]
    w, h = int(rs["width"]), int(pb["height_px"])
    ms = max(1, int(round(float(duration) * 1000)))
    tags = (
        f"{{\\an7\\pos(0,{int(y)})\\bord0\\shad0\\1c{pb['color'].replace('&H00', '&H')}"
        f"\\fscx0\\fscy100\\t(0,{ms},\\fscx100)\\p1}}"
    )
    return (
        f"Dialogue: 3,{timestamp(0.0)},{timestamp(duration)},Bar,,0,0,0,,"
        f"{tags}m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}"
    )


# --------------------------------------------------------------------------------------
# C4 credit tag
# --------------------------------------------------------------------------------------

def credit_style(rs: dict[str, Any], ss: dict[str, Any]) -> str:
    ct = ss["credit_tag"]
    return (
        f"Style: Credit,{rs['font_name']},{int(ct['font_size'])},{ct['color']},&H00000000,"
        f"&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{ct['outline']},{ct['shadow']},1,"
        f"{rs['safe_area']['x_min']},{rs['width'] - rs['safe_area']['x_max']},0,1"
    )


def credit_text(channel: str, ss: dict[str, Any]) -> str:
    name = str(channel or "").replace("{", "").replace("}", "").replace("\\", "").strip() or "UNKNOWN"
    return str(ss["credit_tag"]["text"]).replace("{channel}", name)


def credit_event(
    channel: str,
    duration: float,
    layout: Any,
    ss: dict[str, Any],
    timestamp: Any,
) -> str:
    """Bottom-left of the video band, white at ~80% opacity, for the whole clip."""
    ct = ss["credit_tag"]
    tags = f"{{\\an1\\pos({layout.credit_x},{layout.credit_y})\\alpha{ct['alpha']}}}"
    return (
        f"Dialogue: 2,{timestamp(0.0)},{timestamp(duration)},Credit,,0,0,0,,"
        f"{tags}{credit_text(channel, ss)}"
    )


# --------------------------------------------------------------------------------------
# C2 keyword emojis
# --------------------------------------------------------------------------------------

@dataclass
class EmojiCue:
    at: float
    keyword: str
    codepoint: str
    path: str
    hold: float


def emoji_path(codepoint: str, ss: dict[str, Any]) -> Path:
    d = Path(str(ss["keyword_emoji"]["assets_dir"]))
    if not d.is_absolute():
        d = config.project_root() / d
    return d / f"{str(codepoint).lower()}.png"


def emoji_map(ss: dict[str, Any]) -> dict[str, str]:
    return {kw.normalize(k): str(v).lower() for k, v in (ss["keyword_emoji"].get("map") or {}).items() if k}


def plan_emoji_cues(
    words: Sequence[dict[str, Any]],
    keyword_list: Iterable[str],
    ss: dict[str, Any],
    duration: float,
    require_asset: bool = True,
) -> list[EmojiCue]:
    """One emoji per mapped keyword occurrence, rate-limited to one per `min_gap_s`."""
    ke = ss["keyword_emoji"]
    mapping = emoji_map(ss)
    if not mapping:
        return []
    allowed = {kw.normalize(k) for k in keyword_list}
    gap = float(ke["min_gap_s"])
    hold = float(ke["hold_s"])
    cues: list[EmojiCue] = []
    for w in sorted(words, key=lambda w: float(w["start"])):
        tok = kw.normalize(w.get("word", ""))
        if tok not in mapping or (allowed and tok not in allowed):
            continue
        at = float(w["start"])
        if at < 0 or at + 0.2 > float(duration):
            continue
        if cues and at - cues[-1].at < gap:
            continue
        cp = mapping[tok]
        p = emoji_path(cp, ss)
        if require_asset and not p.exists():
            log.debug("No Twemoji asset for keyword %s (%s)", tok, p)
            continue
        cues.append(EmojiCue(round(at, 3), tok, cp, str(p), min(hold, float(duration) - at)))
    return cues


def emoji_filter(
    cues: Sequence[EmojiCue],
    layout: Any,
    ss: dict[str, Any],
    rs: dict[str, Any],
    in_label: str,
    out_label: str,
) -> tuple[list[str], str]:
    """Return (ffmpeg input args, filter chain) overlaying each cue above the caption.

    Each PNG is a one-frame input scaled to size_px; it rises `rise_px` over `rise_ms`
    and is gated with `enable=between(t, at, at+hold)`.
    """
    if not cues:
        return [], f"[{in_label}]null[{out_label}]"
    ke = ss["keyword_emoji"]
    size = int(ke["size_px"])
    rise = float(ke["rise_px"])
    rise_s = max(0.001, float(ke["rise_ms"]) / 1000.0)
    gap = float(ke["gap_above_caption_px"])
    x = int(rs["width"] / 2 - size / 2)
    y_rest = int(layout.caption_ink[0] - gap - size)

    inputs: list[str] = []
    parts: list[str] = []
    cur = in_label
    for i, cue in enumerate(cues):
        inputs += ["-i", cue.path]
        src = f"{{IDX{i}}}"           # render.py substitutes the real ffmpeg input index
        parts.append(f"[{src}]scale={size}:{size}:flags=lanczos,format=rgba[em{i}]")
        nxt = out_label if i == len(cues) - 1 else f"emv{i}"
        y = f"{y_rest}+{rise:.0f}*(1-min(1,(t-{cue.at:.3f})/{rise_s:.3f}))"
        # The PNG is a single frame, so overlay must hold it (repeatlast=1, the default
        # eof_action=repeat). eof_action=pass would drop the emoji after frame 1.
        parts.append(
            f"[{cur}][em{i}]overlay=x={x}:y='{y}':repeatlast=1:shortest=0:"
            f"enable='between(t,{cue.at:.3f},{cue.at + cue.hold:.3f})'[{nxt}]"
        )
        cur = nxt
    return inputs, ";".join(parts)
