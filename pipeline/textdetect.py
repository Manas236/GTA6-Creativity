"""Text and graphics burned into the SOURCE, where our captions / credit / progress bar would go.

GamingBolt's chapter titles and Hazard's lower thirds sit at the bottom of their frames, exactly
where the zoom-to-fill framing puts our captions (11 Nov office preview). This module looks at the
cut the way the render will show it: ~8 frames sampled across the content, each framed with the same
F1 geometry the render uses (fill or fit, from the clip's segments), in OUTPUT coordinates (1080x1920).

Per frame, text lines are found with free OpenCV heuristics (no model, no API):
  1. morphological gradient -> binary edges (Otsu, floored at `edge_min`),
  2. a horizontal closing joins the letters of a line into one blob,
  3. a blob is a text line when it is flat (aspect >= `line_min_aspect`), `line_min_h`-`line_max_h`
     tall, and holds at least `min_chars` character-like components of similar height on one baseline.
A pixel is "source text" when a text line covered it in at least `min_frames` of the sampled frames:
a title card or a lower third stays put while the picture behind it changes; scenery does not.

`config.yaml -> render.source_text` holds every threshold. pipeline.style.plan_layout uses the result
to move our text off it (PLAN.md calls #184-#187).
"""
from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from . import style
from .rendercore import render_settings
from .style import SourceTextCollision  # noqa: F401 - re-exported: raised by style.plan_layout

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "samples": 8,               # frames sampled across the content (the first one at the start: B's freeze)
    "min_frames": 2,            # a pixel is source text when text covered it in >= this many samples
    "min_area_px": 2500,        # source-text pixels inside one of our boxes before it counts as occupied
    "margin_px": 10,            # our boxes are grown by this much before the test
    "edge_min": 48,             # binary edge threshold floor (0-255 gradient)
    "close_w_px": 21,           # horizontal closing that joins the letters and words of one line
    "line_min_h": 16,           # text line height range in output pixels
    "line_max_h": 140,
    "line_min_aspect": 2.2,     # width / height of a text line
    "min_chars": 3,             # character-like components inside a line
    "char_height_spread": 0.35, # their heights may differ by this fraction of the median
    "baseline_spread": 0.25,    # their bottoms may differ by this fraction of the median height
}


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    rs = render_settings(cfg)
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (rs.get("source_text") or {}).items() if v is not None})
    return out


@dataclass
class Occupancy:
    """Source text on the 1080x1920 output canvas."""
    mask: np.ndarray                                     # bool H x W
    frames: int
    min_area_px: int
    margin_px: int
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)   # (x0, y0, x1, y1) of each blob
    times: list[float] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not bool(self.mask.any())

    def area(self, x0: float, y0: float, x1: float, y1: float) -> int:
        """Source-text pixels inside the box (grown by margin_px)."""
        h, w = self.mask.shape
        m = self.margin_px
        a, b = max(0, int(math.floor(y0 - m))), min(h, int(math.ceil(y1 + m)))
        c, d = max(0, int(math.floor(x0 - m))), min(w, int(math.ceil(x1 + m)))
        if a >= b or c >= d:
            return 0
        return int(self.mask[a:b, c:d].sum())

    def occupied(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        return self.area(x0, y0, x1, y1) >= self.min_area_px

    def summary(self) -> dict[str, Any]:
        return {"frames": self.frames, "pixels": int(self.mask.sum()), "boxes": self.boxes[:12],
                "times": [round(t, 2) for t in self.times]}


# --------------------------------------------------------------------------------------
# Frames, framed like the render
# --------------------------------------------------------------------------------------

def _even(x: float) -> int:
    return max(2, int(round(float(x) / 2.0) * 2))


def framing_filter(mode: str, rs: dict[str, Any], ss: dict[str, Any], zoom_fill: bool = True) -> tuple[str, int, int]:
    """(ffmpeg filter producing the foreground layer as the render draws it, its height, its top y on
    the output canvas). Mirrors render._fg_chain for a constant zoom (1.0 = fit, fill_zoom = fill)."""
    W, H = int(rs["width"]), int(rs["height"])
    if not zoom_fill:
        fg_h = _even(W * 9 / 16)
        return f"scale={W}:{fg_h}:force_original_aspect_ratio=decrease,pad={W}:{fg_h}:(ow-iw)/2:(oh-ih)/2", fg_h, (H - fg_h) // 2
    geo = style.fill_geometry(rs, ss)
    z = geo.fill_zoom if mode == "fill" else 1.0
    chain = (f"scale={geo.canvas_w}:{geo.content_h}:force_original_aspect_ratio=decrease,"
             f"pad={geo.canvas_w}:{geo.canvas_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
             f"crop={_even(geo.canvas_w / z)}:{_even(geo.canvas_h / z)},scale={W}:{geo.fill_h}")
    return chain, geo.fill_h, (H - geo.fill_h) // 2


def output_frame(cut_path: Path | str, t: float, mode: str, rs: dict[str, Any], ss: dict[str, Any],
                 zoom_fill: bool = True) -> np.ndarray | None:
    """The cut's frame at `t` as a gray 1080x1920 canvas (zeros outside the foreground band)."""
    W, H = int(rs["width"]), int(rs["height"])
    chain, fg_h, top = framing_filter(mode, rs, ss, zoom_fill)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(cut_path),
           "-frames:v", "1", "-vf", f"{chain},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        log.warning("Frame at %.2fs of %s not read: %s", t, cut_path, e)
        return None
    if res.returncode != 0 or len(res.stdout) < W * fg_h:
        return None
    fg = np.frombuffer(res.stdout[: W * fg_h], dtype=np.uint8).reshape(fg_h, W)
    canvas = np.zeros((H, W), dtype=np.uint8)
    canvas[top:top + fg_h] = fg
    return canvas


# --------------------------------------------------------------------------------------
# Text lines in one frame
# --------------------------------------------------------------------------------------

def text_line_boxes(gray: np.ndarray, ts: dict[str, Any] | None = None) -> list[tuple[int, int, int, int]]:
    """Boxes (x0, y0, x1, y1) of horizontal text lines in a gray image."""
    import cv2

    ts = {**DEFAULTS, **dict(ts or {})}
    if gray is None or not gray.size:
        return []
    grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    otsu, _ = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    _, edges = cv2.threshold(grad, max(float(otsu), float(ts["edge_min"])), 255, cv2.THRESH_BINARY)
    min_h, max_h = int(ts["line_min_h"]), int(ts["line_max_h"])
    close_w = max(3, int(ts["close_w_px"]))
    joined = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (close_w, 3)))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    for i in range(1, n):
        x, y, w, h, _area = (int(v) for v in stats[i])
        if not (min_h <= h <= max_h) or w < float(ts["line_min_aspect"]) * h:
            continue
        if _is_text_line(edges[y:y + h, x:x + w], h, ts):
            boxes.append((x, y, x + w, y + h))
    return boxes


