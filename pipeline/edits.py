"""Timeline analysis for the style layer: framing segments, punch-ins, dead air, loop end.

Everything here is *planning*: it reads a clip with ffmpeg/ffprobe and returns plain
dataclasses plus the zoom program. Nothing writes a file and nothing builds a full
filtergraph (pipeline.render owns the stream labels), so each decision is unit-testable
with synthetic numbers and no network.

  F1  plan_segments      split at scene cuts, decide fill (moving) vs fit (static/text)
  M3  plan_punch_ins     loudness peaks / scene cuts, >= min_gap apart
  M4  slow_push          folded into the zoom program for fit segments
  A3  plan_dead_air      drop quiet + motionless stretches, remap times via Timeline
  A4  choose_loop_end    end frame whose dHash is closest to frame 1
"""
from __future__ import annotations

import logging
import math
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import endings
from .dedupe import dhash_from_raw_gray_9x8, hamming_distance

log = logging.getLogger(__name__)

_BIG = 1.0e6  # stands in for +inf in ffmpeg expressions


# --------------------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------------------

def motion_profile(video_path: Path | str, step_s: float = 0.2) -> list[tuple[float, float]]:
    """Frame-difference score every `step_s` seconds: [(t, scene_score 0..1)].

    Sampling at 1/step fps and reading libavfilter's scene score gives the mean visual
    change over each step, which is what the F1 static/moving decision needs.
    """
    fps = max(0.1, 1.0 / max(1e-3, float(step_s)))
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-an",
        "-vf", f"fps={fps:.4f},scale=160:90,select='gte(scene\\,0)',metadata=print:key=lavfi.scene_score",
        "-f", "null", "-",
    ]
    out: list[tuple[float, float]] = []
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except Exception as e:
        log.warning("Motion profile failed for %s: %s", video_path, e)
        return out
    t: float | None = None
    for line in res.stderr.splitlines():
        m = re.search(r"pts_time:(-?\d+\.?\d*)", line)
        if m:
            t = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=(-?\d+\.?\d*(?:e-?\d+)?)", line)
        if m and t is not None:
            out.append((t, max(0.0, float(m.group(1)))))
            t = None
    return out


def _profile_mean(profile: Sequence[tuple[float, float]], start: float, end: float, default: float) -> float:
    vals = [v for t, v in profile if start <= t < end]
    if not vals and profile:
        vals = [min(profile, key=lambda p: abs(p[0] - (start + end) / 2.0))[1]]
    return sum(vals) / len(vals) if vals else default


def _profile_at(profile: Sequence[tuple[float, float]], t: float, default: float) -> float:
    if not profile:
        return default
    return min(profile, key=lambda p: abs(p[0] - t))[1]


