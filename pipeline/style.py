"""Style layer: edit-effect presets, flags, numbers and safe-area placement.

Every effect is an independently toggleable flag; presets (P0-P3) are declared in
`config.yaml` as deltas over a base preset and resolved here into a flat
{flag: bool} map. Every number lives in `config.yaml` -> `style:` (defaults below
so an older config still loads).

Flags
    zoom_fill      F1  zoom-to-fill framing with per-segment fit/fill decision
    keyword_color  C1  green keywords in captions (yellow stays the spoken word)
    keyword_emoji  C2  Twemoji PNG popping in above the caption
    progress_bar   C3  yellow bar filling along the top edge
    credit_tag     C4  "via <channel>" in the bottom-left of the video band
    caption_pop    M1  caption chunk scale pop-in
    hook_pop       M2  hook scale-in plus a small shake
    punch_ins      M3  1.12x punch-ins on loudness peaks / scene cuts
    slow_push      M4  1.00 -> 1.06 push across static segments and the B freeze
    sfx            A1  boom on the hook, a whoosh on at most one well-spaced punch-in
    music_bed      A2  CC0 music bed under the clip, ducked under speech
    dead_air       A3  drop silent + motionless stretches, remap captions
    loop_end       A4  end on the frame closest to frame 1 (dHash)
"""
from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from . import config

log = logging.getLogger(__name__)

STYLE_FLAGS: tuple[str, ...] = (
    "zoom_fill",
    "keyword_color",
    "keyword_emoji",
    "progress_bar",
    "credit_tag",
    "caption_pop",
    "hook_pop",
    "punch_ins",
    "slow_push",
    "sfx",
    "music_bed",
    "dead_air",
    "loop_end",
)

#: Presets as deltas over a base preset. P0 is the pipeline default.
PRESET_DEFAULTS: dict[str, dict[str, Any]] = {
    "P0": {"base": None, "enable": ["zoom_fill"]},
    "P1": {
        "base": "P0",
        "enable": [
            "keyword_color", "keyword_emoji", "progress_bar", "credit_tag",
            "caption_pop", "dead_air", "loop_end",
        ],
    },
    "P2": {"base": "P1", "enable": ["hook_pop", "punch_ins", "sfx"]},
    "P3": {"base": "P2", "enable": ["slow_push", "music_bed"]},
}

