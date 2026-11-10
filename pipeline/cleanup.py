"""Stage 12: disk retention (CP4, PLAN.md section 1.8).

Three rules, all from `config.yaml -> retention`:
  1. the SOURCE download goes as soon as the item is rendered (`purge_downloads_immediately`);
  2. the RENDERS (with the cut, the voiceover and the subtitles they were made from) go
     `purge_renders_after_days` (7) after the item was posted;
  3. everything a REJECTED or FAILED item left behind goes `purge_rejected_failed_after_days` (3)
     after it was rejected / failed.

What is never deleted:
  * anything a post row still needs - an item with a row that is pending, uploading,
    container_ready, publishing, or failed-with-retries-left keeps every file (rules 2 and 3);
  * anything outside the media folder (a path in the database is not trusted on its own);
  * a file another item still points at;
  * preview_only items' files (they belong to the style previews, not to the pipeline).

Only file paths recorded on the item row (plus that item's subtitles) are touched, the column is
set to NULL once the file is gone, and a measured item whose renders are gone becomes `cleaned`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import clock, config, db, posting

log = logging.getLogger(__name__)

SOURCE_COLUMNS = ("download_path",)
RENDER_COLUMNS = ("render_a_path", "render_b_path", "cut_path", "voiceover_audio_path")
ALL_COLUMNS = SOURCE_COLUMNS + RENDER_COLUMNS
AFTER_RENDER_STAGES = ("rendered", "awaiting_approval", "approved", "queued", "posted", "measured", "cleaned")


def retention(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    r = ((cfg or {}).get("retention") or {})
    return {
        "source_after_render": bool(r.get("purge_downloads_immediately", True)),
        "renders_days": float(r.get("purge_renders_after_days", 7)),
        "rejected_failed_days": float(r.get("purge_rejected_failed_after_days", 3)),
    }


def inside_media(path: Path) -> bool:
    """True only for a path that really lies inside the media folder (symlinks and .. resolved)."""
    try:
        root = config.media_root().resolve()
        resolved = path.resolve()
        return resolved != root and root in resolved.parents
    except OSError:
        return False


class Cleaner:
    def __init__(self, cfg: dict[str, Any], db_path: Any = None, now: datetime | None = None):
        self.cfg = cfg
        self.db_path = db_path
        self.now = now or clock.utcnow()
        self.rules = retention(cfg)
        self.deleted: list[str] = []
        self.bytes_freed = 0
        self.protected: list[str] = []
        self.cleaned: list[str] = []

    # ------------------------------------------------------------- helpers
    def _referenced_elsewhere(self, path: str, item_id: str) -> bool:
        where = " OR ".join(f"{c} = :p" for c in ALL_COLUMNS)
        with db.db(self.db_path) as conn:
            row = conn.execute(
                f"SELECT 1 FROM items WHERE id != :id AND ({where}) LIMIT 1", {"p": path, "id": item_id}
            ).fetchone()
        return row is not None

    def _delete(self, path: Path) -> bool:
        """Delete one file if it is inside media/. True when the file is gone afterwards."""
        if not inside_media(path):
            log.warning("cleanup: %s is outside the media folder; left alone", path)
            return False
        try:
            if path.is_file():
                size = path.stat().st_size
                path.unlink()
                self.deleted.append(str(path))
                self.bytes_freed += size
            return not path.exists()
        except OSError as e:
            log.warning("cleanup: could not delete %s: %s", path, e)
            return False

    def purge(self, item: dict[str, Any], columns: tuple[str, ...], subtitles: bool = False) -> None:
        """Delete the files in `columns` for one item and NULL the columns whose file is gone."""
        cleared: list[str] = []
        for col in columns:
            value = item.get(col)
            if not value:
                continue
            if self._referenced_elsewhere(value, item["id"]):
                cleared.append(col)       # another item owns the file now; just drop our pointer
                continue
            if self._delete(Path(value)):
                cleared.append(col)
        if subtitles:
            sub_dir = config.media_root() / "subtitles"
            if sub_dir.is_dir():
                for pattern in (f"{item['id']}_*", f"{item['id']}.*"):
                    for f in sub_dir.glob(pattern):
                        self._delete(f)
        if cleared:
            sets = ", ".join(f"{c} = NULL" for c in cleared)
            with db.db(self.db_path) as conn:
                conn.execute(f"UPDATE items SET {sets}, updated_at = ? WHERE id = ?", (db.now(), item["id"]))

    def _items(self, stages: tuple[str, ...]) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in stages)
        with db.db(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT * FROM items WHERE stage IN ({marks}) AND COALESCE(preview_only, 0) = 0", stages
            ).fetchall()
        return [dict(r) for r in rows]

    def _needed_by_a_post(self, item: dict[str, Any]) -> bool:
        if posting.item_has_open_posts(item["id"], self.cfg, self.db_path):
            self.protected.append(item["id"])
            return True
        return False

    # ------------------------------------------------------------- the rules
    def sources_after_render(self) -> None:
        if not self.rules["source_after_render"]:
            return
        for item in self._items(AFTER_RENDER_STAGES):
            if item.get("download_path"):
                self.purge(item, SOURCE_COLUMNS)

    def renders_after_post(self) -> None:
        keep = timedelta(days=self.rules["renders_days"])
        for item in self._items(("posted", "measured")):
            live = [p["posted_at"] for p in posting.get_posts_for_item(item["id"], db_path=self.db_path)
                    if p["status"] == posting.STATUS_LIVE and p["posted_at"]]
            posted_at = clock.parse(max(live) if live else item["stage_updated_at"])
            if self.now < posted_at + keep or self._needed_by_a_post(item):
                continue
            self.purge(item, ALL_COLUMNS, subtitles=True)
            if item["stage"] == "measured":
                # `posted` items are left for analytics to finish; they are picked up here again.
                db.transition_stage(item["id"], "cleaned", db_path=self.db_path)
                db.log_stage(item["id"], "cleanup", "success",
                             message=f"renders purged {self.rules['renders_days']:g} days after posting",
                             db_path=self.db_path)
                self.cleaned.append(item["id"])

    def rejected_and_failed(self) -> None:
        keep = timedelta(days=self.rules["rejected_failed_days"])
        for item in self._items(("rejected", "failed")):
            if not any(item.get(c) for c in ALL_COLUMNS):
                continue
            if self.now < clock.parse(item["stage_updated_at"]) + keep or self._needed_by_a_post(item):
                continue
            self.purge(item, ALL_COLUMNS, subtitles=True)

    def report(self) -> dict[str, Any]:
        return {"deleted_files": len(self.deleted), "bytes_freed": self.bytes_freed, "deleted": self.deleted,
                "cleaned": self.cleaned, "protected": sorted(set(self.protected))}


def run_cleanup_stage(
    cfg: dict[str, Any] | None = None, db_path: Any = None, now: datetime | None = None
) -> dict[str, Any]:
    """One retention pass. Safe to run every cycle: a file that is already gone is a no-op."""
    cfg = cfg or config.get_config()
    cleaner = Cleaner(cfg, db_path, now)
    cleaner.sources_after_render()
    cleaner.renders_after_post()
    cleaner.rejected_and_failed()
    out = cleaner.report()
    if out["deleted_files"]:
        log.info("cleanup: %d file(s), %.1f MB freed", out["deleted_files"], out["bytes_freed"] / 1e6)
    return out