def gray_hashes(video_path: Path | str, fps: float = 5.0) -> list[tuple[float, int]]:
    """dHash of every frame sampled at `fps`: [(t, hash)] with t = index / fps."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video_path), "-an",
        "-vf", f"fps={fps:g},scale=9:8",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    except Exception as e:
        log.warning("Frame hash extraction failed for %s: %s", video_path, e)
        return []
    out: list[tuple[float, int]] = []
    for i in range(len(raw) // 72):
        out.append((i / float(fps), dhash_from_raw_gray_9x8(raw[i * 72:(i + 1) * 72])))
    return out


# --------------------------------------------------------------------------------------
# F1 framing segments
# --------------------------------------------------------------------------------------

@dataclass
class Segment:
    start: float
    end: float
    mode: str          # "fill" (moving gameplay) | "fit" (static / text-heavy)
    motion: float = 0.0

    @property
    def duration(self) -> float:
        return self.end - self.start


def plan_segments(
    duration: float,
    scene_cuts: Sequence[float],
    motion: Sequence[tuple[float, float]],
    ss: dict[str, Any],
    force_mode: str | None = None,
) -> list[Segment]:
    """Split at scene cuts and label each segment fill (moving) or fit (static/text-heavy).

    A segment whose mean frame-difference is below `motion_threshold` (a tweet
    screenshot, a title card, a held frame) keeps the full-width fit; everything else
    gets the zoom-to-fill. Adjacent same-mode segments are merged.
    """
    zf = ss["zoom_fill"]
    min_seg = float(zf["min_segment_s"])
    thresh = float(zf["motion_threshold"])
    duration = max(0.0, float(duration))
    if duration <= 0:
        return []
    if force_mode:
        return [Segment(0.0, duration, force_mode, _profile_mean(motion, 0.0, duration, thresh))]

    bounds = [0.0]
    for c in sorted(float(c) for c in scene_cuts):
        if bounds[-1] + min_seg <= c <= duration - min_seg:
            bounds.append(c)
    bounds.append(duration)

    segs: list[Segment] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= 0:
            continue
        m = _profile_mean(motion, a, b, thresh)
        segs.append(Segment(round(a, 3), round(b, 3), "fit" if m < thresh else "fill", round(m, 5)))

    merged: list[Segment] = []
    for s in segs:
        if merged and merged[-1].mode == s.mode:
            prev = merged[-1]
            total = prev.duration + s.duration
            prev.motion = round((prev.motion * prev.duration + s.motion * s.duration) / total, 5)
            prev.end = s.end
        else:
            merged.append(s)
    return merged


def segment_modes(segments: Sequence[Segment]) -> list[str]:
    """The distinct framing modes a clip uses (what the safe-area layout must satisfy)."""
    return sorted({s.mode for s in segments}) or ["fill"]


# --------------------------------------------------------------------------------------
# A3 dead-air removal
# --------------------------------------------------------------------------------------

@dataclass
class Timeline:
    """Source spans kept after dead-air removal, and the source -> output time map."""
    kept: list[tuple[float, float]]
    source_duration: float
    dropped: list[tuple[float, float]] = field(default_factory=list)

    @property
    def total(self) -> float:
        return round(sum(e - s for s, e in self.kept), 6)

    @property
    def is_identity(self) -> bool:
        return len(self.kept) == 1 and abs(self.kept[0][0]) < 1e-9 and abs(self.kept[0][1] - self.source_duration) < 1e-6

    def remap(self, t: float) -> tuple[float, bool]:
        """Map a source time to output time. Returns (out_t, inside_a_kept_span)."""
        t = float(t)
        acc = 0.0
        for s, e in self.kept:
            if t < s:
                return round(acc, 6), False      # inside a dropped span -> next kept start
            if t <= e:
                return round(acc + (t - s), 6), True
            acc += e - s
        return round(acc, 6), False

    def remap_words(self, words: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remap caption word timings, dropping words that fell inside a cut span."""
        out: list[dict[str, Any]] = []
        for w in words:
            s, s_in = self.remap(float(w["start"]))
            e, e_in = self.remap(float(w["end"]))
            if not (s_in or e_in):
                continue
            if e <= s:
                e = s + 0.08
            out.append({**w, "start": round(s, 3), "end": round(e, 3)})
        return out

    @classmethod
    def identity(cls, duration: float) -> "Timeline":
        return cls(kept=[(0.0, float(duration))], source_duration=float(duration))


def plan_dead_air(
    duration: float,
    loudness: Sequence[tuple[float, float]],
    motion: Sequence[tuple[float, float]],
    ss: dict[str, Any],
    grid_s: float = 0.1,
    words: Sequence[dict[str, Any]] | None = None,
    es: dict[str, Any] | None = None,
) -> Timeline:
    """Drop stretches longer than `min_silence_s` that are BOTH quiet and motionless.

    Longest candidates go first and dropping stops as soon as the remaining length would
    fall to `min_output_s`, so a sparse clip never shrinks below the floor.

    With `words`, a stretch is only dropped when it lies in a sentence break (or before
    the first / after the last word) and touches no word: silence inside a sentence is
    kept, even when the loudness meter calls it dead air.
    """
    da = ss["dead_air"]
    duration = float(duration)
    if duration <= 0 or not loudness:
        return Timeline.identity(duration)

    lufs_max = float(da["loudness_threshold_lufs"])
    motion_max = float(da["motion_threshold"])
    pad = float(da["keep_pad_s"])
    min_sil = float(da["min_silence_s"])
    floor = float(da["min_output_s"])

    # ebur128 reports momentary loudness for the 400 ms window ENDING at t, so every
    # sample lags the audio by half a window. Shift it back onto the window centre,
    # otherwise a cut lands ~0.2 s late and clips the start of the next sound.
    lag = float(da.get("loudness_lag_s", 0.2))
    if lag:
        loudness = [(max(0.0, t - lag), v) for t, v in loudness]

    n = max(1, int(math.ceil(duration / grid_s)))
    runs: list[tuple[float, float]] = []
    run_start: float | None = None
    for i in range(n):
        t = i * grid_s
        quiet = _profile_at(loudness, t, 0.0) < lufs_max
        still = _profile_at(motion, t, 1.0) < motion_max
        if quiet and still:
            if run_start is None:
                run_start = t
        elif run_start is not None:
            runs.append((run_start, t))
            run_start = None
    if run_start is not None:
        runs.append((run_start, duration))

    candidates: list[tuple[float, float]] = []
    kept_in_sentence = 0
    for s, e in runs:
        s2, e2 = s + pad, e - pad
        if e2 - s2 >= min_sil:
            if words and not endings.gap_is_sentence_break(s2, e2, words, es):
                kept_in_sentence += 1
                continue
            candidates.append((round(s2, 3), round(e2, 3)))
    if kept_in_sentence:
        log.info("A3 kept %d quiet stretch(es) that sit inside a sentence", kept_in_sentence)

    chosen: list[tuple[float, float]] = []
    remaining = duration
    for s, e in sorted(candidates, key=lambda c: c[1] - c[0], reverse=True):
        length = e - s
        if remaining - length < floor:
            continue
        chosen.append((s, e))
        remaining -= length
    chosen.sort()

    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in chosen:
        if s > cursor:
            kept.append((round(cursor, 3), round(s, 3)))
        cursor = e
    if cursor < duration:
        kept.append((round(cursor, 3), round(duration, 3)))
    if not kept:
        return Timeline.identity(duration)
    return Timeline(kept=kept, source_duration=duration, dropped=chosen)


