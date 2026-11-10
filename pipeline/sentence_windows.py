"""Sentence-aligned cut windows: start from where sentences END, not from fixed 20 s windows.

The old cut stage scored fixed ~20 s windows on audio/scene and then hunted for a clean sentence
end inside each one's 18-22 s end range; a fast talker (Hazard: 0 of 8 windows) failed every
time although the source had plenty of sentence ends. Now:

1. the source is transcribed once (cut.transcribe_source_span, current model + glossary);
2. every clean sentence end in it is found with the CURRENT rules (pipeline.endings.safe_spans:
   audio pause >= audio_gap_min_s after . ? !, or a measured quiet stretch >= pause_gap_s);
3. every end E gives one candidate window: the best-scoring start in [E - max_dur, E - min_dur],
   scored with the SAME audio/scene formula as before (window_score), plus a modest bonus
   (cut.sentence_windows.start_bonus) when the start sits right before a sentence begins;
4. candidates are tried best first, with cut-level dedupe on top (cut.run_cut_item).

Pure arithmetic over words / envelopes / profiles, so it is unit-testable without media.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import config, endings

DEFAULTS: dict[str, Any] = {
    "full_transcribe_max_s": 660.0,   # transcribe the whole source up to this length (discovery caps at 600)
    "region_count": 6,                # longer: only this many top-scoring regions...
    "region_margin_s": 10.0,          # ...each widened by this much on both sides
    "start_bonus": 0.05,              # added when the window starts right before a sentence (soft)
    "start_step_s": 0.5,              # start grid inside [E - max, E - min]
    "start_lead_s": 0.15,             # a sentence-start window begins this long before the first word
    "length_margin_s": 0.1,           # windows stop this short of max_duration: an encoded cut runs a few
                                      # ms long, and a 22.005 s cut fell outside render's 18-22 s range
}


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(DEFAULTS)
    out.update(((cfg.get("cut") or {}).get("sentence_windows") or {}))
    return out


# ---------------------------------------------------------------------------------------------
# The existing audio/scene window score, factored out
# ---------------------------------------------------------------------------------------------

class Scorer:
    """score(start, end) = audio_weight * mean-loudness(0..1) + scene_weight * min(1, cuts/5)
    - 0.05 if the window touches the first 5 % - 0.05 if it touches the last 5 % (the formula
    scan_cut_windows always used), with prefix sums so thousands of windows are cheap."""

    def __init__(self, total: float, loudness: Sequence[tuple[float, float]], scene_cuts: Sequence[float],
                 cfg: dict[str, Any] | None = None):
        cut_cfg = ((cfg or {}).get("cut") or {})
        w = cut_cfg.get("weights", {}) or {}
        self.audio_w = float(w.get("audio_energy", 0.75))
        self.scene_w = float(w.get("visual_interest", 0.25))
        self.total = float(total)
        pts = sorted((float(t), float(v)) for t, v in loudness)
        self.lt = [t for t, _ in pts]
        self.prefix = [0.0]
        for _, v in pts:
            self.prefix.append(self.prefix[-1] + v)
        self.cuts = sorted(float(c) for c in scene_cuts)

    def mean_lufs(self, a: float, b: float) -> float | None:
        i, j = bisect.bisect_left(self.lt, a), bisect.bisect_right(self.lt, b)
        return (self.prefix[j] - self.prefix[i]) / (j - i) if j > i else None

    def score(self, a: float, b: float) -> float:
        n_cuts = bisect.bisect_right(self.cuts, b) - bisect.bisect_left(self.cuts, a)
        norm_scene = min(1.0, n_cuts / 5.0)
        m = self.mean_lufs(a, b)
        norm_audio = 0.5 if m is None else max(0.0, min(1.0, (m + 50.0) / 40.0))
        penalty = (0.05 if a < 0.05 * self.total else 0.0) + (0.05 if b > 0.95 * self.total else 0.0)
        return self.audio_w * norm_audio + self.scene_w * norm_scene - penalty


# ---------------------------------------------------------------------------------------------
# Sentence ends and starts
# ---------------------------------------------------------------------------------------------

@dataclass
class Region:
    """A stretch of the source that was transcribed (the whole source, or a top-scoring part)."""
    start: float
    end: float
    words: list[dict[str, Any]] = field(default_factory=list)
    env: endings.Envelope | None = None


def clean_ends(region: Region, es: dict[str, Any]) -> list[tuple[float, endings.SafeSpan]]:
    """(end point, its span) for every clean sentence end in the region, by the current rules."""
    spans = endings.safe_spans(region.words, region.start, region.end, region.end, es, env=region.env)
    out: list[tuple[float, endings.SafeSpan]] = []
    for s in spans:
        if not math.isfinite(s.word_end):
            continue                       # the quiet before anyone speaks is not a sentence END
        p = s.point_for(s.start, es)       # the comfortable point nearest the sentence's last word
        p = min(max(round(p, 2), s.start), s.end)
        out.append((p, s))
    return out


def sentence_starts(region: Region, es: dict[str, Any], sw: dict[str, Any]) -> list[float]:
    """Points just before a sentence begins: after every clean break, `start_lead_s` before the
    next word (never before the break itself), plus the beginning of the region's first speech."""
    spans = endings.safe_spans(region.words, region.start, region.end, region.end, es, env=region.env)
    lead = float(sw["start_lead_s"])
    out: list[float] = []
    for s in spans:
        if math.isfinite(s.gap_end):
            out.append(round(max(s.start, s.gap_end - lead, region.start), 2))
    if region.start <= 0.0:
        out.append(0.0)                    # the very beginning of the source
    return sorted(set(out))


