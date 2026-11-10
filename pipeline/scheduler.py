"""Stage 10: posting slots and A/B variant allocation (CP4, PLAN.md section 10).

SLOTS
  `posting.profile` names a timezone profile (`local` = Asia/Kolkata 13:00 + 18:00, `server` =
  America/New_York 18:00 + 21:00). Slots are computed in that timezone (so DST is the zone's
  business, not ours) and stored on `posts.slot_datetime` as UTC ISO. An approved item is queued
  into the next free slot: in the future, not already taken, at least `posting.min_gap_hours` from
  every other scheduled slot and every recent live post, and never more than
  `posting.daily_post_target` per local day.

MISSED SLOTS (the laptop was asleep)
  A queued item whose slot passed more than `posting.missed_slot_grace_minutes` ago, and whose
  post never started, is moved to the next free slot - earliest missed first. So after three days
  asleep, four waiting items go out one per slot over the next two days; nothing is burst-posted.
  An item with a row already `publishing` or `live` is never moved: the two-phase posting machine
  finishes or settles it, which is what rules out a double post.

VARIANT
  * "Approve A only" stays A and stays ab_excluded.
  * Week 1 (the first `autopilot.week_one_days` days counted from the FIRST LIVE POST, and all the
    time before any post is live): 50/50 random.
  * After week 1: epsilon-greedy over `ab_learning` with a floor - the better variant gets
    1 - floor (80%), the other gets the floor (20%). Until both variants have
    `autopilot.bandit_min_samples_per_variant` measured posts it stays 50/50.

Everything takes `now` and an `rng`, so the tests and the CP4 dry run are deterministic.
"""
from __future__ import annotations

import logging
import random
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import clock, config, db, posting

log = logging.getLogger(__name__)

DEFAULT_PROFILES = {
    "local": {"timezone": "Asia/Kolkata", "slots": ["13:00", "18:00"]},
    "server": {"timezone": "America/New_York", "slots": ["18:00", "21:00"]},
}


# ---------------------------------------------------------------------------
# Profiles and slots
# ---------------------------------------------------------------------------

def active_profile(cfg: dict[str, Any] | None = None) -> tuple[str, ZoneInfo, list[dtime]]:
    """(name, timezone, slot times) of `posting.profile`. Raises ValueError on a bad profile."""
    posting_cfg = ((cfg or {}).get("posting") or {})
    name = str(posting_cfg.get("profile", "local"))
    profiles = posting_cfg.get("profiles") or DEFAULT_PROFILES
    if name not in profiles:
        raise ValueError(f"posting.profile '{name}' is not one of {sorted(profiles)}")
    prof = profiles[name]
    tz = ZoneInfo(str(prof["timezone"]))
    slots = sorted(dtime(int(s.split(":")[0]), int(s.split(":")[1])) for s in prof["slots"])
    if not slots:
        raise ValueError(f"posting profile '{name}' has no slots")
    return name, tz, slots


def slot_candidates(after: datetime, cfg: dict[str, Any] | None = None, days: int = 60) -> Iterator[datetime]:
    """Every slot strictly after `after`, in order, as UTC datetimes."""
    _, tz, slots = active_profile(cfg)
    first_day = after.astimezone(tz).date()
    for offset in range(days + 1):
        day = first_day + timedelta(days=offset)
        for slot in slots:
            candidate = datetime.combine(day, slot, tzinfo=tz).astimezone(timezone.utc)
            if candidate > after:
                yield candidate


def local_day(moment: datetime, cfg: dict[str, Any] | None = None) -> date:
    return moment.astimezone(active_profile(cfg)[1]).date()


def reserved_times(db_path: Any = None, exclude_item_id: str | None = None) -> list[datetime]:
    """Moments a new slot must keep its distance from: every queued item's slot, and the time each
    posted item went live."""
    with db.db(db_path) as conn:
        queued = conn.execute(
            """
            SELECT p.item_id, MIN(p.slot_datetime) AS t
            FROM posts p JOIN items i ON i.id = p.item_id
            WHERE i.stage = 'queued' GROUP BY p.item_id
            """
        ).fetchall()
        live = conn.execute(
            "SELECT item_id, MIN(posted_at) AS t FROM posts WHERE status = 'live' AND posted_at IS NOT NULL "
            "GROUP BY item_id"
        ).fetchall()
    seen: dict[str, datetime] = {}
    for row in list(live) + list(queued):   # a queued item with one live row keeps its live time
        if row["t"] and row["item_id"] != exclude_item_id and row["item_id"] not in seen:
            seen[row["item_id"]] = clock.parse(row["t"])
    return sorted(seen.values())


