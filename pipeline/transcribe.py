"""Every faster-whisper transcription goes through here (cut stage, hooks, render, VO, scripts).

* The model is `transcribe.model` in config.yaml (falls back to the older `render.whisper_model`).
* The GTA glossary (`transcribe.glossary`) is passed on EVERY call, as faster-whisper `hotwords`
  and/or as the `initial_prompt` (`transcribe.glossary_as`), so base.en stops hearing
  "wanted level" as "wanna love" and "Leonida" as "Leonidas".
* faster-whisper runs with `vad_filter=True` on every call; its VAD parameters are
  `transcribe.vad`.
* Whisper's word timestamps drift under music (Trailer 2, 11 Nov: "I'm" placed 9.5 s before it was
  said). After whisper, every word is checked against Silero's per-32 ms speech probability
  (`transcribe.speech_check`): a word with no speech anywhere in its span is dropped, and a word that
  starts in music but runs into speech has its start pulled to where the speech begins.
* `cache_key()` folds the model, the glossary, how it is passed, the VAD settings and the speech
  check into one string; the cached cut transcript (`items.transcript_words`) is only reused under
  the same key, so changing any of them re-transcribes instead of burning in captions from the old
  settings.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import config

log = logging.getLogger(__name__)

#: faster-whisper's VadOptions as used before they were configurable (only min_silence was set).
VAD_DEFAULTS: dict[str, Any] = {
    "threshold": 0.5,
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 400,
    "min_speech_duration_ms": 0,
}

SPEECH_CHECK_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "word_min_prob": 0.35,         # a word needs Silero speech probability >= this somewhere in its span...
    "word_pad_s": 0.1,             # ...widened by this much on each side
    "region_threshold": 0.5,       # speech regions: Silero probability >= this (hysteresis down to -0.15)...
    "region_onset_prob": 0.15,     # ...started back where the probability's ramp-up began (>= this)...
    "region_min_silence_s": 0.3,   # ...split by at least this much non-speech...
    "region_pad_s": 0.1,           # ...and padded by this much
    "clamp_tolerance_s": 0.15,     # a word starting more than this before its speech region starts there
}

DEFAULTS: dict[str, Any] = {
    "model": "base.en",
    "glossary": [],
    "glossary_as": "hotwords",       # "hotwords" | "initial_prompt" | "both" | "none"
    "prompt_template": "GTA 6 video. Names and terms: {terms}.",
    "vad": VAD_DEFAULTS,
    "speech_check": SPEECH_CHECK_DEFAULTS,
}

_MODELS: dict[str, Any] = {}

SAMPLE_RATE = 16000
VAD_HOP = 512                      # Silero's window: one probability per 32 ms


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(DEFAULTS)
    legacy = (cfg.get("render") or {}).get("whisper_model")
    if legacy:
        out["model"] = legacy
    out.update({k: v for k, v in ((cfg.get("transcribe") or {}).items()) if v is not None})
    out["glossary"] = [str(t).strip() for t in out.get("glossary") or [] if str(t).strip()]
    out["vad"] = {**VAD_DEFAULTS, **dict(out.get("vad") or {})}
    out["speech_check"] = {**SPEECH_CHECK_DEFAULTS, **dict(out.get("speech_check") or {})}
    return out


def model_name(cfg: dict[str, Any] | None = None) -> str:
    return str(settings(cfg)["model"])


def prompt_kwargs(ts: dict[str, Any]) -> dict[str, Any]:
    """The faster-whisper keyword arguments that carry the glossary."""
    terms = ", ".join(ts["glossary"])
    mode = str(ts.get("glossary_as") or "hotwords").lower()
    if not terms or mode == "none":
        return {}
    out: dict[str, Any] = {}
    if mode in ("hotwords", "both"):
        out["hotwords"] = terms
    if mode in ("initial_prompt", "both"):
        out["initial_prompt"] = str(ts["prompt_template"]).format(terms=terms)
    return out


def cache_key(cfg: dict[str, Any] | None = None, ts: dict[str, Any] | None = None) -> str:
    """'<model>|<hash of glossary + how it is passed + VAD + speech check>': a cached transcript is
    valid only under it."""
    ts = ts or settings(cfg)
    vad = {**VAD_DEFAULTS, **dict(ts.get("vad") or {})}
    sc = {**SPEECH_CHECK_DEFAULTS, **dict(ts.get("speech_check") or {})}
    blob = repr((ts["model"], sorted(prompt_kwargs(ts).items()), sorted(vad.items()), sorted(sc.items())))
    return f"{ts['model']}|{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


# --------------------------------------------------------------------------------------
# Speech evidence (Silero VAD, as bundled with faster-whisper)
# --------------------------------------------------------------------------------------

@dataclass
class SpeechProfile:
    """Silero speech probability per 32 ms hop of one file, and the speech regions built from it."""
    probs: Any                                       # numpy array, one value per hop
    hop_s: float
    duration: float
    regions: list[tuple[float, float]] = field(default_factory=list)

    def max_prob(self, a: float, b: float) -> float:
        if self.probs is None or len(self.probs) == 0:
            return 0.0
        i0 = max(0, int(a / self.hop_s))
        i1 = min(len(self.probs), max(i0 + 1, int(b / self.hop_s) + 1))
        return float(max(self.probs[i0:i1])) if i1 > i0 else 0.0

    def region_for(self, a: float, b: float) -> tuple[float, float] | None:
        """The first speech region overlapping [a, b]."""
        return next(((s, e) for s, e in self.regions if s < b and e > a), None)

    def distance(self, a: float, b: float) -> float:
        """0 when [a, b] touches a speech region, else seconds to the nearest one (inf if none)."""
        best = float("inf")
        for s, e in self.regions:
            if s < b and e > a:
                return 0.0
            best = min(best, s - b if s >= b else a - e)
        return best

    def speech_s(self, a: float = 0.0, b: float | None = None) -> float:
        b = self.duration if b is None else b
        return sum(max(0.0, min(e, b) - max(s, a)) for s, e in self.regions)

    def ratio(self, a: float = 0.0, b: float | None = None) -> float:
        b = self.duration if b is None else b
        return self.speech_s(a, b) / (b - a) if b > a else 0.0


def speech_regions(probs: Sequence[float], hop_s: float, duration: float, sc: dict[str, Any]) -> list[tuple[float, float]]:
    """Speech regions from per-hop probabilities: triggered at >= region_threshold, held while
    >= threshold - 0.15, ended after region_min_silence_s below that. Silero's probability ramps up
    over ~0.3 s at a speech onset, so each start is moved back over the contiguous frames >=
    region_onset_prob before region_pad_s is added; overlapping regions are merged."""
    th = float(sc["region_threshold"])
    neg = max(th - 0.15, 0.01)
    onset = float(sc.get("region_onset_prob", th))
    min_sil = float(sc["region_min_silence_s"])
    pad = float(sc["region_pad_s"])
    raw: list[tuple[int, int]] = []                  # [first hop, last hop + 1)
    start: int | None = None
    last = 0
    for i, p in enumerate(probs):
        if start is None:
            if p >= th:
                start, last = i, i + 1
        elif p >= neg:
            last = i + 1
        elif (i - last) * hop_s >= min_sil:
            raw.append((start, last))
            start = None
    if start is not None:
        raw.append((start, last))
    out: list[tuple[float, float]] = []
    for a, b in raw:
        while a > 0 and probs[a - 1] >= onset:
            a -= 1
        s, e = max(0.0, a * hop_s - pad), min(duration, b * hop_s + pad)
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((round(s, 3), round(e, 3)))
    return [(round(s, 3), round(e, 3)) for s, e in out]


def speech_profile(path: Path | str, cfg: dict[str, Any] | None = None,
                   sc: dict[str, Any] | None = None) -> SpeechProfile | None:
    """Silero speech probabilities and regions for an audio/video file, or None when it cannot be
    measured (then nothing is filtered, and the caller logs it)."""
    sc = sc or settings(cfg)["speech_check"]
    try:
        import numpy as np
        from faster_whisper.audio import decode_audio
        from faster_whisper.vad import get_vad_model

        audio = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        duration = len(audio) / SAMPLE_RATE
        hop_s = VAD_HOP / SAMPLE_RATE
        if len(audio) == 0:
            return SpeechProfile(np.zeros(0), hop_s, 0.0, [])
        padded = np.pad(audio, (0, (-len(audio)) % VAD_HOP))
        probs = np.asarray(get_vad_model()(padded)).reshape(-1)
        return SpeechProfile(probs, hop_s, duration, speech_regions(probs, hop_s, duration, sc))
    except Exception as e:  # noqa: BLE001 - no evidence means no filtering, never a crash
        log.warning("Speech check unavailable for %s: %s", path, e)
        return None


def check_words(
    words: Sequence[dict[str, Any]], profile: SpeechProfile, sc: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(kept, dropped). A word is dropped when Silero hears no speech anywhere in its span (+/-
    word_pad_s): under music whisper places words where nobody speaks. A kept word that starts more
    than clamp_tolerance_s before the speech region it runs into gets its start moved to that
    region's start (its end is left alone: the ending rules measure the audio after it)."""
    sc = {**SPEECH_CHECK_DEFAULTS, **dict(sc or {})}
    pad, min_p, tol = float(sc["word_pad_s"]), float(sc["word_min_prob"]), float(sc["clamp_tolerance_s"])
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for w in words:
        s, e = float(w["start"]), float(w["end"])
        if profile.max_prob(s - pad, e + pad) < min_p:
            dropped.append(dict(w))
            continue
        reg = profile.region_for(s, e)
        if reg and s < reg[0] - tol:
            s = min(reg[0], e - 0.02)
        if kept:
            s = max(s, float(kept[-1]["start"]))
        kept.append({**w, "start": round(s, 3)} if s != float(w["start"]) else dict(w))
    return kept, dropped