@dataclass
class Candidate:
    start: float
    end: float
    score: float                  # window score, bonus included
    at_sentence_start: bool
    reason: str                   # the end's rule: punct_audio | pause | punct (no audio)


def candidates(regions: Sequence[Region], scorer: Scorer, min_dur: float, max_dur: float,
               es: dict[str, Any], sw: dict[str, Any]) -> tuple[list[Candidate], int]:
    """(one candidate per clean sentence end that admits a min_dur..max_dur window, best first;
    the number of clean sentence ends found)."""
    step = max(0.05, float(sw["start_step_s"]))
    bonus = float(sw["start_bonus"])
    max_dur = float(max_dur) - float(sw.get("length_margin_s", 0.0))
    out: list[Candidate] = []
    n_ends = 0
    for region in regions:
        ends = clean_ends(region, es)
        n_ends += len(ends)
        starts_at_sentence = sentence_starts(region, es, sw)
        for end, span in ends:
            lo, hi = max(region.start, end - max_dur), end - min_dur
            if hi < lo - 1e-9:
                continue
            grid = [round(lo + k * step, 2) for k in range(int((hi - lo) / step + 1e-9) + 1)]
            pool = set(grid) | {hi} | {s for s in starts_at_sentence if lo - 1e-9 <= s <= hi + 1e-9}
            pool |= {c for c in scorer.cuts if lo <= c <= hi}          # the old scene-cut snap
            best: Candidate | None = None
            for st in sorted(pool):
                at_start = any(abs(st - s) <= 1e-6 for s in starts_at_sentence)
                sc = scorer.score(st, end) + (bonus if at_start else 0.0)
                if best is None or sc > best.score + 1e-12:
                    best = Candidate(round(st, 2), end, round(sc, 6), at_start, span.reason)
            if best:
                out.append(best)
    out.sort(key=lambda c: (-c.score, c.start))
    return out, n_ends


# ---------------------------------------------------------------------------------------------
# What to transcribe
# ---------------------------------------------------------------------------------------------

def transcribe_regions(total: float, ranked_windows: Sequence[Any], min_dur: float, max_dur: float,
                       tail: float, sw: dict[str, Any]) -> list[tuple[float, float]]:
    """The whole source when it is at most full_transcribe_max_s long; otherwise the top
    `region_count` non-overlapping scored windows, each widened by `region_margin_s` (plus the
    max length and the tail, so their ends can be checked), merged where they touch."""
    total = float(total)
    if total <= float(sw["full_transcribe_max_s"]):
        return [(0.0, total)]
    margin = float(sw["region_margin_s"])
    picked: list[float] = []
    for w in ranked_windows:
        if all(abs(float(w.start) - p) >= max_dur for p in picked):
            picked.append(float(w.start))
        if len(picked) >= int(sw["region_count"]):
            break
    spans = sorted((max(0.0, p - margin), min(total, p + max_dur + tail + margin)) for p in picked)
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(round(a, 2), round(b, 2)) for a, b in merged]