def shift_segments(segments: Sequence[Segment], timeline: Timeline, offset: float = 0.0) -> list[Segment]:
    """Move framing segments from the source timeline onto the output timeline."""
    out: list[Segment] = []
    for s in segments:
        a, _ = timeline.remap(s.start)
        b, _ = timeline.remap(s.end)
        a, b = a + offset, b + offset
        if b - a <= 1e-6:
            continue
        if out and out[-1].mode == s.mode and abs(out[-1].end - a) < 1e-6:
            out[-1].end = round(b, 3)
            continue
        out.append(Segment(round(a, 3), round(b, 3), s.mode, s.motion))
    return out


# --------------------------------------------------------------------------------------
# M3 punch-ins
# --------------------------------------------------------------------------------------

@dataclass
class Punch:
    at: float
    hold: float
    ease_in: float
    ease_out: float
    reason: str = "peak"
    rank: int = 0          # 0 = the strongest punch-in of the clip (A1 offers whooshes in this order)

    @property
    def end(self) -> float:
        return self.at + self.ease_in + self.hold + self.ease_out


def plan_punch_ins(
    duration: float,
    loudness: Sequence[tuple[float, float]],
    scene_cuts: Sequence[float],
    ss: dict[str, Any],
    offset: float = 0.0,
) -> list[Punch]:
    """Pick punch-in times: the loudest moments and scene cuts, >= min_gap_s apart.

    Times are on the output timeline (`offset` is the Variant B freeze).
    """
    pz = ss["punch_ins"]
    ease_in, hold, ease_out = float(pz["ease_in_s"]), float(pz["hold_s"]), float(pz["ease_out_s"])
    gap = float(pz["min_gap_s"])
    first = float(pz["first_at_least_s"])
    span = ease_in + hold + ease_out
    last_allowed = float(duration) - span - 0.1
    if last_allowed <= first:
        return []

    cands: list[tuple[float, float, str]] = []   # (score, t, reason)
    if loudness:
        pts = sorted(loudness)
        for i, (t, v) in enumerate(pts):
            lo = max(0, i - 2)
            if v >= max(x[1] for x in pts[lo:i + 3]):
                cands.append((v, t, "peak"))
    for c in sorted(float(c) for c in scene_cuts):
        cands.append((_profile_at(loudness, c, -20.0) - 0.5, c, "scene_cut"))

    taken: list[Punch] = []
    for score, t, reason in sorted(cands, key=lambda c: (-c[0], c[1])):
        if not (first <= t <= last_allowed):
            continue
        # p.at is on the output timeline (+offset); compare like with like.
        if any(abs((t + offset) - p.at) < gap for p in taken):
            continue
        taken.append(Punch(round(t + offset, 3), hold, ease_in, ease_out, reason, rank=len(taken)))
        if len(taken) >= int(pz["max_count"]):
            break
    taken.sort(key=lambda p: p.at)
    return taken