def next_free_slot(
    after: datetime,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    exclude_item_id: str | None = None,
) -> datetime:
    """The first slot after `after` that is free, far enough from every other post, and within the
    daily target."""
    cfg = cfg or config.get_config()
    posting_cfg = cfg.get("posting") or {}
    min_gap = timedelta(hours=float(posting_cfg.get("min_gap_hours", 3.0)))
    daily_target = int(posting_cfg.get("daily_post_target", 2))
    search_days = int(posting_cfg.get("max_slot_search_days", 60))
    taken = reserved_times(db_path, exclude_item_id=exclude_item_id)

    for candidate in slot_candidates(after, cfg, days=search_days):
        if any(abs(candidate - t) < min_gap for t in taken):
            continue
        day = local_day(candidate, cfg)
        if sum(1 for t in taken if local_day(t, cfg) == day) >= daily_target:
            continue
        return candidate
    raise RuntimeError(f"no free posting slot within {search_days} days of {clock.iso(after)}")


# ---------------------------------------------------------------------------
# Variant allocation
# ---------------------------------------------------------------------------

def in_week_one(cfg: dict[str, Any] | None = None, db_path: Any = None) -> bool:
    """True before any post is live and for `autopilot.week_one_days` after the first live post."""
    days = db.days_since_first_live(db_path)
    week = float((((cfg or {}).get("autopilot") or {}).get("week_one_days", 7)))
    return days is None or days < week


def variant_stats(db_path: Any = None) -> dict[str, dict[str, float]]:
    with db.db(db_path) as conn:
        rows = conn.execute(
            "SELECT dimension_value, sample_count, normalized_score FROM ab_learning WHERE dimension = 'variant'"
        ).fetchall()
    return {r["dimension_value"]: {"n": int(r["sample_count"] or 0), "score": float(r["normalized_score"] or 0.0)}
            for r in rows}


def variant_probabilities(cfg: dict[str, Any] | None = None, db_path: Any = None) -> tuple[dict[str, float], str]:
    """({'A': p, 'B': p}, why). The single place the allocation rule lives; analytics stores the
    same numbers in ab_learning.weight."""
    auto = ((cfg or {}).get("autopilot") or {})
    floor = min(max(float(auto.get("bandit_min_exploration_floor", 0.20)), 0.0), 0.5)
    min_samples = int(auto.get("bandit_min_samples_per_variant", 3))
    even = {"A": 0.5, "B": 0.5}

    if in_week_one(cfg, db_path):
        return even, "week 1: 50/50 random"
    stats = variant_stats(db_path)
    a, b = stats.get("A"), stats.get("B")
    if not a or not b or a["n"] < min_samples or b["n"] < min_samples:
        have = {k: v["n"] for k, v in stats.items()}
        return even, f"50/50: fewer than {min_samples} measured posts per variant ({have or 'none'})"
    if abs(a["score"] - b["score"]) < 1e-9:
        return even, f"50/50: A and B are tied at {a['score']:.3f}"
    best = "A" if a["score"] > b["score"] else "B"
    other = "B" if best == "A" else "A"
    return (
        {best: 1.0 - floor, other: floor},
        f"bandit: {best} leads (A={a['score']:.3f} n={a['n']}, B={b['score']:.3f} n={b['n']}), "
        f"{best} {100 * (1 - floor):.0f}% / {other} {100 * floor:.0f}% floor",
    )


