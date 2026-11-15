"""Queue planning and caption validation used by confirmation and application."""
import re
from datetime import timedelta

from pipeline import clock, db, posting, scheduler


def neighbour(item_id, direction, path):
    with db.db(path) as conn:
        queue = conn.execute(
            "SELECT i.id, MIN(p.slot_datetime) AS slot FROM items i JOIN posts p ON p.item_id=i.id "
            "WHERE i.stage='queued' GROUP BY i.id ORDER BY slot, i.id"
        ).fetchall()
    for index, row in enumerate(queue):
        if row["id"] == item_id:
            target = index + direction
            if 0 <= target < len(queue):
                return queue[target]["id"], row["slot"], queue[target]["slot"]
    raise ValueError("Already at the edge of the queue.")


def swap(item_id, direction, path, cfg, unstarted, expected=None):
    other, here, there = neighbour(item_id, direction, path)
    if expected is not None and [other, here, there] != expected:
        raise ValueError("The queue changed. Confirm the new order again.")
    if not db.is_releasable(db.get_item(other, path)) or not unstarted(posting.get_posts_for_item(other, path)):
        raise ValueError("The neighbouring item has already started posting.")
    a, b = clock.parse(here), clock.parse(there)
    times = scheduler.reserved_times(path, item_id)
    times.remove(b)
    if not scheduler.slot_allowed(a, [*times, b], cfg) or not scheduler.slot_allowed(b, [*times, a], cfg):
        raise ValueError("The swap would breach the minimum gap or two-per-day limit.")
    return [other, here, there]


def next_post(item_id, cfg, path):
    paused, why = posting.meta_paused(path)
    if paused:
        raise ValueError(f"Meta posting is paused. {why}")
    if not posting.enabled_platforms(cfg):
        raise ValueError("Enable at least one posting platform first.")
    now = clock.utcnow()
    return scheduler.earliest_slot(now.replace(microsecond=0) + timedelta(seconds=1), cfg, path, item_id)


def caption_fields(item, payload):
    body, tags = payload.get("body"), payload.get("hashtags")
    if not isinstance(body, str) or not isinstance(tags, str):
        raise ValueError("Caption and hashtags must be text.")
    tags = " ".join(tags.split())
    if any(not re.fullmatch(r"#[\w]+", tag) for tag in tags.split()):
        raise ValueError("Use space-separated hashtags such as #gta6 #gaming.")
    caption = posting.override_caption(item, body, tags)
    if len(caption.encode("utf-16-le")) // 2 > posting.IG_CAPTION_LIMIT:
        raise ValueError("Caption must be at most 2,200 characters including credit and disclosure.")
    if len(re.findall(r"#[\w]+", caption)) > 30:
        raise ValueError("Caption must contain at most 30 hashtags.")
    return body.strip(), tags, caption
