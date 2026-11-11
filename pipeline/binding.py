"""Caption binding: every hook and voiceover record names the exact item and cut segment it was
written for, and render refuses anything that does not match.

Why this exists (the wrong-caption bug): the cut stage can move an item's window - the
sentence-aligned windows, a re-cut by scripts/render_previews.py, a model or glossary change - but it
used to leave `hook_text`, `voiceover_script`, `voiceover_audio_path` and `keywords` on the row, and
render only generated a hook when `hook_text` was empty. A render could therefore burn a hook (and
speak a voiceover) written for a different part of the source video over the new clip.

Now:
  * the hooks stage stores `items.hook_binding`: item id, cut_start/cut_end, cut file, a hash of
    the segment transcript Gemini saw, a hash of the hook and VO script, and how the hook was checked;
  * the voiceover stage stores `items.voiceover_binding`: item id, segment, the script it spoke, the wav;
  * the cut stage clears hook/VO/keywords/renders whenever it moves the window
    (`reset_for_new_segment`);
  * render calls `assert_caption_binding` right before it burns the text in, and fails loudly
    (CaptionMismatchError) if any of it belongs to another item or another segment.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import clock

#: Segment bounds are stored with 2 decimals; anything further apart is another segment.
SEGMENT_TOL_S = 0.005

#: hook_binding.check values that may be rendered. "needs_review" may not (until a human approves).
RENDERABLE_CHECKS = ("segment_check", "human_approved")

#: Item fields that describe one segment's content. Cleared when the window moves.
SEGMENT_CONTENT_FIELDS: dict[str, Any] = {
    "hook_style": None,
    "hook_text": None,
    "hook_grounded": 0,
    "authenticity": "unverified",
    "voiceover_script": None,
    "voiceover_audio_path": None,
    "keywords": None,
    "hook_binding": None,
    "voiceover_binding": None,
    "hook_review": None,
    "render_a_path": None,
    "render_b_path": None,
    "caption_text": None,
}


class CaptionMismatchError(RuntimeError):
    """The hook / voiceover about to be rendered was not made for this item + segment."""


def digest(text: Any) -> str:
    """Short, stable hash of a text (whitespace-normalised)."""
    norm = " ".join(str(text or "").split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def transcript_text(words: Iterable[dict[str, Any]] | None) -> str:
    """The segment transcript exactly as the hooks stage hands it to Gemini."""
    return " ".join(str(w.get("word", "")).strip() for w in (words or [])).strip()


def _load(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _same_time(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) <= SEGMENT_TOL_S
    except (TypeError, ValueError):
        return False


def _same_path(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def hook_record(
    item: dict[str, Any],
    hook_text: str,
    voiceover_script: str,
    transcript: str,
    check: str,
    reason: str = "",
) -> str:
    """The items.hook_binding value for a hook made from `transcript` (the CUT segment's words)."""
    return json.dumps({
        "item_id": item["id"],
        "cut_start": item.get("cut_start"),
        "cut_end": item.get("cut_end"),
        "cut_path": item.get("cut_path"),
        "transcript_sha1": digest(transcript),
        "transcript_chars": len(transcript or ""),
        "hook_sha1": digest(hook_text),
        "voiceover_sha1": digest(voiceover_script),
        "check": check,
        "reason": reason[:300],
        "at": clock.now_iso(),
    })


def voiceover_record(item: dict[str, Any], script: str, path: Path | str) -> str:
    """The items.voiceover_binding value for a wav synthesized from `script`."""
    return json.dumps({
        "item_id": item["id"],
        "cut_start": item.get("cut_start"),
        "cut_end": item.get("cut_end"),
        "script_sha1": digest(script),
        "path": str(path),
        "at": clock.now_iso(),
    })


def with_check(raw: Any, check: str, reason: str = "") -> str | None:
    """A copy of a hook_binding with its `check` replaced (e.g. a human approved it)."""
    rec = _load(raw)
    if rec is None:
        return None
    rec["check"] = check
    if reason:
        rec["reason"] = reason[:300]
    rec["checked_at"] = clock.now_iso()
    return json.dumps(rec)


def _segment_problems(rec: dict[str, Any], item: dict[str, Any], what: str) -> list[str]:
    out: list[str] = []
    if rec.get("item_id") != item.get("id"):
        out.append(f"{what} record belongs to item {rec.get('item_id')!r}, not {item.get('id')!r}")
    for k in ("cut_start", "cut_end"):
        if not _same_time(rec.get(k), item.get(k)):
            out.append(f"{what} was made for {k}={rec.get(k)}, the item's segment has {k}={item.get(k)}")
    return out


def hook_binding_problem(item: dict[str, Any] | None, hook_text: str | None = None) -> str | None:
    """None when the item's hook was made (and passed its check) for exactly this item + segment.
    Otherwise a one-line reason. `hook_text` = the text about to be used (default: the item's)."""
    if not item:
        return "no item"
    text = item.get("hook_text") if hook_text is None else hook_text
    if not text:
        return "no hook text"
    rec = _load(item.get("hook_binding"))
    if rec is None:
        return "hook has no segment record (it predates the segment check or was set by hand)"
    problems = _segment_problems(rec, item, "hook")
    if rec.get("cut_path") and item.get("cut_path") and not _same_path(rec["cut_path"], item["cut_path"]):
        problems.append(f"hook was made from cut file {rec['cut_path']}, the item's cut is {item['cut_path']}")
    if rec.get("hook_sha1") != digest(text):
        problems.append("the hook text is not the text the record was made for")
    if rec.get("check") not in RENDERABLE_CHECKS:
        problems.append(f"hook check is {rec.get('check')!r} (needs a pass or a human approval)")
    return "; ".join(problems) or None


def voiceover_binding_problem(item: dict[str, Any] | None, vo_path: Path | str | None = None) -> str | None:
    """None when the voiceover wav was synthesized from the item's current script for this segment."""
    if not item:
        return "no item"
    path = vo_path if vo_path is not None else item.get("voiceover_audio_path")
    if not path:
        return "no voiceover audio"
    rec = _load(item.get("voiceover_binding"))
    if rec is None:
        return "voiceover has no segment record"
    problems = _segment_problems(rec, item, "voiceover")
    if rec.get("script_sha1") != digest(item.get("voiceover_script")):
        problems.append("voiceover audio was synthesized from a different script")
    if not _same_path(rec.get("path"), path):
        problems.append(f"voiceover record names {rec.get('path')}, render would use {path}")
    return "; ".join(problems) or None


def assert_caption_binding(
    item: dict[str, Any],
    hook_text: str,
    *,
    cut_path: Path | str | None = None,
    vo_path: Path | str | None = None,
    transcript_words: Sequence[dict[str, Any]] | None = None,
) -> None:
    """Hard render-time check: the hook (and voiceover, when `vo_path` is given) about to be burned
    in belong to THIS item and THIS segment. Raises CaptionMismatchError naming every mismatch.

    `cut_path`: the file being rendered; it must be the cut the hook was made from.
    `transcript_words`: the cached segment words (cut timeline); when given, they must be the
    transcript the hook was generated from."""
    problems: list[str] = []
    hp = hook_binding_problem(item, hook_text)
    if hp:
        problems.append(hp)
    rec = _load(item.get("hook_binding")) or {}
    if cut_path is not None and rec.get("cut_path") and not _same_path(rec["cut_path"], cut_path):
        problems.append(f"rendering cut {cut_path}, but the hook was made from {rec['cut_path']}")
    if transcript_words is not None and rec and rec.get("transcript_sha1") != digest(transcript_text(transcript_words)):
        problems.append("the segment transcript differs from the one the hook was generated from")
    if vo_path is not None:
        vp = voiceover_binding_problem(item, vo_path)
        if vp:
            problems.append(vp)
    if problems:
        raise CaptionMismatchError(
            f"caption_mismatch for {item.get('id')} [{item.get('cut_start')}-{item.get('cut_end')} s]: "
            + " | ".join(dict.fromkeys(problems))
        )


def reset_for_new_segment(item: dict[str, Any], new_start: float, new_end: float) -> dict[str, Any]:
    """Fields to clear when the cut stage (re)cuts `item` to [new_start, new_end].

    Empty when the item's hook was made for exactly that segment (a same-window re-cut keeps it).
    Otherwise every per-segment field, so nothing written for the old window can be rendered."""
    rec = _load(item.get("hook_binding"))
    if rec and rec.get("item_id") == item.get("id") and _same_time(rec.get("cut_start"), new_start) \
            and _same_time(rec.get("cut_end"), new_end):
        return {}
    has_content = any(item.get(k) for k in ("hook_text", "voiceover_script", "voiceover_audio_path",
                                            "keywords", "hook_binding", "render_a_path", "render_b_path"))
    return dict(SEGMENT_CONTENT_FIELDS) if has_content else {}
