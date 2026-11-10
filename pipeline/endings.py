"""Speech-safe endings: a clip may only end after a completed sentence.

One definition, used by every stage that picks or moves an end (the cut stage, the
render-stage end snap, A4's loop end) and by A3's dead-air removal:

A *sentence break* follows word i when the gap to word i+1 (or, for the last word, to the
end of the transcribed audio) is either
  * >= ``punct_gap_s`` and word i ends in ``.``, ``?`` or ``!`` (closing quotes/brackets
    stripped; an ellipsis or a known abbreviation does not count), or
  * >= ``pause_gap_s``, whatever word i is, or
  * word i ends in ``.``, ``?`` or ``!`` and the AUDIO pauses right after it (at least
    ``audio_gap_min_s`` at ``audio_gap_drop_db`` under the speech level).

When the audio is available (it always is in the cut and render stages) the punctuation case
is decided by the AUDIO alone, and the end sits in the middle of the earliest measured pause
after the word. Whisper's word timestamps are not precise enough for short gaps, in both
directions: base.en reported "so." -> "The" 0.00 s apart over a real 0.16 s pause, and it
reported a >= 0.15 s gap after "online." where the audio shows the speaker running straight
into "So" (+2 dB at the cut point - the one mid-sentence end the first verification found).
The "gap >= ``pause_gap_s``" case is measured in the audio too (rule 4): a stretch at least
``pause_gap_s`` long that is ``pause_drop_db`` under the speaker. Whisper's word ends moved by
~0.3 s between two transcriptions of the same audio; it reported 0.63 s after "download," where
the real pause was 0.32 s (after a comma - not a sentence end), and an end placed "0.07 s after"
that word by whisper's clock was +7 dB loud. Whisper's own gaps only count when it hears nobody
for at least ``no_speech_gap_s`` (music or gameplay with no speech under it). Every end, in
every stage, is therefore in audio quieter than the speech.

Its *safe span* is ``[word_end + min_after_word_s, next_start - next_word_margin_s]``:
an end anywhere inside sits in silence after a finished sentence, never inside a word.
A word gap that is not a sentence break is never a safe end, however long a pause a
loudness meter sees there.

Every number lives in ``config.yaml -> cut.ending``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from . import config

DEFAULTS: dict[str, Any] = {
    "sentence_punct": ".?!",
    "punct_gap_s": 0.15,          # . ? ! followed by at least this much silence ends a sentence
    "pause_gap_s": 0.6,           # any word gap this long ends a sentence
    "min_after_word_s": 0.07,     # the end is never closer than this to the last word
    "next_word_margin_s": 0.07,   # ... nor closer than this to the next word
    "end_after_word_s": 0.35,     # in a long gap, keep at least this much room on both sides
    "tail_s": 2.0,                # cut stage: transcribe this far past the window's max end
    "max_windows": 8,             # cut stage: scored windows to try before no_clean_ending
    "audio_gap_drop_db": 12.0,    # a measured pause is this far under the speech level...
    "audio_gap_min_s": 0.06,      # ...for at least this long
    "audio_gap_search_s": 0.4,    # looked for up to this far after whisper's word end
    "pause_drop_db": 8.0,         # an end in a long pause must be at least this far under speech
    "no_speech_gap_s": 2.0,       # whisper hearing nobody this long counts without a level test
    "abbreviations": ["mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "etc.", "jr.", "sr.", "e.g.", "i.e."],
}

_CLOSERS = "\"')]}”’"
_EPS = 1e-6


# Bumped whenever the rule changes in a way that can invalidate an end the cut stage already
# verified: render only trusts a cut-stage verdict made under the current rule.
RULE_VERSION = 4


class NoCleanEnding(RuntimeError):
    """Raised when speech runs through every allowed end point."""


@dataclass(frozen=True)
class SafeSpan:
    start: float          # earliest safe end
    end: float            # latest safe end (math.inf when nothing follows in the known audio)
    word_end: float       # end of the completed sentence's last word (-inf: before any speech)
    gap_end: float        # start of the next word (math.inf when none is known)
    reason: str           # "punct" | "pause" | "punct_audio" | "verified"

    def contains(self, t: float, tol: float = 1e-6) -> bool:
        return self.start - tol <= t <= self.end + tol

    def point_for(self, target: float, es: dict[str, Any]) -> float:
        """Where to place an end in this span, as close to `target` as is comfortable.

        In a long gap the end may move toward the target but keeps `end_after_word_s`
        of room on both sides; in a short gap it sits in the middle of the safe span.
        """
        if self.reason == "verified":
            # An earlier stage already placed this end (the cut's own end): keep it.
            return min(max(target, self.start), self.end)
        if self.reason == "punct_audio":
            return (self.start + self.end) / 2.0          # the middle of the measured pause
        room = float(es["end_after_word_s"])
        inner_lo = self.word_end + room if math.isfinite(self.word_end) else self.start
        inner_hi = self.gap_end - room if math.isfinite(self.gap_end) else self.end
        inner_lo, inner_hi = max(inner_lo, self.start), min(inner_hi, self.end)
        if inner_lo <= inner_hi:
            return min(max(target, inner_lo), inner_hi)
        hi = self.end if math.isfinite(self.end) else self.start
        return (self.start + hi) / 2.0


def ending_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(DEFAULTS)
    out.update(((cfg.get("cut") or {}).get("ending") or {}))
    return out


def ends_sentence(word: str, es: dict[str, Any] | None = None) -> bool:
    """True when a whisper word carries sentence-final punctuation."""
    es = es or DEFAULTS
    w = str(word or "").strip().rstrip(_CLOSERS)
    if not w or w[-1] not in str(es["sentence_punct"]):
        return False
    if w.endswith("..") or w.endswith("…"):
        return False                      # an ellipsis trails off; it does not finish
    if w.lower() in {a.lower() for a in es.get("abbreviations") or []}:
        return False
    return True


def _sorted_words(words: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    ws = [w for w in words if w.get("start") is not None and w.get("end") is not None]
    return sorted(ws, key=lambda w: (float(w["start"]), float(w["end"])))


def sentence_breaks(
    words: Sequence[dict[str, Any]],
    audio_end: float,
    es: dict[str, Any] | None = None,
) -> list[SafeSpan]:
    """Every sentence break in `words`, unclipped. The gap after the last word reaches
    `audio_end` (the end of the audio the words were transcribed from)."""
    es = es or DEFAULTS
    ws = _sorted_words(words)
    after = float(es["min_after_word_s"])
    margin = float(es["next_word_margin_s"])
    out: list[SafeSpan] = []
    if ws:
        # Before anyone speaks there is no sentence to interrupt.
        first = float(ws[0]["start"])
        out.append(SafeSpan(-math.inf, first - margin, -math.inf, first, "pause"))
    for i, w in enumerate(ws):
        w_end = float(w["end"])
        nxt = float(ws[i + 1]["start"]) if i + 1 < len(ws) else float(audio_end)
        is_last = i + 1 >= len(ws)
        # A later word may start inside this one (whisper overlap): the gap is what is left.
        if not is_last:
            nxt = max(w_end, min(float(x["start"]) for x in ws[i + 1:]))
        gap = nxt - w_end + _EPS                      # float noise must not decide a boundary
        if gap >= float(es["pause_gap_s"]):
            reason = "pause"
        elif gap >= float(es["punct_gap_s"]) and ends_sentence(str(w.get("word", "")), es):
            reason = "punct"
        else:
            continue
        lo = w_end + after
        hi = nxt - margin if not is_last else nxt
        if lo > hi:
            continue
        out.append(SafeSpan(lo, hi, w_end, nxt if not is_last else math.inf, reason))
    return out


@dataclass
class Envelope:
    """Short-window audio level in dB on the same timeline as the words: db[i] is the level
    at t0 + i * hop."""
    t0: float
    hop: float
    db: Sequence[float]

    def at(self, i: int) -> float:
        return float(self.db[i])

    def time(self, i: int) -> float:
        return self.t0 + i * self.hop

    def index(self, t: float) -> int:
        return int(round((t - self.t0) / self.hop))

    def speech_level(self, floor_db: float = -50.0) -> float | None:
        vals = sorted(float(v) for v in self.db if float(v) > floor_db)
        return vals[len(vals) // 2] if vals else None


def audio_sentence_gaps(
    words: Sequence[dict[str, Any]],
    env: Envelope | None,
    es: dict[str, Any] | None = None,
) -> list[SafeSpan]:
    """Sentence breaks measured in the audio: after every word ending in . ? !, the EARLIEST
    stretch of at least audio_gap_min_s that sits audio_gap_drop_db under the speech level,
    starting no earlier than 0.1 s before whisper's word end, within audio_gap_search_s of it
    and before the next word could be well under way. The earliest, not the quietest: a
    sentence-final pause comes first, a later dip may be a closure inside the next word."""
    es = es or DEFAULTS
    if env is None or not len(env.db):
        return []
    speech = env.speech_level()
    if speech is None:
        return []
    thresh = speech - float(es["audio_gap_drop_db"])
    min_frames = max(1, int(math.ceil(float(es["audio_gap_min_s"]) / env.hop - 1e-9)))
    ws = _sorted_words(words)
    out: list[SafeSpan] = []
    for i, w in enumerate(ws):
        if not ends_sentence(str(w.get("word", "")), es):
            continue
        w_end = float(w["end"])
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        lo = w_end - 0.10
        hi = w_end + float(es["audio_gap_search_s"])
        if nxt is not None:
            hi = min(hi, float(nxt["start"]) + 0.25, float(nxt["end"]) - 0.05)
        a, b = max(0, env.index(lo)), min(len(env.db) - 1, env.index(hi))
        best: tuple[float, int, int] | None = None      # (mean dB, first, last)
        k = a
        while k <= b:
            if env.at(k) <= thresh:
                j = k
                while j + 1 <= b and env.at(j + 1) <= thresh:
                    j += 1
                if j - k + 1 >= min_frames:
                    best = (0.0, k, j)
                    break
                k = j + 1
            else:
                k += 1
        if best is None:
            continue
        s, e = env.time(best[1]), env.time(best[2])
        mid = (s + e) / 2.0
        half = max(0.0, min((e - s) / 2.0 - 0.01, 0.05))
        out.append(SafeSpan(mid - half, mid + half, w_end, e, "punct_audio"))
    return out


def quiet_runs(env: Envelope, lo: float, hi: float, drop_db: float, min_s: float) -> list[tuple[float, float]]:
    """Stretches inside [lo, hi] at least `min_s` long whose level is `drop_db` under the speech."""
    speech = env.speech_level()
    if speech is None:
        return []
    thresh = speech - float(drop_db)
    a = max(0, env.index(lo))
    b = min(len(env.db) - 1, env.index(min(hi, env.time(len(env.db) - 1))))
    min_frames = max(1, int(math.ceil(float(min_s) / env.hop - 1e-9)))
    out: list[tuple[float, float]] = []
    k = a
    while k <= b:
        if env.at(k) <= thresh:
            j = k
            while j + 1 <= b and env.at(j + 1) <= thresh:
                j += 1
            if j - k + 1 >= min_frames:
                out.append((env.time(k), env.time(j)))
            k = j + 1
        else:
            k += 1
    return out


def _confirm_quiet(span: SafeSpan, env: Envelope, es: dict[str, Any]) -> list[SafeSpan]:
    """The parts of a whisper pause span where the audio really is quiet."""
    hi = span.end if math.isfinite(span.end) else env.time(len(env.db) - 1)
    lo = span.start if math.isfinite(span.start) else env.time(0)
    out = []
    for s, e in quiet_runs(env, lo, hi, float(es["pause_drop_db"]), float(es["audio_gap_min_s"])):
        a, b = max(s + 0.01, lo), min(e - 0.01, hi)
        if a <= b:
            out.append(SafeSpan(a, b, span.word_end, span.gap_end, span.reason))
    return out


def safe_spans(
    words: Sequence[dict[str, Any]],
    lo: float,
    hi: float,
    audio_end: float,
    es: dict[str, Any] | None = None,
    trusted_end: float | None = None,
    env: Envelope | None = None,
) -> list[SafeSpan]:
    """Sentence-break spans clipped to the allowed end range [lo, hi].

    `trusted_end` is an end an earlier stage already proved safe with more context (the
    cut stage saw the audio after the cut; the render stage only sees the cut), e.g. the
    cut's own end. It is added as a one-point span when it lies in [lo, hi].
    """
    es = es or DEFAULTS
    out: list[SafeSpan] = []
    breaks = sentence_breaks(words, audio_end, es)
    audio = env is not None and env.speech_level() is not None
    if audio:
        # With audio, the audio decides (see the module docstring): punctuation needs a measured
        # pause right after the word, and the long-pause case is a measured quiet stretch of at
        # least pause_gap_s. Whisper's own gaps only count when it heard nobody for a long time.
        room = float(es["end_after_word_s"])
        confirmed: list[SafeSpan] = []
        for b in breaks:
            if b.reason == "punct":
                continue
            gap_end = b.gap_end if math.isfinite(b.gap_end) else float(audio_end)
            if not math.isfinite(b.word_end) or gap_end - b.word_end >= float(es["no_speech_gap_s"]):
                start = b.start if not math.isfinite(b.word_end) else max(b.start, b.word_end + room)
                if start <= b.end:
                    confirmed.append(SafeSpan(start, b.end, b.word_end, b.gap_end, b.reason))
        ws = _sorted_words(words)
        for s, e in quiet_runs(env, env.time(0), env.time(len(env.db) - 1),
                               float(es["pause_drop_db"]), float(es["pause_gap_s"])):
            a, z = s + 0.05, e - 0.05
            prev = [float(w["end"]) for w in ws if float(w["end"]) <= s + 0.2]
            nxt = [float(w["start"]) for w in ws if float(w["start"]) >= e - 0.2]
            confirmed.append(SafeSpan(a, z, max(prev) if prev else -math.inf, min(nxt) if nxt else math.inf, "pause"))
        breaks = confirmed + audio_sentence_gaps(words, env, es)
    for s in breaks:
        a, b = max(s.start, lo), min(s.end, hi)
        if a <= b + 1e-9:
            out.append(SafeSpan(a, max(a, b), s.word_end, s.gap_end, s.reason))
    if trusted_end is not None and lo - 1e-6 <= trusted_end <= hi + 1e-6:
        if not any(s.contains(trusted_end) for s in out):
            before = [float(w["end"]) for w in words if float(w["end"]) <= trusted_end + 1e-6]
            spoken_through = any(float(w["start"]) < trusted_end < float(w["end"]) for w in words)
            if not spoken_through:
                # The earlier stage checked trusted_end itself. Only the measured quiet stretch
                # leading up to it is safe too; without audio, only the point (an end placed
                # "after the last word" by whisper's clock was +7 dB loud once).
                last = max(before) if before else -math.inf
                a = trusted_end
                if audio:
                    tail = quiet_tail_s(trusted_end, env, es, drop_db=float(es["pause_drop_db"])) or 0.0
                    if tail > 0.02:
                        a = max(lo, trusted_end - tail + 0.01)
                        if math.isfinite(last):
                            a = max(a, last + float(es["min_after_word_s"]))
                out.append(SafeSpan(min(a, trusted_end), trusted_end, last, math.inf, "verified"))
    out.sort(key=lambda s: s.start)
    return out


def choose_end(spans: Sequence[SafeSpan], target: float, es: dict[str, Any] | None = None) -> float | None:
    """The safe end nearest `target`; on a tie, the earlier one (the latest before it)."""
    es = es or DEFAULTS
    best: tuple[float, float, SafeSpan] | None = None       # (distance, point, span)
    for s in spans:
        p = s.point_for(float(target), es)
        key = (round(abs(p - float(target)), 6), p)
        if best is None or key < (round(best[0], 6), best[1]):
            best = (abs(p - float(target)), p, s)
    if best is None:
        return None
    # Round to the millisecond, but never out of the span: a one-point span (the cut's own end,
    # e.g. 18.98567 s) rounded up would fall outside it and be rejected as "not after a sentence".
    _, p, span = best
    return min(max(round(p, 3), span.start), span.end)


def span_at(t: float, spans: Sequence[SafeSpan], tol: float = 1e-4) -> SafeSpan | None:
    for s in spans:
        if s.contains(t, tol):
            return s
    return None


def snap_inside(t: float, span: SafeSpan, step: float) -> float:
    """Round `t` to the `step` grid (a frame, a centisecond) without leaving `span`."""
    step = float(step)
    r = round(round(float(t) / step) * step, 6)
    if span.contains(r):
        return r
    for cand in (math.ceil(span.start / step - 1e-9) * step, math.floor(span.end / step + 1e-9) * step):
        cand = round(cand, 6)
        if span.contains(cand):
            return cand
    return round(float(t), 6)        # span narrower than one step: keep the exact point


def last_sentence_word_end(
    words: Sequence[dict[str, Any]], before: float, audio_end: float, es: dict[str, Any] | None = None
) -> float | None:
    """End of the last word that completes a sentence before `before` (None if none)."""
    ends = [s.word_end for s in sentence_breaks(words, audio_end, es)
            if math.isfinite(s.word_end) and s.word_end <= before]
    return max(ends) if ends else None


def speaks_through(t: float, words: Sequence[dict[str, Any]]) -> bool:
    """True when `t` falls inside a word."""
    return any(float(w["start"]) < t < float(w["end"]) for w in words)


def gap_is_sentence_break(
    s: float,
    e: float,
    words: Sequence[dict[str, Any]],
    es: dict[str, Any] | None = None,
    margin: float = 0.05,
) -> bool:
    """A3 guard: may the audio in [s, e] be dropped without cutting into a sentence?

    True only when no word touches [s - margin, e + margin] and the gap holding the span
    is a sentence break (or lies before the first / after the last word).
    """
    es = es or DEFAULTS
    ws = _sorted_words(words)
    if not ws:
        return True
    if any(float(w["start"]) < e + margin and float(w["end"]) > s - margin for w in ws):
        return False
    prev = [w for w in ws if float(w["end"]) <= s]
    nxt = [w for w in ws if float(w["start"]) >= e]
    if not prev or not nxt:
        return True
    p, n = prev[-1], nxt[0]
    gap = float(n["start"]) - float(p["end"]) + _EPS
    if gap >= float(es["pause_gap_s"]):
        return True
    return gap >= float(es["punct_gap_s"]) and ends_sentence(str(p.get("word", "")), es)


def quiet_tail_s(end: float, env: Envelope | None, es: dict[str, Any] | None = None,
                 drop_db: float | None = None) -> float | None:
    """How long the audio has already been quiet (audio_gap_drop_db, or `drop_db`, under speech)
    at `end`; None without a usable envelope. The end fade must fit inside this."""
    es = es or DEFAULTS
    if env is None or not len(env.db):
        return None
    speech = env.speech_level()
    if speech is None:
        return None
    thresh = speech - float(drop_db if drop_db is not None else es["audio_gap_drop_db"])
    k = min(len(env.db) - 1, env.index(end))
    j = k
    while j >= 0 and env.at(j) <= thresh:
        j -= 1
    return max(0.0, end - env.time(j + 1)) if j < k else 0.0


def content_end(
    words: Sequence[dict[str, Any]],
    duration: float,
    min_dur: float,
    max_dur: float,
    es: dict[str, Any] | None = None,
    trusted_end: float | None = None,
    env: Envelope | None = None,
) -> tuple[float, list[SafeSpan]]:
    """Render-stage end inside an existing cut: the safe end nearest the cut's end, i.e.
    never earlier than the last completed sentence the cut holds.

    `trusted_end` is the cut's own end when the cut stage verified it (it saw what follows
    the cut). Raises NoCleanEnding when the speech runs through every allowed end.
    """
    es = es or DEFAULTS
    duration = float(duration)
    hi = min(float(max_dur), duration)
    lo = min(float(min_dur), hi)
    spans = safe_spans(words, lo, hi, duration, es, trusted_end=trusted_end, env=env)
    end = choose_end(spans, duration, es)
    if end is None:
        raise NoCleanEnding(
            f"no_clean_ending: speech runs through every end point in {lo:.2f}-{hi:.2f}s of the cut"
        )
    return end, spans
