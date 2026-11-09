"""C1: caption keywords (green) with a Gemini source and a zero-network rule fallback.

The Gemini hook call (pipeline.hooks) now also returns 3-6 `keywords`; they are cached
in `items.keywords` as a JSON array. Without an API key the rule fallback picks:
  * tokens containing a digit (6, 2026, 60FPS, GTA6, ...)
  * GTA names and places (GTA_TERMS below)
  * anything in `style.keyword_color.extra_terms`
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from . import db, style

log = logging.getLogger(__name__)

#: GTA characters, places and franchise nouns that read as keywords in a caption.
GTA_TERMS: tuple[str, ...] = (
    "GTA", "GTAVI", "GTA6", "ROCKSTAR", "GRAND", "THEFT", "AUTO",
    "JASON", "LUCIA", "CAL", "BOOBIE", "DRE", "RAUL", "BRIAN",
    "VICE", "LEONIDA", "AMBROSIA", "MIAMI", "KEYS", "EVERGLADES",
    "PORT", "GATOR", "MOUNT", "KALAGA", "GRASSRIVERS", "HAMLET",
    "TRAILER", "GAMEPLAY", "LEAK", "LEAKED", "MAP", "HEIST", "POLICE",
    "CHASE", "MONEY", "CASH", "CAR", "CARS", "BIKE", "BEACH", "ONLINE",
    "MULTIPLAYER", "LAUNCH", "RELEASE", "PS5", "XBOX", "RTX", "FPS",
)

_WORD_RE = re.compile(r"[A-Z0-9][A-Z0-9'&-]*")


def normalize(token: str) -> str:
    """Upper-case a caption token and drop punctuation, matching caption rendering."""
    return "".join(ch for ch in str(token).upper() if ch.isalnum() or ch in "'&-").strip("'&-")


def _candidate_terms(ss: dict[str, Any]) -> set[str]:
    extra = ss.get("keyword_color", {}).get("extra_terms") or []
    return {normalize(t) for t in (*GTA_TERMS, *extra) if normalize(t)}


def rule_keywords(
    text_or_words: str | Iterable[Any],
    cfg: dict[str, Any] | None = None,
    limit: int | None = None,
) -> list[str]:
    """Zero-network keyword extraction: numbers, GTA names/places and the config list."""
    ss = style.style_settings(cfg)
    terms = _candidate_terms(ss)
    limit = int(limit if limit is not None else ss["keyword_color"]["max_keywords"])

    if isinstance(text_or_words, str):
        tokens = _WORD_RE.findall(text_or_words.upper())
    else:
        tokens = []
        for w in text_or_words:
            raw = w.get("word") if isinstance(w, dict) else w
            tok = normalize(raw)
            if tok:
                tokens.append(tok)

    out: list[str] = []
    for tok in tokens:
        if tok in out:
            continue
        if any(ch.isdigit() for ch in tok) or tok in terms:
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def clean_keywords(raw: Any, cfg: dict[str, Any] | None = None) -> list[str]:
    """Normalize a model-supplied keyword list down to max_keywords unique tokens."""
    ss = style.style_settings(cfg)
    limit = int(ss["keyword_color"]["max_keywords"])
    if isinstance(raw, str):
        raw = re.split(r"[,;]+", raw)
    out: list[str] = []
    for item in raw or []:
        for tok in str(item).split():
            tok = normalize(tok)
            if tok and tok not in out:
                out.append(tok)
    return out[:limit]


def store_keywords(item_id: str, keywords: list[str], db_path: Any = None) -> None:
    """Cache keywords on the item row as a JSON array."""
    with db.db(db_path) as conn:
        conn.execute(
            "UPDATE items SET keywords = :kw, updated_at = :now WHERE id = :id",
            {"id": item_id, "kw": json.dumps(keywords), "now": db.now()},
        )


def load_keywords(item: dict[str, Any] | None) -> list[str]:
    """Read the cached keyword list off an item row (empty when absent/corrupt)."""
    if not item:
        return []
    raw = item.get("keywords")
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    return [normalize(t) for t in data if normalize(t)] if isinstance(data, list) else []


def keywords_for_item(
    item: dict[str, Any] | None,
    words: Iterable[Any] | None = None,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    item_id: str | None = None,
) -> list[str]:
    """Cached Gemini keywords when present, otherwise the rule fallback (cached back)."""
    ss = style.style_settings(cfg)
    cached = load_keywords(item)
    if len(cached) >= int(ss["keyword_color"]["min_keywords"]):
        return cached[: int(ss["keyword_color"]["max_keywords"])]

    source = list(words or [])
    if not source and item:
        source = [item.get("hook_text") or ""]
    found = rule_keywords(source if source and isinstance(source[0], dict) else " ".join(map(str, source)), cfg)
    merged = cached + [k for k in found if k not in cached]
    merged = merged[: int(ss["keyword_color"]["max_keywords"])]
    iid = item_id or (item or {}).get("id")
    if merged and iid:
        try:
            store_keywords(iid, merged, db_path=db_path)
        except Exception as e:  # pragma: no cover - cache write is best effort
            log.debug("Could not cache keywords for %s: %s", iid, e)
    return merged


def is_keyword(token: str, keywords: Iterable[str]) -> bool:
    """Caption-time match: normalized equality against the keyword list."""
    tok = normalize(token)
    return bool(tok) and tok in {normalize(k) for k in keywords}
