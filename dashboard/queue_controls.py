"""Queue changes that preserve the scheduler's gap and daily cap."""

from datetime import datetime, time, timedelta

from pipeline import clock, db, posting, scheduler


def limits(cfg):
    settings = cfg.get("posting", {})
    return timedelta(hours=float(settings.get("min_gap_hours", 3))), min(2, int(settings.get("daily_post_target", 2)))


def allowed(candidate, taken, cfg):
    gap, cap = limits(cfg)
    day = scheduler.local_day(candidate, cfg)
    return (all(candidate != other and abs(candidate - other) >= gap for other in taken)
            and sum(scheduler.local_day(other, cfg) == day for other in taken) < cap)


def earliest(cfg, path, item_id):
    candidate = clock.utcnow().replace(microsecond=0)
    taken = scheduler.reserved_times(path, item_id)
    gap, cap = limits(cfg)
    tz = scheduler.active_profile(cfg)[1]
    horizon = candidate + timedelta(days=int(cfg.get("posting", {}).get("max_slot_search_days", 60)))
    while candidate <= horizon:
        day = candidate.astimezone(tz).date()
        if sum(t.astimezone(tz).date() == day for t in taken) >= cap:
            candidate = datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(candidate.tzinfo)
            continue
        conflicts = [t for t in taken if t == candidate or abs(candidate - t) < gap]
        if not conflicts:
            return candidate
        candidate = max(conflicts) + max(gap, timedelta(seconds=1))
    raise ValueError("No allowed slot within the posting search horizon.")


def move(item_id, direction, cfg, path):
    from .commands import ControlError, unstarted

    with db.db(path) as conn:
        rows = conn.execute(
            "SELECT i.id, MIN(p.slot_datetime) AS slot FROM items i JOIN posts p ON p.item_id=i.id "
            "WHERE i.stage='queued' GROUP BY i.id ORDER BY slot, i.id"
        ).fetchall()
    order = [row["id"] for row in rows]
    index = order.index(item_id) + direction
    if not 0 <= index < len(order):
        raise ControlError("The item is already at that end of the queue.")
    other = rows[index]
    if not unstarted(posting.get_posts_for_item(other["id"], path)):
        raise ControlError("Publishing has already started for the neighbouring item.")
    slot = posting.item_slot(posting.get_posts_for_item(item_id, path))
    taken = scheduler.reserved_times(path, item_id)
    taken.remove(clock.parse(other["slot"]))
    first, second = clock.parse(slot), clock.parse(other["slot"])
    if not allowed(first, taken + [second], cfg) or not allowed(second, taken + [first], cfg):
        raise ControlError("Swapping these slots would violate the posting gap or daily cap.")
    set_slot(item_id, other["slot"], path)
    set_slot(other["id"], slot, path)
    return f"Moved to {second.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}."


def set_slot(item_id, slot, path, post_now=False):
    """Give an unstarted item a new slot (callers validate `unstarted` first).

    A row a dry run walked keeps nothing from it: its fake container id, its attempt count and
    its publish time are cleared, so a live pass creates a real container instead of uploading
    to "<dry-run-container:N>". `post_now` marks a slot the missed-slot rule must never move."""
    with db.db(path) as conn:
        conn.execute(
            "UPDATE posts SET slot_datetime=?, status='pending', variant_decided_at=NULL, updated_at=?, "
            "remote_container_id=NULL, attempt_count=0, publish_started_at=NULL, error_message=NULL, "
            "no_retry=0, post_now=? WHERE item_id=? AND status NOT IN ('live', 'publishing')",
            (slot, db.now(), int(bool(post_now)), item_id),
        )


def scheduled(path):
    with db.db(path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT i.id, MIN(p.slot_datetime) AS slot FROM items i JOIN posts p ON p.item_id=i.id "
            "WHERE i.stage='queued' GROUP BY i.id HAVING slot IS NOT NULL ORDER BY slot, i.id"
        )]


def reposition(item_id, target_id, expected, cfg, path):
    """Reuse the allocated slots after validating every item and reservation."""
    from .commands import ControlError, publishing_block_reason, unstarted

    rows = scheduled(path)
    if rows != expected:
        raise ControlError("The schedule changed. Refresh and try the move again.")
    order = [row["id"] for row in rows]
    old, new = order.index(item_id), order.index(target_id)
    order.insert(new, order.pop(old))
    left, right = sorted((old, new))
    reserved = scheduler.reserved_times(path)
    for index in range(left, right + 1):
        previous = rows[index]
        item = db.get_item(previous["id"], path)
        title = item.get("original_title") or previous["id"]
        if not db.is_releasable(item):
            raise ControlError(f"“{title}” is preview-only.")
        posts = posting.get_posts_for_item(previous["id"], path)
        if not unstarted(posts):
            reason = publishing_block_reason(posts)
            raise ControlError(f"“{title}” cannot move. {reason}")
        reserved.remove(clock.parse(previous["slot"]))
    for index in range(left, right + 1):
        slot = clock.parse(rows[index]["slot"])
        if not allowed(slot, reserved, cfg):
            raise ControlError("The resulting schedule would violate the posting gap or daily cap.")
        reserved.append(slot)
    for index in range(left, right + 1):
        set_slot(order[index], rows[index]["slot"], path)
    return f"Moved to position {new + 1}."
