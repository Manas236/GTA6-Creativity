"""Content gate: is a clip window real, MOVING GAME FOOTAGE? (replaces the scene-cut "static" floor)

The old static-source floor counted hard scene cuts across the whole source. That measures editing,
not motion: a Twitch clip of a streamer playing (one continuous camera, 0 cuts) was rejected as
"static", while a news video flipping between tweet screenshots (many cuts) passed. This module looks
at what is actually on screen, on the window that becomes the clip:

1. MOTION - frames are sampled every 1/`fps` s. For each consecutive pair the previous frame is
   first aligned to the current one with a global zoom/pan/rotate (OpenCV ECC, affine); the frame's
   motion is the share of pixels that STILL differ by more than `pixel_delta` grey levels. Removing
   the global transform is what makes a Ken Burns slideshow (a still slowly zoomed) read as static,
   while gameplay keeps its parallax and moving characters. A sample is "moving" when that share is
   >= `moving_frac`; the window needs `min_motion_share` of its samples moving. Gameplay moves almost
   all the time; a tweet screenshot, a still, a text card or a slideshow does not (a slideshow moves
   only at its transitions).

2. GAMEPLAY - every `clip_every_s` one of those frames goes through CLIP ViT-B/32 (OpenAI, MIT
   licence; the ONNX export Xenova/clip-vit-base-patch32, run with onnxruntime on the CPU - free,
   local, no API) as three square crops (left/centre/right; top/middle/bottom for a vertical
   source) so the screen is seen at a readable scale; its zero-shot probabilities over the prompts in
   PROMPTS are averaged over the crops and summed per group: P(game) = the "game" prompts' share. A frame is gameplay when P(game) >= `game_prob`; the window
   needs `min_game_share` of its frames to be gameplay. The non-game prompts name what must fail:
   tweet / social-post / article screenshots, text cards, a facecam on its own, game menus and
   loading screens, a desktop or browser, black frames.

Why CLIP and not OCR text coverage: OCR cannot see the two failures that have no text (a facecam on
its own, a still image), and real gameplay is full of text that must NOT fail it (Twitch chat
overlays, HUD, subtitles, sub alerts). CLIP judges the whole frame the way a person glancing at it
would. Text-heavy frames are covered by CLIP's screenshot / text-card prompts.

`scan()` decodes a span once and returns a per-sample timeline; `verdict()` judges any window of it,
so the cut stage scans the source once and judges each candidate window from the timeline.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from . import config

log = logging.getLogger(__name__)

MODEL_REPO = "Xenova/clip-vit-base-patch32"
MODEL_REVISION = "6b7c523703ae26e39be4b03774966f8571c7f523"
MODEL_FILES = ("onnx/vision_model.onnx", "onnx/text_model.onnx", "tokenizer.json")
SIZE = 224
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
LOGIT_SCALE = 100.0

# (group, label, prompt). Group "game" counts toward P(game); every other group is a way to fail.
# Chosen on 28 hand-labelled frames from the 19 Nov Make job, the two 9 Nov fallback-hook sources and
# two trailers (28/28 with three crops); generic wording on purpose. NOT in the list, on purpose:
# "a Twitch stream ... with a webcam in the corner" - every streamer layout has one, so it matched
# menus, tweets and facecams alike.
PROMPTS: tuple[tuple[str, str, str], ...] = (
    ("game", "gameplay", "a screenshot of a video game"),
    ("game", "gameplay", "a 3D video game scene with characters"),
    ("game", "gameplay", "a video game character driving a car"),
    ("game", "gameplay", "a dark indoor scene in a video game"),
    ("game", "gameplay", "a cinematic cutscene from a video game"),
    ("game", "gameplay", "a realistic video game city at night"),
    ("game", "gameplay", "a video game character in a room"),
    ("text", "tweet / social post", "a screenshot of a Twitter post with text"),
    ("text", "web page / article", "a web page full of text"),
    ("text", "web page / article", "a news article"),
    ("text", "text card", "white text on a black background"),
    ("text", "text card", "a title card with big text"),
    ("facecam", "facecam", "a close-up of a man's face talking into a microphone"),
    ("facecam", "facecam", "a man with headphones on a webcam"),
    ("menu", "game menu", "a menu screen with a list of options"),
    ("menu", "game menu", "a settings page with sliders and toggles"),
    ("menu", "game menu", "a video game main menu with the game's logo"),
    ("blank", "black screen", "a mostly black screen"),
    ("desktop", "desktop / browser", "a computer desktop with a web browser open"),
)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "fps": 2.0,                 # motion samples per second
    "pixel_delta": 6,           # grey-level change (0-255) left after global alignment that counts a pixel
    "moving_frac": 0.10,        # a sample moves when this share of its pixels still changed
    "min_motion_share": 0.60,   # window: share of samples that must be moving
    "clip_every_s": 1.0,        # one CLIP frame every this many seconds
    "game_prob": 0.50,          # a CLIP frame is gameplay when P(game) >= this
    "min_game_share": 0.60,     # window: share of CLIP frames that must be gameplay
    "source_min_game_share": 0.15,   # whole source: below this there is no window worth transcribing
    "source_min_motion_share": 0.15,
    "model_dir": "assets/models/clip",
}


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(DEFAULTS)
    out.update({k: v for k, v in ((cfg or {}).get("content_gate") or {}).items() if v is not None})
    return out


# --------------------------------------------------------------------------------------------
# CLIP (ONNX)
# --------------------------------------------------------------------------------------------

class _Clip:
    def __init__(self, model_dir: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.vision = ort.InferenceSession(str(model_dir / "onnx" / "vision_model.onnx"), opts,
                                           providers=["CPUExecutionProvider"])
        text = ort.InferenceSession(str(model_dir / "onnx" / "text_model.onnx"), opts,
                                    providers=["CPUExecutionProvider"])
        tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        embeds = []
        for _, _, prompt in PROMPTS:   # one at a time: the export takes no attention mask
            ids = np.array([tok.encode(prompt).ids], dtype=np.int64)
            embeds.append(text.run(None, {"input_ids": ids})[0][0])
        t = np.stack(embeds).astype(np.float32)
        self.text = t / np.linalg.norm(t, axis=1, keepdims=True)
        self.game_mask = np.array([g == "game" for g, _, _ in PROMPTS])

    def probs(self, frames: np.ndarray) -> np.ndarray:
        """(n, 224, 224, 3) uint8 RGB crops -> (n, len(PROMPTS)) softmax probabilities."""
        out = []
        for i in range(0, len(frames), 16):
            x = (frames[i:i + 16].astype(np.float32) / 255.0 - MEAN) / STD
            emb = self.vision.run(None, {"pixel_values": x.transpose(0, 3, 1, 2).copy()})[0]
            emb = emb / np.linalg.norm(emb, axis=1, keepdims=True)
            logits = LOGIT_SCALE * emb @ self.text.T
            logits -= logits.max(axis=1, keepdims=True)
            p = np.exp(logits)
            out.append(p / p.sum(axis=1, keepdims=True))
        return np.concatenate(out) if out else np.zeros((0, len(PROMPTS)), dtype=np.float32)


_MODEL: _Clip | None = None
_LOCK = threading.Lock()


def model_dir(cfg: dict[str, Any] | None = None) -> Path:
    p = Path(settings(cfg)["model_dir"])
    return p if p.is_absolute() else config.project_root() / p


def ensure_model(cfg: dict[str, Any] | None = None) -> Path:
    """Download the CLIP ONNX files once (~600 MB, Hugging Face, pinned revision)."""
    root = model_dir(cfg)
    missing = [f for f in MODEL_FILES if not (root / f).is_file()]
    if missing:
        from huggingface_hub import hf_hub_download
        log.info("Downloading CLIP (%s) for the content gate: %s", MODEL_REPO, ", ".join(missing))
        for f in missing:
            hf_hub_download(MODEL_REPO, f, revision=MODEL_REVISION, local_dir=str(root))
    return root


def get_model(cfg: dict[str, Any] | None = None) -> _Clip:
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            _MODEL = _Clip(ensure_model(cfg))
        return _MODEL


# --------------------------------------------------------------------------------------------
# Scan + verdict
# --------------------------------------------------------------------------------------------

@dataclass
class Sample:
    t: float                          # seconds on the SOURCE timeline
    motion: float | None = None       # share of pixels changed since the previous sample
    game: float | None = None         # CLIP P(game), only on CLIP frames
    label: str | None = None          # CLIP's top label on CLIP frames


@dataclass
class Timeline:
    start: float
    end: float
    samples: list[Sample] = field(default_factory=list)

    def window(self, start: float, end: float) -> list[Sample]:
        return [s for s in self.samples if start - 1e-6 <= s.t < end]


def _probe_size(video: Path | str) -> tuple[int, int]:
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, timeout=60)
    try:
        w, h = (int(x) for x in res.stdout.strip().split(",")[:2])
        return w, h
    except ValueError:
        return 1920, 1080


def _frames(video: Path | str, start: float, end: float | None, fps: float):
    """RGB frames scaled so the SHORT side is 224 (the aspect ratio is kept for the crops)."""
    w, h = _probe_size(video)
    if w >= h:
        fw, fh = max(SIZE, int(round(w * SIZE / h / 2)) * 2), SIZE
    else:
        fw, fh = SIZE, max(SIZE, int(round(h * SIZE / w / 2)) * 2)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{max(0.1, end - start):.3f}"]
    cmd += ["-i", str(video), "-an", "-vf", f"fps={fps},scale={fw}:{fh}:flags=area",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    size = fw * fh * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(fh, fw, 3)
    finally:
        proc.stdout.close()
        proc.wait(timeout=60)


def _crops(frame: np.ndarray) -> list[np.ndarray]:
    """Three SIZE x SIZE crops along the long side (left/centre/right or top/middle/bottom)."""
    h, w, _ = frame.shape
    if w >= h:
        return [frame[:SIZE, x:x + SIZE] for x in (0, (w - SIZE) // 2, w - SIZE)]
    return [frame[y:y + SIZE, :SIZE] for y in (0, (h - SIZE) // 2, h - SIZE)]


_MW, _MH = 160, 90


def _grey(frame: np.ndarray) -> np.ndarray:
    """Small blurred grey frame, contrast-stretched (2nd-98th percentile -> 0-255, at most ~10x) so a
    dark scene's motion counts like a bright one's (ohnePixel's red-lit club, dark leaked footage)."""
    import cv2
    g = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    g = cv2.resize(g, (_MW, _MH) if frame.shape[1] >= frame.shape[0] else (_MH, _MW),
                   interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(g, (5, 5), 0).astype(np.float32)
    lo, hi = np.percentile(g, 2), np.percentile(g, 98)
    return (g - lo) * (255.0 / max(float(hi - lo), 24.0))


def residual_motion(prev: np.ndarray, cur: np.ndarray, delta: float) -> float:
    """Share of pixels that still differ after aligning `prev` to `cur` with a global affine warp."""
    import cv2
    diff = np.abs(cur - prev) > delta
    if diff.mean() <= 0.02:            # nothing moved: no alignment needed
        return float(diff.mean())
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 40, 1e-4)
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(cur, prev, warp, cv2.MOTION_AFFINE, crit, None, 1)
    except cv2.error:                  # a hard cut or a blank frame: no global fit, count it raw
        return float(diff.mean())
    h, w = cur.shape
    aligned = cv2.warpAffine(prev, warp, (w, h), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                             borderMode=cv2.BORDER_REPLICATE)
    res = np.abs(cur - aligned) > delta
    res[:4, :] = False
    res[-4:, :] = False
    res[:, :4] = False
    res[:, -4:] = False
    return float(res.mean())


def scan(video: Path | str, start: float = 0.0, end: float | None = None,
         cfg: dict[str, Any] | None = None, model: Any = None) -> Timeline:
    """Decode [start, end) of `video` once: motion for every sample, CLIP for every clip_every_s."""
    gs = settings(cfg)
    fps = float(gs["fps"])
    every = max(1, int(round(float(gs["clip_every_s"]) * fps)))
    samples: list[Sample] = []
    clip_frames: list[np.ndarray] = []
    clip_idx: list[int] = []
    prev = None
    for i, frame in enumerate(_frames(video, start, end, fps)):
        s = Sample(round(start + i / fps, 3))
        g = _grey(frame)
        if prev is not None:
            s.motion = round(residual_motion(prev, g, float(gs["pixel_delta"])), 4)
        prev = g
        if i % every == 0:
            clip_frames.extend(_crops(frame))
            clip_idx.append(i)
        samples.append(s)
    if clip_frames:
        m = model or get_model(cfg)
        probs = m.probs(np.stack(clip_frames)).reshape(len(clip_idx), 3, -1).mean(axis=1)
        labels = [label for _, label, _ in PROMPTS]
        game_mask = np.array([g == "game" for g, _, _ in PROMPTS])
        for i, p in zip(clip_idx, probs):
            game = float(p[game_mask].sum())
            samples[i].game = round(game, 4)
            # on a non-game frame, name the non-game prompt that won (for the failure reason)
            pick = p if game >= 0.5 else np.where(game_mask, -1.0, p)
            samples[i].label = labels[int(np.argmax(pick))]
    stop = end if end is not None else (samples[-1].t + 1.0 / fps if samples else start)
    return Timeline(start, stop, samples)


def verdict(tl: Timeline, start: float | None = None, end: float | None = None,
            cfg: dict[str, Any] | None = None, *, scope: str = "window") -> dict[str, Any]:
    """Judge [start, end) of a scanned timeline. scope="source" uses the looser source floors."""
    gs = settings(cfg)
    start = tl.start if start is None else start
    end = tl.end if end is None else end
    win = tl.window(start, end)
    motions = [s.motion for s in win if s.motion is not None]
    games = [s for s in win if s.game is not None]
    motion_share = sum(m >= float(gs["moving_frac"]) for m in motions) / len(motions) if motions else 0.0
    game_share = sum(s.game >= float(gs["game_prob"]) for s in games) / len(games) if games else 0.0
    others: dict[str, int] = {}
    for s in games:
        if s.game < float(gs["game_prob"]) and s.label:
            others[s.label] = others.get(s.label, 0) + 1
    need_motion = float(gs["source_min_motion_share" if scope == "source" else "min_motion_share"])
    need_game = float(gs["source_min_game_share" if scope == "source" else "min_game_share"])
    reasons = []
    if motion_share < need_motion:
        reasons.append(f"static: {motion_share:.0%} of the {'source' if scope == 'source' else 'window'} "
                       f"moves (needs {need_motion:.0%})")
    if game_share < need_game:
        top = ", ".join(f"{k} {v}" for k, v in sorted(others.items(), key=lambda kv: -kv[1])[:3])
        reasons.append(f"not_gameplay: {game_share:.0%} gameplay frames (needs {need_game:.0%})"
                       + (f"; instead: {top}" if top else ""))
    return {
        "ok": not reasons,
        "start": round(start, 2), "end": round(end, 2),
        "motion_share": round(motion_share, 3), "game_share": round(game_share, 3),
        "median_motion": round(float(np.median(motions)), 4) if motions else 0.0,
        "mean_game_prob": round(float(np.mean([s.game for s in games])), 3) if games else 0.0,
        "samples": len(motions), "clip_frames": len(games),
        "non_game": others,
        "reason": "; ".join(reasons),
    }


def judge_span(video: Path | str, start: float = 0.0, end: float | None = None,
               cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """scan + verdict for one span (scripts and the proof run use this)."""
    tl = scan(video, start, end, cfg)
    return verdict(tl, start, end if end is not None else tl.end, cfg)


def timeline_summary(tl: Timeline, step_s: float = 5.0) -> list[dict[str, Any]]:
    """Per-step digest for logs and the cut selection record."""
    out = []
    t = tl.start
    while t < tl.end:
        v = verdict(tl, t, min(tl.end, t + step_s))
        out.append({"t": round(t, 1), "motion": v["motion_share"], "game": v["game_share"]})
        t += step_s
    return out


def windows_by_gameplay(tl: Timeline, starts_ends: Sequence[tuple[float, float]],
                        cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return [verdict(tl, a, b, cfg) for a, b in starts_ends]
