"""Take old items out of Review: stage `archived`, files MOVED (never deleted) to media/archive/<date>/.

Review lists rendered / awaiting_approval (To approve), needs_review (Needs a decision), failed and
rejected. An archived item is in none of those tabs; the Library still lists it. Everything under the
media folder that belongs to the item (download, cut, voiceover, renders + their .meta.json, subtitles,
thumbnails, preview renders) moves to media/archive/<date>/<its old path relative to media>/, and
the item's path columns and `renders` rows are pointed at the new place, so nothing dangles and the
move can be undone by hand. Items with a posts row are never touched (a post may be live).
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from . import clock, config, db, telegram_bot

log = logging.getLogger(__name__)

REVIEW_STAGES = ("rendered", "awaiting_approval", "needs_review", "failed", "rejected")
PATH_COLUMNS = ("download_path", "cut_path", "voiceover_audio_path", "render_a_path", "render_b_path")


def review_items_before(day: str, db_path: Any = None) -> list[dict[str, Any]]:
    """Items shown in Review that were created before `day` (YYYY-MM-DD, UTC) and never posted."""
    with db.db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM items WHERE stage IN ({','.join('?' * len(REVIEW_STAGES))}) "
            "AND created_at < ? AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.item_id = items.id) "
            "ORDER BY created_at, id", (*REVIEW_STAGES, f"{day}T00:00:00Z"))
        return [dict(r) for r in rows]


def item_files(item: dict[str, Any], media: Path, db_path: Any = None) -> list[Path]:
    """Every existing file or folder under `media` that belongs to the item."""
    found: list[Path] = []
    for col in PATH_COLUMNS:
        if item.get(col):
            found.append(Path(item[col]))
    for r in db.renders_for_item(item["id"], db_path):
        found += [Path(r["path"]), Path(r["meta_path"])] if r.get("meta_path") else [Path(r["path"])]
    if media.is_dir():
        for p in media.rglob(f"*{item['id']}*"):
            if "archive" not in p.relative_to(media).parts[:1]:
                found.append(p)
    root = media.resolve()
    keep: set[Path] = set()
    for p in found:
        rp = p.resolve()
        # only what exists inside the media folder (and is not archived already) moves
        if not p.exists() or not rp.is_relative_to(root) or rp.relative_to(root).parts[:1] == ("archive",):
            continue
        keep.add(rp)
    # a folder that moves takes the files inside it along
    dirs = [d for d in keep if d.is_dir()]
    return sorted(p for p in keep if not any(p != d and p.is_relative_to(d) for d in dirs))


def archive_item(item: dict[str, Any], day: str, *, db_path: Any = None, media: Path | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    media = media or config.media_root()
    dest_root = media / "archive" / day
    moves: dict[str, str] = {}
    for src in item_files(item, media, db_path):
        dest = dest_root / src.resolve().relative_to(media.resolve())
        moves[str(src)] = str(dest)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
    note = (f"archived {day}: created {item['created_at'][:10]}, before the {day} Review reset "
            f"(was {item['stage']}{': ' + item['rejection_reason'] if item.get('rejection_reason') else ''})")
    if not dry_run:
        def moved(p: str | None) -> str | None:
            if not p:
                return p
            for src, dest in moves.items():
                if Path(p) == Path(src):
                    return dest
                try:
                    return str(Path(dest) / Path(p).relative_to(src))
                except ValueError:
                    continue
            return p
        fields: dict[str, Any] = {"rejection_reason": note[:1000], "creation_phase": None}
        fields.update({col: moved(item.get(col)) for col in PATH_COLUMNS if item.get(col)})
        with db.db(db_path) as conn:
            for r in conn.execute("SELECT * FROM renders WHERE item_id=?", (item["id"],)).fetchall():
                conn.execute("UPDATE renders SET path=?, meta_path=? WHERE item_id=? AND variant=?",
                             (moved(r["path"]), moved(r["meta_path"]), item["id"], r["variant"]))
        db.transition_stage(item["id"], "archived", fields, db_path)
        db.log_stage(item["id"], "archive", "success", message=note[:1000], db_path=db_path)
        card = telegram_bot.card_state(db_path)
        if card and card.get("item_id") == item["id"]:
            telegram_bot.set_card_state(None, db_path)
    return {"id": item["id"], "was": item["stage"], "title": item.get("original_title"),
            "author": item.get("author_name"), "moved": moves, "note": note}


def archive_before(day: str | None = None, *, db_path: Any = None, media: Path | None = None,
                   dry_run: bool = False) -> list[dict[str, Any]]:
    day = day or clock.utcnow().date().isoformat()
    return [archive_item(item, day, db_path=db_path, media=media, dry_run=dry_run)
            for item in review_items_before(day, db_path)]
