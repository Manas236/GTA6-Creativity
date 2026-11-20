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

VARIANT (drawn when the slot comes due, not when the item is queued: posting.decide_variant)
  * "Approve A only" / "Approve B only" (ab_excluded, or approval_type approved_a_only /
    approved_b_only) keep the item's chosen_variant and stay ab_excluded (fixed at once). Only
    a pinned item with no chosen_variant recorded falls back: to its approval_type's variant,
    else A.
  * Week 1 (the first `autopilot.week_one_days` days counted from the FIRST LIVE POST, and all the
    time before any post is live): 50/50 random.
  * After week 1: epsilon-greedy over `ab_learning` with a floor - the better variant gets
    1 - floor (80%), the other gets the floor (20%). Until both variants have
    `autopilot.bandit_min_samples_per_variant` measured posts it stays 50/50.
  Because the draw happens at the slot, an item queued on day 6 for a day-8 slot uses the bandit
  and the learning as it stands on day 8.

Everything takes `now` and an `rng`, so the tests and the CP4 dry run are deterministic.
"""
from __future__ import annotations

import logging
import random
import sqlite3
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from . import clock, config, db, post_progress, posting

log = logging.getLogger(__name__)

DEFAULT_PROFILES = {
    "local": {"timezone": "Asia/Kolkata", "slots": ["13:00", "18:00"]},
    "server": {"timezone": "America/New_York", "slots": ["18:00", "21:00"]},
}
#: approval_type of a single-variant approval -> the variant the reviewer pinned.
PINNED_APPROVAL_TYPES = {"approved_a_only": "A", "approved_b_only": "B"}


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


def slot_available(candidate, taken, cfg):
    """Check a candidate against live and queued reservations in the active profile."""
    posting_cfg = (cfg or {}).get("posting", {})
    gap = timedelta(hours=float(posting_cfg.get("min_gap_hours", 3)))
    target = int(posting_cfg.get("daily_post_target", 2))
    day = local_day(candidate, cfg)
    return (all(candidate != other and abs(candidate - other) >= gap for other in taken)
            and sum(local_day(other, cfg) == day for other in taken) < target)


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
    search_days = int(posting_cfg.get("max_slot_search_days", 60))
    taken = reserved_times(db_path, exclude_item_id=exclude_item_id)

    for candidate in slot_candidates(after, cfg, days=search_days):
        if slot_available(candidate, taken, cfg):
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


def variant_probabilities(cfg: dict[str, Any] | None = None, db_path: Any = None, arms=None) -> tuple[dict[str, float], str]:
    """({'A': p, 'B': p}, why). The single place the allocation rule lives; analytics stores the
    same numbers in ab_learning.weight."""
    auto = ((cfg or {}).get("autopilot") or {})
    if arms:
        even = dict.fromkeys(arms, 1 / len(arms))
        minimum = int(auto.get("bandit_min_samples_per_variant", 3))
        stats = variant_stats(db_path)
        if in_week_one(cfg, db_path) or any(stats.get(arm, {}).get("n", 0) < minimum for arm in arms):
            return even, "uniform preset exploration"
        best = max(stats[arm]["score"] for arm in arms)
        leaders = [arm for arm in arms if abs(stats[arm]["score"] - best) < 1e-9]
        floor = max(0, min(float(auto.get("bandit_min_exploration_floor", 0.20)), 1 / len(arms)))
        bonus = (1 - floor * len(arms)) / len(leaders)
        return {arm: floor + (bonus if arm in leaders else 0) for arm in arms}, "preset bandit"
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


def pinned_variant(item: dict[str, Any]) -> str | None:
    """The variant a reviewer pinned with "Approve A only" / "Approve B only", or None when the
    variant is left to the draw.

    A pinned item (ab_excluded, or a single-variant approval_type) keeps its own chosen_variant.
    If that was never recorded it falls back to the approval_type's variant, and only then to A.
    """
    import json

    versions = json.loads(item.get("approved_versions") or "[]")
    if versions:
        return versions[0] if len(versions) == 1 else None
    approval = item.get("approval_type")
    if not (int(item.get("ab_excluded") or 0) or approval in PINNED_APPROVAL_TYPES):
        return None
    chosen = str(item.get("chosen_variant") or "").strip().upper()
    if chosen in ("A", "B"):
        return chosen
    return PINNED_APPROVAL_TYPES.get(approval, "A")


def choose_variant(
    item: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    rng: random.Random | None = None,
) -> tuple[str, str]:
    """(variant, why) for one approved item. A pinned item (A only / B only) keeps its variant."""
    pinned = pinned_variant(item)
    if pinned:
        return pinned, f"approved {pinned} only: stays {pinned}, excluded from the A/B comparison"
    import json

    versions = json.loads(item.get("approved_versions") or "[]")
    probs, why = variant_probabilities(cfg, db_path, versions)
    if versions:
        roll = (rng or random).random()
        for variant, probability in probs.items():
            roll -= probability
            if roll < 0:
                return variant, why
        return versions[-1], why
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
        if any(r.get("post_now") for r in rows):
            # Post now means "as soon as allowed": never silently pushed to a later grid slot.
            # The posting check holds it for the gap, cap or a Meta pause and says why on the panel.
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
                        variant_decided_at = CASE WHEN status = 'live' THEN variant_decided_at ELSE NULL END,
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
        post_progress.ensure_run(item["id"], "scheduled", f"Slot {old_slot} reached", db_path=db_path,
                                 slot=old_slot)
        post_progress.record(item["id"], "checks", "waiting",
                             f"Missed its slot {old_slot} by more than the grace; moved to {new_slot} UTC.",
                             db_path=db_path, hold="missed", moved_to=new_slot)
        log.info("scheduler: %s missed %s, moved to %s", item["id"], old_slot, new_slot)
        moved.append((item["id"], old_slot, new_slot))
    return moved


def schedule_approved(
    now: datetime,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    rng: random.Random | None = None,
) -> list[dict[str, Any]]:
    """Give every approved item the next free slot and move it to `queued`.

    The variant is left undecided (posting.UNDECIDED_VARIANT) unless the item is pinned (A only /
    B only), which keeps its chosen_variant; it is drawn when the slot comes due, so it always
    uses the latest learning."""
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
            pinned = pinned_variant(item)
            item["chosen_variant"] = pinned
            why = (f"approved {pinned} only: stays {pinned}" if pinned
                   else "drawn when the slot comes due, from the learning at that time")
            slot = clock.iso(next_free_slot(now, cfg, db_path, exclude_item_id=item_id))
            # Order matters for crash safety: the rows are created (idempotently) first and the
            # stage moves last, so a crash in between re-runs this block and changes nothing.
            rows = posting.ensure_post_rows(item, slot_datetime=slot, cfg=cfg, db_path=db_path)
            slot = posting.item_slot(rows) or slot           # rows from an earlier attempt keep theirs
            variant = rows[0]["variant"] if rows else posting.UNDECIDED_VARIANT
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
