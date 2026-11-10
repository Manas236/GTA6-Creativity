"""Every faster-whisper transcription goes through here (cut stage, hooks, render, VO, scripts).

* The model is `transcribe.model` in config.yaml (falls back to the older `render.whisper_model`).
* The GTA glossary (`transcribe.glossary`) is passed on EVERY call, as faster-whisper `hotwords`
  and/or as the `initial_prompt` (`transcribe.glossary_as`), so base.en stops hearing
  "wanted level" as "wanna love" and "Leonida" as "Leonidas".
* `cache_key()` folds the model, the glossary and how it is passed into one string; the cached
  cut transcript (`items.transcript_words`) is only reused under the same key, so changing any
  of them re-transcribes instead of burning in captions from the old settings.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Sequence

from . import config

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "model": "base.en",
    "glossary": [],
    "glossary_as": "hotwords",       # "hotwords" | "initial_prompt" | "both" | "none"
    "prompt_template": "GTA 6 video. Names and terms: {terms}.",
}

_MODELS: dict[str, Any] = {}


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(DEFAULTS)
    legacy = (cfg.get("render") or {}).get("whisper_model")
    if legacy:
        out["model"] = legacy
    out.update({k: v for k, v in ((cfg.get("transcribe") or {}).items()) if v is not None})
    out["glossary"] = [str(t).strip() for t in out.get("glossary") or [] if str(t).strip()]
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
    """'<model>|<hash of glossary + how it is passed>': a cached transcript is valid only under it."""
    ts = ts or settings(cfg)
    blob = repr((ts["model"], sorted(prompt_kwargs(ts).items())))
    return f"{ts['model']}|{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


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
    """Word-level transcript [{word, start, end, prob}] with the configured model and glossary.

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
    try:
        model = get_model(str(ts["model"]))
        segments, _ = model.transcribe(
            path_str,
            word_timestamps=True,
            language="en",
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            **prompt_kwargs(ts),
        )
        words: list[dict[str, Any]] = []
        for segment in segments:
            for w in segment.words or []:
                clean = w.word.strip()
                if clean:
                    words.append({"word": clean, "start": float(w.start), "end": float(w.end),
                                  "prob": float(w.probability)})
        return words
    except Exception as e:
        if raise_errors:
            raise
        log.warning("Whisper transcription failed for %s: %s", path_str, e)
        return []


def text(words: Sequence[dict[str, Any]]) -> str:
    return " ".join(str(w.get("word", "")).strip() for w in words).strip()