def choose_variant(
    item: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    rng: random.Random | None = None,
) -> tuple[str, str]:
    """(variant, why) for one approved item."""
    if int(item.get("ab_excluded") or 0) or item.get("approval_type") == "approved_a_only":
        return "A", "approved A only: stays A, excluded from the A/B comparison"
    probs, why = variant_probabilities(cfg, db_path)
    roll = (rng or random).random()
    variant = "A" if roll < probs["A"] else "B"
    return variant, why


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def reschedule_missed(
    now: datetime, cfg: dict[str, Any] | None = None, db_path: Any = None
) -> list[tuple[str, str, str]]:
    """Move every queued item that missed its slot to the next free one. Returns
    [(item_id, old_slot, new_slot)], earliest missed slot first."""
    cfg = cfg or config.get_config()
    moved: list[tuple[str, str, str]] = []
    candidates: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for item in db.get_items_by_stage("queued", db_path=db_path, releasable_only=True):
        rows = posting.get_posts_for_item(item["id"], db_path=db_path)
        if not rows or all(r["status"] == posting.STATUS_DRY_RUN for r in rows):
            continue   # a dry run already walked this item; it waits for dry_run to be switched off
        if all(posting.is_terminal_failure(r, cfg) or r["status"] == posting.STATUS_DRY_RUN for r in rows):
            continue
        if posting.slot_is_missed(rows, now, cfg):
            candidates.append((posting.item_slot(rows) or "", item, rows))

    for old_slot, item, rows in sorted(candidates, key=lambda c: c[0]):
        new_slot = clock.iso(next_free_slot(now, cfg, db_path, exclude_item_id=item["id"]))
        try:
            with db.db(db_path) as conn:
                conn.execute(
                    """
                    UPDATE posts SET slot_datetime = ?, updated_at = ?,
                        status = CASE WHEN status IN ('uploading', 'container_ready', 'dry_run') THEN 'pending'
                                      WHEN status = 'failed' AND COALESCE(no_retry, 0) = 0 THEN 'pending'
                                      ELSE status END,
                        remote_container_id = CASE WHEN status = 'live' THEN remote_container_id ELSE NULL END,
                        publish_started_at = NULL
                    WHERE item_id = ?
                    """,
                    (new_slot, db.now(), item["id"]),
                )
        except sqlite3.IntegrityError as e:
            posting.fail_refused_item(item["id"], e, db_path=db_path)
            continue
        db.log_stage(item["id"], "scheduler", "success",
                     message=f"missed slot {old_slot}; moved to {new_slot}", db_path=db_path)
        log.info("scheduler: %s missed %s, moved to %s", item["id"], old_slot, new_slot)
        moved.append((item["id"], old_slot, new_slot))
    return moved


def schedule_approved(
    now: datetime,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Give every approved item a variant and the next free slot, and move it to `queued`."""
    cfg = cfg or config.get_config()
    out: list[dict[str, Any]] = []
    for item in db.get_items_by_stage("approved", db_path=db_path):   # highest discovery score first
        item_id = item["id"]
        refusal = posting.refusal_reason(item, cfg)
        if refusal or db.is_synthetic(item):
            # preview_only cannot reach `approved` (trigger); a synthetic dry-run item can, when its
            # test card is tapped. Neither is ever scheduled.
            db.mark_rejected(item_id, "not_schedulable: " + (refusal or "synthetic dry-run item"), db_path=db_path)
            db.log_stage(item_id, "scheduler", "failure", message=refusal or "synthetic dry-run item", db_path=db_path)
            continue
        try:
            variant, why = choose_variant(item, cfg, db_path, rng)
            slot = clock.iso(next_free_slot(now, cfg, db_path, exclude_item_id=item_id))
            item["chosen_variant"] = variant
            # Order matters for crash safety: the rows are created (idempotently) first and the
            # stage moves last, so a crash in between re-runs this block and changes nothing.
            with db.db(db_path) as conn:
                conn.execute("UPDATE items SET chosen_variant = ?, updated_at = ? WHERE id = ?",
                             (variant, db.now(), item_id))
            rows = posting.ensure_post_rows(item, slot_datetime=slot, cfg=cfg, db_path=db_path)
            slot = posting.item_slot(rows) or slot           # rows from an earlier attempt keep theirs
            variant = rows[0]["variant"] if rows else variant
            if rows and rows[0]["variant"] != item["chosen_variant"]:
                with db.db(db_path) as conn:
                    conn.execute("UPDATE items SET chosen_variant = ? WHERE id = ?", (variant, item_id))
            db.transition_stage(item_id, "queued", db_path=db_path)
        except sqlite3.IntegrityError as e:
            posting.fail_refused_item(item_id, e, db_path=db_path)
            continue
        except posting.PublishError as e:
            db.mark_failed(item_id, "scheduler", str(e), db_path=db_path)
            continue
        db.log_stage(item_id, "scheduler", "success",
                     message=f"variant {variant} ({why}); slot {slot}", db_path=db_path)
        log.info("scheduler: %s -> variant %s, slot %s (%s)", item_id, variant, slot, why)
        out.append({"item_id": item_id, "variant": variant, "slot": slot, "why": why})
    return out


def run_scheduler_stage(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """One scheduling pass: un-freeze dry-run rows if we went live, re-slot missed items, then
    queue newly approved items. Missed items are re-slotted first so they keep their place ahead
    of newer approvals."""
    cfg = cfg or config.get_config()
    now = now or clock.utcnow()
    reset = posting.reset_dry_run_rows(cfg=cfg, db_path=db_path)
    moved = reschedule_missed(now, cfg, db_path)
    scheduled = schedule_approved(now, cfg, db_path, rng)
    return {"profile": active_profile(cfg)[0], "reset_after_dry_run": reset, "rescheduled": moved,
            "scheduled": scheduled}