def download_root() -> str | None:
    from .render import whisper_download_root   # lazy: render imports this module

    return whisper_download_root()


def get_model(name: str) -> Any:
    """One loaded WhisperModel per model name (CPU, int8); faster_whisper imported lazily."""
    if name not in _MODELS:
        root = download_root()          # first: it may repoint HF_HOME before huggingface_hub loads
        import faster_whisper

        log.info("Loading faster-whisper model '%s' (device=cpu, compute_type=int8)...", name)
        _MODELS[name] = faster_whisper.WhisperModel(name, device="cpu", compute_type="int8",
                                                    download_root=root)
    return _MODELS[name]


def transcribe_words(
    path: Path | str,
    cfg: dict[str, Any] | None = None,
    *,
    model_name: str | None = None,
    raise_errors: bool = False,
    ts: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Word-level transcript [{word, start, end, prob}] with the configured model and glossary,
    whisper's VAD filter on (`transcribe.vad`), and every word checked against measured speech
    (`transcribe.speech_check`, see `check_words`).

    A failure is logged and [] is returned; raise_errors=True re-raises instead, so a caller
    can tell "whisper broke" from "nobody speaks". `model_name` / `ts` override the config
    (the benchmark uses them); the glossary still comes from the settings either way.
    """
    path_str = str(path)
    if not Path(path_str).exists():
        if raise_errors:
            raise FileNotFoundError(f"cannot transcribe, file not found: {path_str}")
        return []
    ts = dict(ts or settings(cfg))
    if model_name:
        ts["model"] = model_name
    vad = {**VAD_DEFAULTS, **dict(ts.get("vad") or {})}
    sc = {**SPEECH_CHECK_DEFAULTS, **dict(ts.get("speech_check") or {})}
    try:
        model = get_model(str(ts["model"]))
        segments, _ = model.transcribe(
            path_str,
            word_timestamps=True,
            language="en",
            vad_filter=True,
            vad_parameters=dict(vad),
            **prompt_kwargs(ts),
        )
        words: list[dict[str, Any]] = []
        for segment in segments:
            for w in segment.words or []:
                clean = w.word.strip()
                if clean:
                    words.append({"word": clean, "start": float(w.start), "end": float(w.end),
                                  "prob": float(w.probability)})
        if words and sc.get("enabled", True):
            profile = speech_profile(path_str, sc=sc)
            if profile is not None:
                words, dropped = check_words(words, profile, sc)
                if dropped:
                    log.info("Speech check on %s: dropped %d word(s) with no speech under them: %s",
                             Path(path_str).name, len(dropped),
                             ", ".join(f"{d['word']}@{float(d['start']):.2f}s" for d in dropped[:12]))
        return words
    except Exception as e:
        if raise_errors:
            raise
        log.warning("Whisper transcription failed for %s: %s", path_str, e)
        return []


def text(words: Sequence[dict[str, Any]]) -> str:
    return " ".join(str(w.get("word", "")).strip() for w in words).strip()