# --------------------------------------------------------------------------------------
# The zoom program (F1 base + M4 push + M3 punches), as both a float function
# and an ffmpeg zoompan expression
# --------------------------------------------------------------------------------------

@dataclass
class ZoomPiece:
    """A half-open [start, end) piece of the base zoom: z = a + b * (t - start)."""
    start: float
    end: float
    a: float
    b: float


@dataclass
class ZoomProgram:
    pieces: list[ZoomPiece]
    punches: list[Punch]
    punch_gain: float = 0.0

    @property
    def is_constant(self) -> bool:
        return (
            not self.punches
            and all(abs(p.b) < 1e-9 for p in self.pieces)
            and len({round(p.a, 6) for p in self.pieces}) <= 1
        )

    @property
    def constant_value(self) -> float:
        return round(self.pieces[0].a, 6) if self.pieces else 1.0


def _base_at(pieces: Sequence[ZoomPiece], t: float) -> float:
    for p in pieces:
        if p.start <= t < p.end:
            return p.a + p.b * (t - p.start)
    return pieces[-1].a + pieces[-1].b * (pieces[-1].end - pieces[-1].start) if pieces else 1.0


def _punch_env(p: Punch, t: float) -> float:
    if t < p.at or t >= p.end:
        return 0.0
    x = t - p.at
    if x < p.ease_in:
        return 0.5 - 0.5 * math.cos(math.pi * x / p.ease_in)
    if x < p.ease_in + p.hold:
        return 1.0
    return 0.5 + 0.5 * math.cos(math.pi * (x - p.ease_in - p.hold) / p.ease_out)


def zoom_value(prog: ZoomProgram, t: float) -> float:
    """The zoom factor at output time `t` (the Python mirror of the ffmpeg expression)."""
    env = sum(_punch_env(p, t) for p in prog.punches)
    return max(1.0, _base_at(prog.pieces, t) * (1.0 + prog.punch_gain * min(1.0, env)))


def build_zoom_program(
    segments: Sequence[Segment],
    punches: Sequence[Punch],
    fill_zoom: float,
    ss: dict[str, Any],
    *,
    zoom_fill: bool = True,
    slow_push: bool = False,
    punch_ins: bool = False,
    total_s: float | None = None,
) -> ZoomProgram:
    """Combine F1 per-segment framing, the M4 push and the M3 punches into one program."""
    zf = ss["zoom_fill"]
    cf = max(0.0, float(zf["crossfade_s"]))
    push_to = float(ss["slow_push"]["zoom_to"])
    segs = list(segments)
    if not segs:
        segs = [Segment(0.0, float(total_s or 1.0), "fill" if zoom_fill else "fit")]

    def target(seg: Segment) -> tuple[float, float]:
        """(zoom at segment start, zoom at segment end)."""
        if seg.mode == "fill" and zoom_fill:
            return fill_zoom, fill_zoom
        if slow_push:
            return 1.0, push_to
        return 1.0, 1.0

    pieces: list[ZoomPiece] = []
    for i, seg in enumerate(segs):
        z0, z1 = target(seg)
        half_in = cf / 2.0 if i > 0 else 0.0
        half_out = cf / 2.0 if i < len(segs) - 1 else 0.0
        body_start = seg.start + half_in
        body_end = max(body_start + 1e-3, seg.end - half_out)
        span = max(1e-6, seg.end - seg.start)
        a = z0 + (z1 - z0) * (half_in / span)
        b = (z1 - z0) / span
        pieces.append(ZoomPiece(round(body_start, 4), round(body_end, 4), round(a, 6), round(b, 8)))
        if half_out > 0 and i < len(segs) - 1:
            nz0, _ = target(segs[i + 1])
            from_z = z0 + (z1 - z0) * ((body_end - seg.start) / span)
            pieces.append(ZoomPiece(
                round(body_end, 4), round(seg.end + half_out, 4),
                round(from_z, 6), round((nz0 - from_z) / max(1e-6, cf), 8),
            ))
    if pieces:
        pieces[0].start = 0.0
        pieces[-1].end = _BIG
    gain = (float(ss["punch_ins"]["zoom"]) - 1.0) if punch_ins else 0.0
    return ZoomProgram(pieces=pieces, punches=list(punches) if punch_ins else [], punch_gain=round(gain, 6))