STYLE_DEFAULTS: dict[str, Any] = {
    "preset": "P0",
    "presets": PRESET_DEFAULTS,
    # F1 ------------------------------------------------------------------
    "zoom_fill": {
        "fill_scale": 1.40,
        "supersample": 1.5,
        "crossfade_s": 0.25,
        "motion_threshold": 0.018,
        "motion_step_s": 0.2,
        "min_segment_s": 1.2,
        "edge_clearance_px": 40,
    },
    # M1 ------------------------------------------------------------------
    "caption_pop": {"from_scale": 80, "peak_scale": 115, "peak_ms": 60, "settle_ms": 160},
    # M2 ------------------------------------------------------------------
    "hook_pop": {"from_scale": 55, "scale_ms": 250, "shake_deg": 1.5, "shake_ms": 400, "shake_steps": 4},
    # M3 ------------------------------------------------------------------
    "punch_ins": {
        "zoom": 1.12,
        "ease_in_s": 0.12,
        "hold_s": 0.8,
        "ease_out_s": 0.12,
        "min_gap_s": 3.0,
        "max_count": 4,
        "first_at_least_s": 1.5,
    },
    # M4 ------------------------------------------------------------------
    "slow_push": {"zoom_to": 1.06},
    # C1 ------------------------------------------------------------------
    "keyword_color": {
        "color": "&H0000FF00",
        "min_keywords": 3,
        "max_keywords": 6,
        "extra_terms": [],
    },
    # C2 ------------------------------------------------------------------
    "keyword_emoji": {
        "assets_dir": "assets/emoji",
        "size_px": 110,
        "min_gap_s": 4.0,
        "hold_s": 1.6,
        "rise_px": 24,
        "rise_ms": 120,
        "gap_above_caption_px": 28,
        "map": {},
    },
    # C3 ------------------------------------------------------------------
    "progress_bar": {"height_px": 10, "color": "&H0000FFFF"},
    # C4 ------------------------------------------------------------------
    "credit_tag": {
        "text": "via {channel}",
        "font_size": 71,
        "color": "&H00FFFFFF",
        "alpha": "&H33&",
        "outline": 3,
        "shadow": 2,
        "inset_x_px": 0,
        "inset_y_px": 16,
        "gap_from_caption_px": 24,
    },
    # A1 ------------------------------------------------------------------
    "sfx": {
        "dir": "assets/sfx",
        "boom_file": "boom.wav",
        "whoosh_file": "whoosh.wav",
        "headroom_db": 6.0,
        "boom_at_s": 0.0,
        "whoosh_lead_s": 0.06,
        "min_gap_s": 5.0,
        "max_per_clip": 2,
    },
    # A2 ------------------------------------------------------------------
    "music_bed": {
        "file": "assets/music/bed.mp3",
        "level_db": -24.0,
        "duck_db": -8.0,
        "duck_ramp_s": 0.08,
        "speech_merge_gap_s": 0.3,
    },
    # A3 ------------------------------------------------------------------
    "dead_air": {
        "min_silence_s": 0.4,
        "loudness_threshold_lufs": -45.0,
        "motion_threshold": 0.012,
        "keep_pad_s": 0.12,
        "min_output_s": 15.0,
        "loudness_lag_s": 0.2,
    },
    # A4 ------------------------------------------------------------------
    "loop_end": {"search_step_s": 0.2, "pause_bonus_bits": 6, "audio_fade_s": 0.15},
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def style_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return `style:` from config deep-merged over STYLE_DEFAULTS."""
    cfg = cfg if cfg is not None else config.get_config()
    return _deep_merge(STYLE_DEFAULTS, dict(cfg.get("style", {}) or {}))


def preset_names(cfg: dict[str, Any] | None = None) -> list[str]:
    return sorted((style_settings(cfg).get("presets") or {}).keys())


def default_preset(cfg: dict[str, Any] | None = None) -> str:
    """The preset the pipeline renders with unless a caller asks for another."""
    return str(style_settings(cfg).get("preset") or "P0")


def resolve_preset(preset: str | None = None, cfg: dict[str, Any] | None = None) -> dict[str, bool]:
    """Resolve a preset name into a flat {flag: bool} map by walking its `base` chain."""
    ss = style_settings(cfg)
    presets = ss.get("presets") or {}
    name = preset or str(ss.get("preset") or "P0")
    if name not in presets:
        raise KeyError(f"Unknown style preset {name!r}; known: {sorted(presets)}")

    chain: list[str] = []
    cur: str | None = name
    while cur:
        if cur in chain:
            raise ValueError(f"Cyclic style preset base chain at {cur!r}")
        chain.append(cur)
        if cur not in presets:
            raise KeyError(f"Unknown style preset base {cur!r}")
        cur = presets[cur].get("base")

    flags = {f: False for f in STYLE_FLAGS}
    for nm in reversed(chain):  # base first, then each delta
        spec = presets[nm]
        for f in spec.get("enable") or []:
            if f not in flags:
                raise KeyError(f"Preset {nm!r} enables unknown style flag {f!r}")
            flags[f] = True
        for f in spec.get("disable") or []:
            if f not in flags:
                raise KeyError(f"Preset {nm!r} disables unknown style flag {f!r}")
            flags[f] = False
    return flags


# --------------------------------------------------------------------------------------
# F1 geometry: the foreground band for each framing mode
# --------------------------------------------------------------------------------------

def _even(x: float) -> int:
    return int(round(float(x) / 2.0) * 2)


@dataclass(frozen=True)
class FillGeometry:
    """Sizes for the zoom-to-fill foreground layer.

    The foreground is always composited as `out_w x fill_h`. Inside it the source sits
    on a supersampled transparent canvas and zoompan zooms between:
      zoom 1.0        -> the whole frame fits full width (`fit_h` tall, bg visible above/below)
      zoom fill_zoom  -> the frame fills the band, sides cropped
    """
    out_w: int
    fit_h: int          # foreground height of the full-width fit (1080x608 for 16:9)
    fill_h: int         # foreground height when filling (the composited layer height)
    canvas_w: int       # supersampled working canvas
    canvas_h: int
    content_h: int      # source height inside the working canvas
    fill_zoom: float    # zoompan z that turns fit into fill
    band_top_fill: int
    band_bottom_fill: int
    band_top_fit: int
    band_bottom_fit: int

    def band(self, mode: str) -> tuple[int, int]:
        """(top_y, bottom_y) of the visible footage for 'fill' or 'fit'."""
        if mode == "fill":
            return self.band_top_fill, self.band_bottom_fill
        return self.band_top_fit, self.band_bottom_fit

    def edges(self, modes: list[str] | tuple[str, ...]) -> list[int]:
        """Every horizontal video edge present in a clip using `modes`."""
        out: set[int] = set()
        for m in set(modes) or {"fill"}:
            t, b = self.band(m)
            out.update((t, b))
        return sorted(out)


def fill_geometry(rs: dict[str, Any], ss: dict[str, Any]) -> FillGeometry:
    """Derive the F1 foreground sizes from render + style config."""
    zf = ss["zoom_fill"]
    W, H = int(rs["width"]), int(rs["height"])
    scale = float(zf["fill_scale"])
    sup = max(1.0, float(zf["supersample"]))
    fit_h = _even(W * 9 / 16)
    fill_h = _even(fit_h * scale)
    canvas_w = _even(W * sup)
    canvas_h = _even(fill_h * sup)
    content_h = _even(canvas_w * 9 / 16)
    fill_zoom = canvas_h / content_h
    return FillGeometry(
        out_w=W,
        fit_h=fit_h,
        fill_h=fill_h,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
        content_h=content_h,
        fill_zoom=round(fill_zoom, 6),
        band_top_fill=(H - fill_h) // 2,
        band_bottom_fill=(H - fill_h) // 2 + fill_h,
        band_top_fit=(H - fill_h) // 2 + (fill_h - fit_h) // 2,
        band_bottom_fit=(H - fill_h) // 2 + (fill_h - fit_h) // 2 + fit_h,
    )


# --------------------------------------------------------------------------------------
# Safe-area placement after F1
# --------------------------------------------------------------------------------------

class SourceTextCollision(RuntimeError):
    """No clear place inside the safe area for our captions or credit: text burned into the source is
    there (pipeline.textdetect). The item goes to needs_review with reason source_text_collision."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


@dataclass
class Layout:
    """Where the text sits once F1 has changed the video band."""
    caption_center_y: int
    hook_center_y: int
    hook_line_pitch: float
    credit_x: int
    credit_y: int                      # baseline for \an1 (bottom-left anchor)
    caption_ink: tuple[int, int] = (0, 0)
    hook_ink: tuple[int, int] = (0, 0)
    credit_ink: tuple[int, int] = (0, 0)
    video_edges: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    progress_y: int = 0                # top of the C3 progress bar (0 = the canvas top edge)


def _clear_of_edges(y0: float, y1: float, edges: list[int], clearance: float) -> bool:
    """True when the ink box [y0, y1] does not sit on any video edge."""
    return all(y1 <= e - clearance or y0 >= e + clearance for e in edges)


def ink_half_height(cap_px: float, outline: float, shadow: float) -> float:
    """Half the rendered ink height of one all-caps line (fill + outline + shadow).

    Matches the measured libass ink box: cap/2 + outline + shadow.
    """
    return float(cap_px) / 2.0 + float(outline) + float(shadow)


def plan_layout(
    modes: list[str] | tuple[str, ...],
    hook_font_size: float,
    hook_lines: int,
    rs: dict[str, Any],
    ss: dict[str, Any],
    cap_height_px: Any,
    caption_scale: float = 1.0,
    text_box_px: Any = None,
    *,
    occupancy: Any = None,
    captions: bool = True,
    credit: bool = True,
    progress: bool = True,
    credit_width_px: float | None = None,
) -> Layout:
    """Place captions, the hook band and the credit tag so that, for every framing mode
    the clip uses, the ink stays inside the safe area and never sits on a video edge.

    `cap_height_px(font_size, font_path)` and `text_box_px(font_size, font_path)` (the
    full ascender-to-descender height, for the mixed-case credit tag) are injected from
    pipeline.rendercore to avoid an import cycle. `caption_scale` is the largest
    transient caption scale - M1's pop peaks at 115%, and that peak must respect the
    safe area too, not just the settled 100%.

    `occupancy` (pipeline.textdetect.Occupancy): text burned into the source, on the output canvas.
    When it covers the caption box, the captions move to the nearest clear centre inside the safe
    area that clears the video edges and the hook; the credit tag (left-aligned, `credit_width_px`
    wide) and the progress bar move the same way. `captions` / `credit` / `progress` say which of
    them this render draws. No clear place for the captions or the credit -> SourceTextCollision.
    Without occupancy (or with none of our boxes covered) the layout is exactly what it was.
    """
    sa = rs["safe_area"]
    cap, hk = rs["caption"], rs["hook"]
    geo = fill_geometry(rs, ss)
    clearance = float(ss["zoom_fill"]["edge_clearance_px"])
    edges = geo.edges(modes)
    notes: list[str] = []

    # --- captions: nearest valid centre to the configured center_y -------------------
    cap_half = ink_half_height(
        cap_height_px(cap["font_size"], rs["font_path"]) * max(1.0, float(caption_scale)),
        cap["outline"], cap["shadow"],
    )
    lo = int(round(sa["y_min"] + cap_half))
    hi = int(round(sa["y_max"] - cap_half))
    want = int(cap["center_y"])
    cap_y = None
    for c in sorted(range(lo, hi + 1), key=lambda c: (abs(c - want), c)):
        if _clear_of_edges(c - cap_half, c + cap_half, edges, clearance):
            cap_y = c
            break
    if cap_y is None:
        cap_y = max(lo, min(hi, want))
        notes.append(f"no caption centre clears every video edge by {clearance:.0f}px; kept y={cap_y}")
    elif cap_y != want:
        notes.append(f"captions moved {want} -> {cap_y} to clear the video edges {edges}")
    cap_ink = (int(round(cap_y - cap_half)), int(round(cap_y + cap_half)))

    # --- hook: centred in the room above the highest video edge ---------------------
    hook_cap = cap_height_px(hook_font_size, rs["font_path"])
    line_ink = hook_cap + 2.0 * float(hk["outline"]) + float(hk["shadow"])
    # Absolute outline/shadow must not eat the gap when the hook had to shrink to fit.
    pitch = max(hook_cap * float(hk.get("line_spacing", 1.75)), line_ink + float(hk.get("min_line_gap_px", 18)))
    line_half = ink_half_height(hook_cap, hk["outline"], hk["shadow"])
    n = max(1, int(hook_lines))
    block_half = pitch * (n - 1) / 2.0 + line_half
    top_limit = float(sa["y_min"])
    bottom_limit = min(edges) - clearance if edges else float(sa["y_max"])
    if bottom_limit - top_limit >= 2 * block_half:
        hook_y = int(round((top_limit + bottom_limit) / 2.0))
    else:
        hook_y = int(round((float(hk["band_top_y"]) + float(hk["band_bottom_y"])) / 2.0))
        notes.append(f"hook block ({2 * block_half:.0f}px) does not fit above y={bottom_limit:.0f}; kept band centre")
    hook_ink = (int(round(hook_y - block_half)), int(round(hook_y + block_half)))

    # --- source text (pipeline.textdetect): move the captions off it ------------------
    occ = occupancy if occupancy is not None and not getattr(occupancy, "empty", True) else None
    text_gap = float(ss["credit_tag"]["gap_from_caption_px"])
    x0_sa, x1_sa = float(sa["x_min"]), float(sa["x_max"])
    if occ is not None:
        hits = [b for b in getattr(occ, "boxes", [])][:6]
        if hook_lines and occ.occupied(x0_sa, hook_ink[0], x1_sa, hook_ink[1]):
            notes.append(f"source text under the hook band {hook_ink} (hook kept: no rule moves it) {hits}")
        if captions and occ.occupied(x0_sa, cap_ink[0], x1_sa, cap_ink[1]):
            def caption_ok(c: int) -> bool:
                y0, y1 = c - cap_half, c + cap_half
                return (_clear_of_edges(y0, y1, edges, clearance)
                        and (y1 + text_gap <= hook_ink[0] or y0 - text_gap >= hook_ink[1])
                        and not occ.occupied(x0_sa, y0, x1_sa, y1))
            new_y = next((c for c in sorted(range(lo, hi + 1), key=lambda c: (abs(c - cap_y), c)) if caption_ok(c)), None)
            if new_y is None:
                raise SourceTextCollision(
                    f"source_text_collision: no clear caption band in the safe area (source text at {hits})",
                    {"element": "captions", "boxes": hits, "caption_ink": cap_ink})
            notes.append(f"captions moved {cap_y} -> {new_y}: source text in the caption band {hits}")
            cap_y = new_y
            cap_ink = (int(round(cap_y - cap_half)), int(round(cap_y + cap_half)))

    # --- credit tag: bottom-left of the video band, never on the captions ----------
    ct = ss["credit_tag"]
    # "via <channel>" is mixed case: lower-case ascenders reach above the cap height and
    # 'g'/'y' hang below the baseline, so measuring it as all-caps under-reads the box by
    # ~20 px and the "never overlaps the captions" guarantee would not hold.
    credit_cap = cap_height_px(ct["font_size"], rs["font_path"])
    box = float(text_box_px(ct["font_size"], rs["font_path"])) if text_box_px else credit_cap * 1.4
    credit_h = box + 2.0 * float(ct["outline"]) + float(ct["shadow"])
    credit_half = credit_h / 2.0
    band_bottoms = [geo.band(m)[1] for m in (set(modes) or {"fill"})]
    y_max_allowed = float(sa["y_max"]) - 10.0
    gap = float(ct["gap_from_caption_px"])

    def overlaps_caption(bottom: float) -> bool:
        return not (bottom - credit_h >= cap_ink[1] + gap or bottom <= cap_ink[0] - gap)

    credit_y = min(band_bottoms) - float(ct["inset_y_px"])          # (a) inside every band
    if overlaps_caption(credit_y) or credit_y > y_max_allowed:
        below = cap_ink[1] + gap + credit_h                          # (b) just below the captions
        above = cap_ink[0] - gap                                     # (c) just above the captions
        if below <= y_max_allowed:
            credit_y = below
            notes.append(f"credit tag moved below the captions (y={credit_y:.0f})")
        elif above - credit_h >= sa["y_min"]:
            credit_y = above
            notes.append(f"credit tag moved above the captions (y={credit_y:.0f})")
        else:
            credit_y = y_max_allowed
            notes.append(f"credit tag clamped to the safe area (y={credit_y:.0f})")
    credit_y_i = int(round(min(credit_y, y_max_allowed)))
    # \an1 anchors the glyph origin, but the outline and shadow stick out to the left,
    # so inset by them or the ink crosses the safe-area edge.
    credit_x = int(round(sa["x_min"] + int(ct["inset_x_px"]) + float(ct["outline"]) + float(ct["shadow"])))
    credit_w = float(credit_width_px) if credit_width_px else (x1_sa - x0_sa) / 2.0
    credit_x1 = credit_x + credit_w

    # --- source text: move the credit tag off it (same rules as the captions) --------
    if occ is not None and credit and occ.occupied(credit_x, credit_y_i - credit_h, credit_x1, credit_y_i):
        def apart(y0: float, y1: float, ink: tuple[int, int], on: bool) -> bool:
            return not on or y1 + gap <= ink[0] or y0 - gap >= ink[1]

        def credit_ok(b: int) -> bool:
            y0 = b - credit_h
            return (y0 >= sa["y_min"] and _clear_of_edges(y0, b, edges, clearance)
                    and apart(y0, b, cap_ink, captions) and apart(y0, b, hook_ink, bool(hook_lines))
                    and not occ.occupied(credit_x, y0, credit_x1, b))
        lo_b, hi_b = int(math.ceil(sa["y_min"] + credit_h)), int(y_max_allowed)
        new_b = next((b for b in sorted(range(lo_b, hi_b + 1), key=lambda b: (abs(b - credit_y_i), b)) if credit_ok(b)), None)
        if new_b is None:
            raise SourceTextCollision(
                f"source_text_collision: no clear place for the credit tag (source text at {occ.boxes[:6]})",
                {"element": "credit", "boxes": occ.boxes[:6], "credit_ink": (credit_y_i - credit_h, credit_y_i)})
        notes.append(f"credit tag moved {credit_y_i} -> {new_b}: source text under it {occ.boxes[:6]}")
        credit_y_i = new_b

    # --- source text: the progress bar (top edge) moves down to the first clear row ---------
    progress_y = 0
    if occ is not None and progress:
        bar_h = float(ss["progress_bar"]["height_px"])
        W = float(rs["width"])
        if occ.occupied(0, 0, W, bar_h):
            new_p = next((y for y in range(0, int(sa["y_min"])) if not occ.occupied(0, y, W, y + bar_h)), None)
            if new_p is None:
                new_p = 0
                notes.append("progress bar kept at the top: source text in every row above the safe area")
            else:
                notes.append(f"progress bar moved 0 -> {new_p}: source text at the top edge")
            progress_y = int(new_p)

    return Layout(
        caption_center_y=cap_y,
        hook_center_y=hook_y,
        hook_line_pitch=round(pitch, 2),
        credit_x=credit_x,
        credit_y=credit_y_i,
        caption_ink=cap_ink,
        hook_ink=hook_ink,
        credit_ink=(int(round(credit_y_i - credit_h)), credit_y_i),
        video_edges=edges,
        notes=notes,
        progress_y=progress_y,
    )