def _is_text_line(edges: np.ndarray, line_h: int, ts: dict[str, Any]) -> bool:
    """At least min_chars character-like components of similar height on one baseline."""
    import cv2

    n, _labels, stats, _ = cv2.connectedComponentsWithStats(edges, connectivity=8)
    chars = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if h < 0.35 * line_h or h > 1.05 * line_h or w > 1.6 * h or w < 2 or area < 6:
            continue
        chars.append((h, y + h))
    if len(chars) < int(ts["min_chars"]):
        return False
    hs = np.array([c[0] for c in chars], dtype=float)
    bottoms = np.array([c[1] for c in chars], dtype=float)
    med = float(np.median(hs))
    similar = np.abs(hs - med) <= float(ts["char_height_spread"]) * med
    if similar.sum() < int(ts["min_chars"]):
        return False
    base = np.median(bottoms[similar])
    aligned = np.abs(bottoms[similar] - base) <= float(ts["baseline_spread"]) * med
    return int(aligned.sum()) >= int(ts["min_chars"])


# --------------------------------------------------------------------------------------
# The clip
# --------------------------------------------------------------------------------------

def sample_times(end_s: float, n: int) -> list[float]:
    """n times across [0, end_s): the first at the very start (Variant B freezes that frame)."""
    n = max(1, int(n))
    end_s = max(0.1, float(end_s))
    return [round(min(end_s - 0.05, 0.05 + i * end_s / n), 3) for i in range(n)]


def mode_at(segments: Sequence[Any], t: float) -> str:
    for s in segments or []:
        if float(s.start) <= t < float(s.end):
            return str(s.mode)
    return str(segments[-1].mode) if segments else "fill"


def detect_source_text(
    cut_path: Path | str,
    end_s: float,
    segments: Sequence[Any] | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    zoom_fill: bool = True,
    frame_reader: Any = None,
) -> Occupancy | None:
    """Persistent source text on the output canvas for this cut, or None when switched off or
    unreadable. `segments` are the clip's F1 segments (cut timeline) so each sample is framed in the
    mode the render will use at that moment. `frame_reader(t, mode)` replaces ffmpeg in tests."""
    ts = settings(cfg)
    if not ts.get("enabled", True):
        return None
    rs = render_settings(cfg)
    ss = style.style_settings(cfg)
    W, H = int(rs["width"]), int(rs["height"])
    count = np.zeros((H, W), dtype=np.uint8)
    boxes_all: list[tuple[int, int, int, int]] = []
    used: list[float] = []
    for t in sample_times(end_s, int(ts["samples"])):
        mode = mode_at(segments or [], t) if zoom_fill else "fit"
        frame = frame_reader(t, mode) if frame_reader else output_frame(cut_path, t, mode, rs, ss, zoom_fill)
        if frame is None:
            continue
        used.append(t)
        layer = np.zeros((H, W), dtype=np.uint8)
        for x0, y0, x1, y1 in text_line_boxes(frame, ts):
            layer[y0:y1, x0:x1] = 1
            boxes_all.append((x0, y0, x1, y1))
        count += layer
    if not used:
        return None
    mask = count >= max(1, int(ts["min_frames"]))
    occ = Occupancy(mask, len(used), int(ts["min_area_px"]), int(ts["margin_px"]), _persistent_boxes(mask), used)
    if not occ.empty:
        log.info("Source text in %s: %s", Path(str(cut_path)).name, occ.summary())
    return occ


def _persistent_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    import cv2

    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    return [(int(s[0]), int(s[1]), int(s[0] + s[2]), int(s[1] + s[3])) for s in stats[1:]]