def zoom_expr(prog: ZoomProgram, var: str = "it") -> str:
    """Render the zoom program as a zoompan `z` expression (half-open gated pieces)."""
    if prog.is_constant:
        return f"{prog.constant_value:.6f}"
    base_terms = [
        f"gte({var},{p.start:.4f})*lt({var},{p.end:.4f})*({p.a:.6f}{p.b:+.8f}*({var}-{p.start:.4f}))"
        for p in prog.pieces
    ]
    base = "+".join(base_terms) if base_terms else "1"
    if not prog.punches or prog.punch_gain <= 0:
        return f"max(1,{base})"
    envs = []
    for p in prog.punches:
        t1, t2 = p.at + p.ease_in, p.at + p.ease_in + p.hold
        envs.append(
            f"gte({var},{p.at:.4f})*lt({var},{t1:.4f})*(0.5-0.5*cos(PI*({var}-{p.at:.4f})/{p.ease_in:.4f}))"
            f"+gte({var},{t1:.4f})*lt({var},{t2:.4f})"
            f"+gte({var},{t2:.4f})*lt({var},{p.end:.4f})*(0.5+0.5*cos(PI*({var}-{t2:.4f})/{p.ease_out:.4f}))"
        )
    env = "+".join(f"({e})" for e in envs)
    return f"max(1,({base})*(1+{prog.punch_gain:.6f}*min(1,{env})))"


# --------------------------------------------------------------------------------------
# A4 loop-friendly ending
# --------------------------------------------------------------------------------------

@dataclass
class LoopEnd:
    end_s: float
    distance: int
    score: float
    in_pause: bool
    candidates: int


def _in_speech_pause(t: float, words: Sequence[dict[str, Any]], margin: float = 0.12) -> bool:
    return not any(float(w["start"]) - margin <= t <= float(w["end"]) + margin for w in words)


def choose_loop_end(
    hashes: Sequence[tuple[float, int]],
    min_end: float,
    max_end: float,
    words: Sequence[dict[str, Any]],
    ss: dict[str, Any],
    proposed_end: float | None = None,
    safe_spans: Sequence[endings.SafeSpan] | None = None,
    es: dict[str, Any] | None = None,
) -> LoopEnd | None:
    """Pick the end frame whose dHash is closest to frame 1, preferring a speech pause.

    `hashes` comes from gray_hashes(); index 0 is frame 1 (the loop reference).

    With `safe_spans` (the clip has speech) only ends inside a span are candidates, so A4
    can never move the end into a word or into an unfinished sentence. A span too narrow
    to hold a sampled frame is still a candidate: its preferred point is scored with the
    nearest sampled frame's hash (at most half a search step away).
    """
    le = ss["loop_end"]
    if not hashes or max_end <= min_end:
        return None
    ref = hashes[0][1]
    bonus = float(le["pause_bonus_bits"])
    step = float(le["search_step_s"])
    target = float(proposed_end if proposed_end is not None else max_end)

    cands: list[tuple[float, int, bool]] = []       # (t, hash, in_pause)
    if safe_spans is None:
        last_t = -1.0
        for t, h in hashes:
            if not (min_end <= t <= max_end):
                continue
            if t - last_t < step - 1e-6:
                continue
            last_t = t
            cands.append((t, h, _in_speech_pause(t, words)))
    else:
        es = es or endings.DEFAULTS
        for span in safe_spans:
            inside = [(t, h) for t, h in hashes if span.contains(t) and min_end <= t <= max_end]
            if inside:
                last_t = -1.0
                for t, h in inside:
                    if t - last_t < step - 1e-6:
                        continue
                    last_t = t
                    cands.append((t, h, True))
                continue
            p = span.point_for(target, es)
            if not (min_end - 1e-6 <= p <= max_end + 1e-6):
                continue
            t_near, h_near = min(hashes, key=lambda th: abs(th[0] - p))
            if abs(t_near - p) <= step / 2.0 + 1e-6:
                cands.append((p, h_near, True))

    best: LoopEnd | None = None
    for t, h, pause in cands:
        dist = hamming_distance(ref, h)
        score = dist - (bonus if pause else 0.0)
        if best is None or (score, abs(t - target)) < (best.score, abs(best.end_s - target)):
            best = LoopEnd(round(t, 3), dist, round(score, 3), pause, 0)
    if best:
        best.candidates = len(cands)
    return best
