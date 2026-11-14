"""Stage 8: Meta (Instagram + Facebook Page) Reels publishing, two-phase and crash-safe (CP3).

Endpoints (Meta Graph API, the flows the author's earlier LoudGraph pipeline publishes with today -
see `pipeline/publish_instagram.py` there; examples use v25.0):

  Instagram Reels, resumable upload from a local file:
    POST /{ig-user-id}/media            media_type=REELS, upload_type=resumable, caption,
                                        share_to_feed -> container id
    POST https://rupload.facebook.com/ig-api-upload/{ver}/{container-id}
         headers Authorization: OAuth <token>, offset: 0, file_size: <bytes>; body = the file bytes
    GET  /{container-id}?fields=status_code  -> FINISHED | IN_PROGRESS | ERROR | EXPIRED | PUBLISHED
    POST /{ig-user-id}/media_publish    creation_id=<container-id> -> media id

  Facebook Page Reels, video_reels start / upload / finish:
    POST /{page-id}/video_reels         upload_phase=start -> video_id, upload_url
    POST https://rupload.facebook.com/video-upload/{ver}/{video-id}   (same headers)
    POST /{page-id}/video_reels         upload_phase=finish, video_id, video_state=PUBLISHED,
                                        description
    GET  /{video-id}?fields=status,permalink_url

TWO-PHASE AND CRASH-SAFE (PLAN.md section 9)
  Phase 1  container / upload session is created and its id is written to `posts.remote_container_id`
           BEFORE anything else happens:  uploading -> container_ready
  Phase 2  the bytes go up, the row is moved to `publishing` and the publish time is recorded
           BEFORE the publish call is issued:  container_ready -> publishing -> live

  A row found in `publishing` on resume is never published again blindly. `remote_state()` asks the
  remote first - the container's status and the account's recent media - and the row is only:
    * `live`          when the remote shows it published;
    * restarted       only when the remote proves nothing was published (the container is ERROR /
                      EXPIRED, or the Facebook video failed), because then a publish is impossible;
    * `failed` + alert for anything else, INCLUDING a container that merely reads FINISHED. A
                      FINISHED container cannot tell a publish that never happened from one whose
                      media has not yet appeared in /media, so that state counts as unknown and is
                      never re-published.

  An item is marked `posted` only when every enabled platform row is `live`; a failed Facebook row
  therefore retries Facebook alone and leaves a live Instagram row untouched.

DRY RUN (`app.dry_run`, true by default)
  `posting.dry_run_mode: plan_only` (the default) sends NO write request at all: the whole state
  machine runs, every request that would go out is logged and written to a plan file, and the row
  ends as `dry_run`.
  `posting.dry_run_mode: up_to_publish` additionally creates the real container and uploads the real
  bytes, and skips only the publish call. That touches the live account, so it is opt-in.

RATE LIMITS
  Every request is paced to `posting.meta_min_gap_seconds` through `kv`, so the gap holds across
  processes. A 429 or a Graph rate-limit / block code pauses `meta_graph` for
  `posting.rate_limit_pause_hours` through `rate_limit`, and the pause is checked before a run.
  5xx and network errors are retried with capped exponential backoff.

Network sits behind the `Transport` interface, so the tests are fully mocked and make no calls.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from . import clock, config, db, rate_limit
from .errors import PublishError, RateLimitError

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
RUPLOAD_BASE = "https://rupload.facebook.com"
API_VERSION = "v25.0"
META_SOURCE = rate_limit.META_SOURCE   # the key `rate_limits` pauses under
K_NEXT_FREE = "meta_api:next_free"  # epoch seconds: the earliest moment the next request may be sent
MAX_PACING_WAIT_S = 120.0

# Graph error codes that mean "you are being limited / blocked", not "this request was wrong".
RATE_LIMIT_CODES = {4, 17, 32, 613, 80004}
IG_CAPTION_LIMIT = 2200

PLATFORM_INSTAGRAM = "instagram"
PLATFORM_FACEBOOK = "facebook"

# posts.status values of the two-phase machine, in order.
STATUS_PENDING = "pending"
STATUS_UPLOADING = "uploading"
STATUS_CONTAINER_READY = "container_ready"
STATUS_PUBLISHING = "publishing"
STATUS_LIVE = "live"
STATUS_FAILED = "failed"
STATUS_DRY_RUN = "dry_run"

# Rows that still have work to do: cleanup must not delete their media, and their slot is taken.
OPEN_STATUSES = (STATUS_PENDING, STATUS_UPLOADING, STATUS_CONTAINER_READY, STATUS_PUBLISHING)
# A row created at queue time carries this until its slot comes due and the variant is drawn
# (decide_variant). It is never a renderable variant, so it can never be uploaded by mistake.
UNDECIDED_VARIANT = "?"
# Once a row holds any of these (or a container id), the variant is frozen for the whole item.
VARIANT_FROZEN_STATUSES = (STATUS_UPLOADING, STATUS_CONTAINER_READY, STATUS_PUBLISHING, STATUS_LIVE)
# Rows whose publish has (or may have) reached the remote: such an item is never re-slotted.
STARTED_STATUSES = (STATUS_PUBLISHING, STATUS_LIVE)

HASHTAGS = "#gta6 #grandtheftauto #gta #gaming #rockstargames"

# Creator-credit templates per source (PLAN.md section 9). `posting.caption_templates` in
# config.yaml overrides them. The lone "." keeps Instagram from folding the hashtags into the
# visible first lines. Reddit was dropped as a source (Decision A), so it has no template.
CAPTION_TEMPLATES = {
    "youtube": "{hook_text}\n\nCredit: {author_name}\n.\n{hashtags}",
    "twitch": "{hook_text}\n\nClipped from {author_name} on Twitch\ntwitch.tv/{channel}\n.\n{hashtags} #twitchclips",
}
DEFAULT_CAPTION_SOURCE = "youtube"


# ---------------------------------------------------------------------------
# Caption template
# ---------------------------------------------------------------------------

def twitch_channel(author_name: str) -> str:
    """The channel part of a twitch.tv link: the login is the display name lower-cased, no spaces."""
    return re.sub(r"[^a-z0-9_]", "", (author_name or "").lower())


def build_caption(
    hook_text: str,
    author_name: str,
    source: str = DEFAULT_CAPTION_SOURCE,
    hashtags: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """The caption with creator credit (PLAN.md section 9), from the template for `source`.

    YouTube: hook, blank line, "Credit: <creator>", ".", hashtags.
    Twitch:  hook, blank line, "Clipped from <streamer> on Twitch", the channel link, ".", hashtags.
    An unknown source uses the YouTube template.
    """
    posting_cfg = (cfg or {}).get("posting") or {}
    templates = {**CAPTION_TEMPLATES, **(posting_cfg.get("caption_templates") or {})}
    template = templates.get(source) or templates[DEFAULT_CAPTION_SOURCE]
    author = author_name or "Creator"
    channel = twitch_channel(author)
    if source == "twitch" and not channel:
        # A display name with no ASCII login (e.g. non-Latin): credit without a broken link.
        template = template.replace("twitch.tv/{channel}\n", "")
    caption = template.format(
        hook_text=(hook_text or "").strip(),
        author_name=author,
        channel=channel,
        hashtags=hashtags or posting_cfg.get("hashtags") or HASHTAGS,
    )
    return caption[:IG_CAPTION_LIMIT]


def caption_for_item(item: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """The caption to send for an item: the stored one, else built from the template."""
    stored = (item.get("caption_text") or "").strip()
    if stored:
        return stored[:IG_CAPTION_LIMIT]
    return build_caption(
        item.get("hook_text") or "",
        item.get("author_name") or "Creator",
        item.get("source") or DEFAULT_CAPTION_SOURCE,
        cfg=cfg,
    )


# ---------------------------------------------------------------------------
# Network interface (the only place a Meta request is sent from)
# ---------------------------------------------------------------------------

class Transport(Protocol):
    """Sends one HTTP request and returns a response-like object. Tests supply a fake."""

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        ...


class RequestsTransport:
    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        fn = requests.get if method == "GET" else requests.post
        return fn(url, **kwargs)


def redact(text: Any) -> str:
    """A Graph URL carries access_token in the query string; never let it reach a log or the DB."""
    out = re.sub(r"(access_token=)[^&\s'\"]+", r"\1***", str(text))
    for key in ("IG_ACCESS_TOKEN", "FB_PAGE_ACCESS_TOKEN"):
        tok = config.secret(key)
        if tok:
            out = out.replace(tok, f"<{key}>")
    return out


def _redact_params(params: dict[str, Any]) -> dict[str, Any]:
    return {k: ("<ACCESS_TOKEN>" if k == "access_token" else v) for k, v in params.items()}


class MetaClient:
    """Paced, retrying, token-redacting access to the Graph API and rupload.

    Every request waits until `min_gap_s` have passed since the previous one. That moment lives in
    `kv`, so the gap holds across processes (the publisher, the bot and a doctor check share it) -
    the approach used in LoudGraph `pipeline/meta_api.py`.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        cfg: dict[str, Any] | None = None,
        db_path: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg or config.get_config()
        posting = self.cfg.get("posting") or {}
        self.transport: Transport = transport or RequestsTransport()
        self.db_path = db_path
        self.clock = clock
        self.sleep = sleep
        self.min_gap_s = float(posting.get("meta_min_gap_seconds", 3.0))
        self.max_retries = int(posting.get("max_retries", 3))
        self.retry_backoff_s = float(posting.get("retry_backoff_s", 2.0))
        self.retry_backoff_cap_s = float(posting.get("retry_backoff_cap_s", 30.0))
        self.pause_hours = float(posting.get("rate_limit_pause_hours", 3.0))
        self.api_version = str(posting.get("api_version", API_VERSION))
        self.graph_base = str(posting.get("graph_base", GRAPH_BASE))
        self.rupload_base = str(posting.get("rupload_base", RUPLOAD_BASE))

    # ------------------------------------------------------------- pacing
    def base(self) -> str:
        return f"{self.graph_base}/{self.api_version}"

    def _reserve(self, now: float) -> float:
        """The moment this request may be sent, moving the shared mark on for the next caller."""
        if self.min_gap_s <= 0:
            return now
        with db.db(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT value FROM kv WHERE key = ?", (K_NEXT_FREE,)).fetchone()
                try:
                    free = float(row["value"]) if row and row["value"] else 0.0
                except (TypeError, ValueError):
                    free = 0.0
                turn = max(now, min(free, now + MAX_PACING_WAIT_S))
                conn.execute(
                    "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (K_NEXT_FREE, repr(turn + self.min_gap_s), db.now()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return turn

    def wait_turn(self) -> float:
        now = self.clock()
        try:
            wait = self._reserve(now) - now
        except sqlite3.Error as e:
            wait = self.min_gap_s  # the pacing must never be the reason a post fails
            log.warning("meta: could not read the time of the last request (%s); waiting %.1fs", e, wait)
        if wait > 0:
            self.sleep(wait)
        return max(wait, 0.0)

    # ------------------------------------------------------------- sending
    def request(self, method: str, url: str, *, retry: bool = True, **kwargs: Any) -> Any:
        """One paced request, retried on 5xx and network errors with capped exponential backoff.

        retry=False is for the publish calls: they are not idempotent, and a lost response may
        mean the post went out, so they are sent once and the caller settles from the remote."""
        attempt = 0
        while True:
            self.wait_turn()
            try:
                resp = self.transport(method, url, **kwargs)
            except requests.RequestException as e:
                e.args = tuple(redact(a) for a in e.args) or (redact(e),)
                if not retry or attempt >= self.max_retries:
                    raise
                self._backoff(attempt, f"network error: {redact(e)}")
                attempt += 1
                continue
            status = int(getattr(resp, "status_code", 200))
            if retry and status >= 500 and attempt < self.max_retries:
                self._backoff(attempt, f"HTTP {status}")
                attempt += 1
                continue
            return resp

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(self.retry_backoff_s * (2 ** attempt), self.retry_backoff_cap_s)
        log.warning("meta: %s; retrying in %.1fs (attempt %d/%d)", why, delay, attempt + 1, self.max_retries)
        self.sleep(delay)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    # ------------------------------------------------------------- answers
    def check(self, resp: Any, what: str) -> dict[str, Any]:
        """The JSON body of a successful response. A rate limit or block raises RateLimitError."""
        status = int(getattr(resp, "status_code", 200))
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {"data": data}
        error = data.get("error") or {}
        code = error.get("code")
        if status == 429 or (code in RATE_LIMIT_CODES):
            self.pause(f"{what}: HTTP {status} {redact(error or data)}"[:400])
        if status != 200 or error:
            body = redact(error or getattr(resp, "text", "") or data)
            raise PublishError(f"{what}: HTTP {status} {str(body)[:300]}")
        return data

    def pause(self, message: str) -> None:
        """A 429 or block holds every Meta call back for `posting.rate_limit_pause_hours`."""
        rate_limit.record_error(META_SOURCE, message, pause_hours=self.pause_hours, db_path=self.db_path)
        raise RateLimitError(META_SOURCE, self.pause_hours * 3600.0, message)


def meta_paused(db_path: Any = None) -> tuple[bool, str]:
    return rate_limit.is_source_paused(META_SOURCE, db_path=db_path)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def ig_token() -> str:
    return config.secret("IG_ACCESS_TOKEN")


def fb_token() -> str:
    """The Page token for Facebook Page Reels; IG_ACCESS_TOKEN when it is itself a Page token."""
    return config.secret("FB_PAGE_ACCESS_TOKEN") or config.secret("IG_ACCESS_TOKEN")


def graph_time(ts: str | None) -> str | None:
    """A Graph time ("2026-11-09T09:43:52+0000") as this database writes times."""
    try:
        return datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# posts rows
# ---------------------------------------------------------------------------

def enabled_platforms(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or config.get_config()
    platforms = ((cfg.get("posting") or {}).get("platforms") or {})
    return [p for p in (PLATFORM_INSTAGRAM, PLATFORM_FACEBOOK) if platforms.get(p)]


def get_post(post_id: int, db_path: Any = None) -> dict[str, Any] | None:
    with db.db(db_path) as conn:
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        return dict(row) if row else None


def get_posts_for_item(item_id: str, db_path: Any = None) -> list[dict[str, Any]]:
    with db.db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE item_id = ? ORDER BY platform ASC", (item_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_posts_by_status(status: str, db_path: Any = None) -> list[dict[str, Any]]:
    with db.db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY id ASC", (status,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_post(post_id: int, db_path: Any = None, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = db.now()
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    params = dict(fields, id=post_id)
    with db.db(db_path) as conn:
        conn.execute(f"UPDATE posts SET {sets} WHERE id = :id", params)


def set_status(post_id: int, status: str, db_path: Any = None, **fields: Any) -> None:
    update_post(post_id, db_path=db_path, status=status, **fields)
    log.info("post %s -> %s%s", post_id, status, f" ({fields})" if fields else "")


def ensure_post_rows(
    item: dict[str, Any],
    slot_datetime: str | None = None,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> list[dict[str, Any]]:
    """One `pending` row per enabled platform, created once (UNIQUE(item_id, platform)).

    The variant is NOT drawn here: rows start as UNDECIDED_VARIANT and `decide_variant` draws
    it when the slot comes due, from the learning as it is then. Only a variant already fixed
    on the item (Approve A only) is written at once.
    """
    cfg = cfg or config.get_config()
    refusal = refusal_reason(item, cfg)
    if refusal:
        raise PublishError(refusal)
    fixed = (item.get("chosen_variant") or "").upper()
    variant = fixed if fixed in ("A", "B") else UNDECIDED_VARIANT
    slot = slot_datetime or db.now()
    caption = caption_for_item(item, cfg)
    ts = db.now()
    with db.db(db_path) as conn:
        for platform in enabled_platforms(cfg):
            conn.execute(
                """
                INSERT INTO posts (
                    item_id, platform, variant, hook_style, source, authenticity, style_preset,
                    slot_datetime, status, ab_excluded, caption_text, created_at, updated_at,
                    variant_decided_at
                ) VALUES (
                    :item_id, :platform, :variant, :hook_style, :source, :authenticity, :style_preset,
                    :slot_datetime, :status, :ab_excluded, :caption_text, :created_at, :updated_at,
                    :variant_decided_at
                )
                ON CONFLICT(item_id, platform) DO NOTHING
                """,
                {
                    "item_id": item["id"],
                    "platform": platform,
                    "variant": variant,
                    "hook_style": item.get("hook_style") or "unknown",
                    "source": item.get("source") or "unknown",
                    "authenticity": item.get("authenticity") or "unverified",
                    "style_preset": item.get("style_preset") or "P0",
                    "slot_datetime": slot,
                    "status": STATUS_PENDING,
                    "ab_excluded": int(item.get("ab_excluded") or 0),
                    "caption_text": caption,
                    "created_at": ts,
                    "updated_at": ts,
                    "variant_decided_at": ts if variant != UNDECIDED_VARIANT else None,
                },
            )
    return get_posts_for_item(item["id"], db_path=db_path)


def variant_is_frozen(rows: list[dict[str, Any]]) -> bool:
    """A row has begun uploading (or holds a container): the item's variant can never change."""
    return any(r["status"] in VARIANT_FROZEN_STATUSES or r.get("remote_container_id") for r in rows)


def decide_variant(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    rng: Any = None,
) -> str | None:
    """Draw the item's A/B variant NOW - when its slot has come and it is about to be posted -
    from the week-1 status and the `ab_learning` cache as they are at this moment.

    Idempotent and crash-safe: rows that already carry a decision keep it, and once any row has
    begun (VARIANT_FROZEN_STATUSES) the variant is frozen for the item. Returns the variant, or
    None if the item has no rows.
    """
    from . import scheduler   # lazy: scheduler imports this module

    rows = get_posts_for_item(item_id, db_path=db_path)
    if not rows:
        return None
    decided = [r for r in rows if r.get("variant_decided_at") and r["variant"] in ("A", "B")]
    frozen = variant_is_frozen(rows)
    if decided and (frozen or len(decided) == len(rows)):
        variant, why = decided[0]["variant"], "already decided"
    elif frozen:
        # A row began before decisions were recorded (an older database): keep what it uses.
        started = next(r for r in rows if r["status"] in VARIANT_FROZEN_STATUSES or r.get("remote_container_id"))
        if started["variant"] not in ("A", "B"):
            # Never guess what an already-begun row used; video_for_post refuses to upload it.
            log.error("item %s: a row began with no known variant; leaving it undecided", item_id)
            return None
        variant, why = started["variant"], "frozen: a row had already begun"
    else:
        item = db.get_item(item_id, db_path=db_path) or {"id": item_id}
        variant, why = scheduler.choose_variant(item, cfg, db_path, rng)
    ts = db.now()
    with db.db(db_path) as conn:
        conn.execute(
            "UPDATE posts SET variant = ?, variant_decided_at = ?, updated_at = ? "
            "WHERE item_id = ? AND (variant_decided_at IS NULL OR variant NOT IN ('A', 'B'))",
            (variant, ts, ts, item_id),
        )
        conn.execute("UPDATE items SET chosen_variant = ?, updated_at = ? WHERE id = ?", (variant, ts, item_id))
    if why != "already decided":
        db.log_stage(item_id, "scheduler", "success", message=f"variant {variant} drawn at the slot ({why})",
                     db_path=db_path)
        log.info("variant for %s drawn at its slot: %s (%s)", item_id, variant, why)
    return variant


def is_dry_run(cfg: dict[str, Any] | None) -> bool:
    return bool(((cfg or {}).get("app") or {}).get("dry_run", True))


def refusal_reason(item: dict[str, Any], cfg: dict[str, Any] | None = None) -> str | None:
    """Why this item may not get `posts` rows, or None. Checked explicitly, before any write."""
    if int(item.get("preview_only") or 0):
        return f"item {item['id']} is preview_only and is never posted"
    if db.is_synthetic(item) and not is_dry_run(cfg):
        return f"item {item['id']} is a synthetic dry-run item and is never posted for real"
    return None


def max_post_attempts(cfg: dict[str, Any] | None) -> int:
    return int((((cfg or {}).get("posting") or {}).get("max_post_attempts", 3)))


def is_terminal_failure(post: dict[str, Any], cfg: dict[str, Any] | None = None) -> bool:
    """A failed row that will never be tried again: flagged no_retry, or out of attempts."""
    if post.get("status") != STATUS_FAILED:
        return False
    return bool(post.get("no_retry")) or int(post.get("attempt_count") or 0) >= max_post_attempts(cfg)


def item_has_open_posts(item_id: str, cfg: dict[str, Any] | None = None, db_path: Any = None) -> bool:
    """True while any row of the item is pending / mid-publish or still has retries left.
    Cleanup uses this: the media such a row needs is never deleted."""
    for post in get_posts_for_item(item_id, db_path=db_path):
        if post["status"] in OPEN_STATUSES:
            return True
        if post["status"] == STATUS_FAILED and not is_terminal_failure(post, cfg):
            return True
    return False


def missed_slot_grace(cfg: dict[str, Any] | None) -> float:
    """Seconds after its slot that a not-yet-started post may still go out."""
    return float((((cfg or {}).get("posting") or {}).get("missed_slot_grace_minutes", 90))) * 60.0


def item_slot(rows: list[dict[str, Any]]) -> str | None:
    slots = [r["slot_datetime"] for r in rows if r.get("slot_datetime")]
    return min(slots) if slots else None


def slot_is_missed(rows: list[dict[str, Any]], now: datetime, cfg: dict[str, Any] | None = None) -> bool:
    """The slot passed more than the grace ago and nothing has (or may have) been published.

    Such an item is not posted late: the scheduler moves it to the next free slot. Once any row
    is `publishing` or `live` the post is underway and finishes where it is.
    """
    slot = item_slot(rows)
    if not slot or any(r["status"] in STARTED_STATUSES for r in rows):
        return False
    return (now - clock.parse(slot)).total_seconds() > missed_slot_grace(cfg)


def reset_dry_run_rows(cfg: dict[str, Any] | None = None, db_path: Any = None) -> int:
    """Going live: rows a dry run finished as `dry_run` become `pending` again.

    Without this, flipping app.dry_run to false would find every queued item already "done" and it
    would never be posted. Their slots are in the past by then, so the scheduler re-slots them one
    per slot - they do not all go out at once. Does nothing while app.dry_run is true.
    """
    if is_dry_run(cfg):
        return 0
    with db.db(db_path) as conn:
        cur = conn.execute(
            f"""
            UPDATE posts SET status = 'pending', remote_container_id = NULL, publish_started_at = NULL,
                             attempt_count = 0, error_message = 'reset after dry run', updated_at = ?,
                             variant_decided_at = NULL
            WHERE status = 'dry_run'
              AND item_id IN (SELECT id FROM items WHERE stage IN ('approved', 'queued') AND {db.RELEASABLE_SQL})
            """,
            (db.now(),),
        )
        return cur.rowcount


def video_for_post(item: dict[str, Any], post: dict[str, Any]) -> Path:
    variant = (post.get("variant") or "").upper()
    if variant not in ("A", "B"):
        raise PublishError(f"item {item['id']}: the variant has not been decided (row holds {variant!r})")
    path = item.get("render_a_path") if variant == "A" else item.get("render_b_path")
    if not path:
        raise PublishError(f"item {item['id']} has no variant {variant} render")
    video = Path(path)
    if not video.exists():
        raise PublishError(f"variant {variant} render missing on disk: {video}")
    return video


# ---------------------------------------------------------------------------
# Dry run plan
# ---------------------------------------------------------------------------

class Plan:
    """What a dry run would have sent. Logged, and written next to the renders."""

    def __init__(self, post_id: int, platform: str):
        self.post_id = post_id
        self.platform = platform
        self.steps: list[dict[str, Any]] = []

    def add(self, method: str, url: str, **detail: Any) -> None:
        step = {"method": method, "url": redact(url)}
        if "params" in detail:
            step["params"] = _redact_params(detail.pop("params"))
        if "headers" in detail:
            step["headers"] = {
                k: ("OAuth <ACCESS_TOKEN>" if k.lower() == "authorization" else v)
                for k, v in detail.pop("headers").items()
            }
        step.update(detail)
        self.steps.append(step)
        log.info("DRY RUN %s/%s would send: %s %s %s", self.platform, self.post_id, method, step["url"],
                 json.dumps({k: v for k, v in step.items() if k not in ("method", "url")})[:600])

    def write(self, directory: Path | None = None) -> Path | None:
        try:
            out_dir = directory or config.media_dir("dry_run")
            out = Path(out_dir) / f"post_{self.post_id}_{self.platform}.json"
            out.write_text(
                json.dumps({"dry_run": True, "platform": self.platform, "steps": self.steps}, indent=1),
                encoding="utf-8",
            )
            return out
        except OSError as e:
            log.warning("could not write the dry-run plan: %s", e)
            return None


# ---------------------------------------------------------------------------
# The two-phase publisher
# ---------------------------------------------------------------------------

class Publisher:
    """Publishes one `posts` row at a time through the two-phase, crash-safe state machine."""

    def __init__(
        self,
        client: MetaClient | None = None,
        cfg: dict[str, Any] | None = None,
        db_path: Any = None,
        alert: Callable[[str, str | None], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        rng: Any = None,
    ):
        self.cfg = cfg or config.get_config()
        self.db_path = db_path
        self.rng = rng          # the A/B draw (decide_variant); None = the random module
        self.client = client or MetaClient(cfg=self.cfg, db_path=db_path, sleep=sleep, clock=clock)
        self.sleep = sleep
        self.clock = clock
        self._alert = alert
        posting = self.cfg.get("posting") or {}
        self.poll_seconds = float(posting.get("container_poll_seconds", 10.0))
        self.poll_max_minutes = float(posting.get("container_poll_max_minutes", 5.0))
        self.recent_media_limit = int(posting.get("recent_media_limit", 25))
        self.max_attempts = int(posting.get("max_post_attempts", 3))

    # ------------------------------------------------------------- helpers
    def dry_run(self) -> bool:
        return bool((self.cfg.get("app") or {}).get("dry_run", True))

    def dry_run_mode(self) -> str:
        return str(((self.cfg.get("posting") or {}).get("dry_run_mode") or "plan_only")).lower()

    def may_send(self, kind: str) -> bool:
        """`kind` is 'read', 'container', 'upload' or 'publish'."""
        if not self.dry_run():
            return True
        if kind == "read":
            return True
        if self.dry_run_mode() == "up_to_publish":
            return kind != "publish"
        return False

    def alert(self, text: str, key: str | None = None) -> None:
        """Failure-only alert into the approval chat. Never let alerting break a publish."""
        try:
            if self._alert:
                self._alert(text, key)
                return
            from . import telegram_bot
            if not (config.secret("TELEGRAM_BOT_TOKEN") and config.secret("TELEGRAM_CHAT_ID")):
                log.warning("ALERT (not delivered, no telegram credentials): %s", text)
                return
            telegram_bot.ApprovalBot(cfg=self.cfg, db_path=self.db_path).alert(text, key=key)
        except Exception as e:  # noqa: BLE001
            log.warning("alert not delivered (%s): %s", e, text)

    def fail(self, post: dict[str, Any], message: str, alert: bool = True, terminal: bool = False) -> str:
        """`terminal=True` flags the row no_retry: no later pass may touch it again."""
        msg = redact(message)[:900]
        extra = {"no_retry": 1} if terminal else {}
        set_status(post["id"], STATUS_FAILED, db_path=self.db_path, error_message=msg, **extra)
        db.log_stage(post["item_id"], "posting", "failure", message=f"{post['platform']}: {msg}", db_path=self.db_path)
        if alert:
            self.alert(
                f"{post['platform']} post failed for item {post['item_id']}: {msg}",
                key=f"post_failed:{post['id']}",
            )
        return STATUS_FAILED

    # ------------------------------------------------------------- entry
    def publish(self, post_id: int, item: dict[str, Any] | None = None) -> str:
        """Drive one row to `live` (or `dry_run` / `failed`). Resumes from whatever status it holds."""
        post = get_post(post_id, db_path=self.db_path)
        if not post:
            raise PublishError(f"no post row {post_id}")
        if post["status"] in (STATUS_LIVE, STATUS_DRY_RUN):
            return post["status"]
        if post["status"] == STATUS_FAILED and post.get("no_retry"):
            return STATUS_FAILED   # failed for good (e.g. unknown remote state): never sent again

        paused, why = meta_paused(self.db_path)
        if paused:
            log.warning("meta is paused, %s not sent: %s", post["platform"], why)
            return post["status"]

        if post["variant"] not in ("A", "B") or not post.get("variant_decided_at"):
            # The post is beginning now: this is when its slot came due, so draw the variant.
            decide_variant(post["item_id"], cfg=self.cfg, db_path=self.db_path, rng=self.rng)
            post = get_post(post_id, db_path=self.db_path) or post
            item = None
        item = item or db.get_item(post["item_id"], db_path=self.db_path)
        if not item:
            return self.fail(post, f"item {post['item_id']} is gone")

        attempts = int(post.get("attempt_count") or 0) + 1
        if attempts > self.max_attempts:
            return self.fail(
                post, f"gave up after {self.max_attempts} attempts (last status {post['status']})",
                terminal=True,
            )
        update_post(post_id, db_path=self.db_path, attempt_count=attempts)
        post["attempt_count"] = attempts

        # A row in `publishing` is the dangerous one: ask the remote before anything else.
        if post["status"] == STATUS_PUBLISHING:
            return self.settle(post, item)

        try:
            if post["platform"] == PLATFORM_INSTAGRAM:
                return self._publish_instagram(post, item)
            if post["platform"] == PLATFORM_FACEBOOK:
                return self._publish_facebook(post, item)
            return self.fail(post, f"unknown platform {post['platform']}")
        except RateLimitError as e:
            update_post(post_id, db_path=self.db_path, error_message=redact(e)[:900])
            log.warning("meta rate limited; %s left at %s", post["platform"], post["status"])
            self.alert(f"Meta paused for {self.client.pause_hours:.0f}h: {redact(e)[:300]}", key="meta_rate_limit")
            raise
        except (PublishError, requests.RequestException, OSError) as e:
            current = get_post(post_id, db_path=self.db_path) or post
            if current["status"] == STATUS_PUBLISHING:
                # The publish call itself went wrong: Meta may have published anyway (a lost
                # response). Never mark this retryable - leave it `publishing` so the next pass
                # settles it from the remote state, which never publishes twice (call #60).
                update_post(post_id, db_path=self.db_path, error_message=redact(f"{type(e).__name__}: {e}")[:900])
                log.warning("%s publish call failed for %s (%s); left publishing, to be settled from the remote",
                            post["platform"], post["item_id"], type(e).__name__)
                return STATUS_PUBLISHING
            return self.fail(post, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------- instagram
    def _publish_instagram(self, post: dict[str, Any], item: dict[str, Any]) -> str:
        post_id = post["id"]
        ig_user = config.secret("IG_USER_ID")
        token = ig_token()
        caption = post.get("caption_text") or caption_for_item(item)
        video = video_for_post(item, post)
        size = video.stat().st_size
        plan = Plan(post_id, PLATFORM_INSTAGRAM)
        base = self.client.base()

        if not self.dry_run() and not (ig_user and token):
            return self.fail(post, "IG_USER_ID / IG_ACCESS_TOKEN missing")
        ig_user = ig_user or "<IG_USER_ID>"

        container_id = post.get("remote_container_id")

        # ---- Phase 1: the container, saved BEFORE anything else happens.
        if post["status"] in (STATUS_PENDING, STATUS_UPLOADING) and not container_id:
            set_status(post_id, STATUS_UPLOADING, db_path=self.db_path, error_message=None)
            params = {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true",
                "access_token": token,
            }
            url = f"{base}/{ig_user}/media"
            if self.may_send("container"):
                data = self.client.check(self.client.post(url, data=params, timeout=60), "create REELS container")
                container_id = data["id"]
            else:
                plan.add("POST", url, params=params)
                container_id = f"<dry-run-container:{post_id}>"
            update_post(post_id, db_path=self.db_path, remote_container_id=container_id)
            set_status(post_id, STATUS_CONTAINER_READY, db_path=self.db_path)
            post = get_post(post_id, db_path=self.db_path) or post

        # ---- Phase 2a: the bytes.
        upload_url = f"{self.client.rupload_base}/ig-api-upload/{self.client.api_version}/{container_id}"
        headers = {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}
        if self.may_send("upload"):
            with open(video, "rb") as fh:
                resp = self.client.post(upload_url, headers=headers, data=fh, timeout=600)
            body = self.client.check(resp, "rupload")
            if not body.get("success", True):
                return self.fail(post, f"rupload refused the file: {body}")
            status = self._wait_for_container(container_id, token)
            if status != "FINISHED":
                return self.fail(post, f"container is {status}, not FINISHED")
        else:
            plan.add("POST", upload_url, headers=headers, body=str(video), file_size=size)
            plan.add(
                "GET", f"{base}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                until="FINISHED", every_s=self.poll_seconds, max_min=self.poll_max_minutes,
            )

        # ---- Phase 2b: `publishing` is written BEFORE the publish call is issued.
        set_status(post_id, STATUS_PUBLISHING, db_path=self.db_path, publish_started_at=db.now())
        publish_url = f"{base}/{ig_user}/media_publish"
        publish_params = {"creation_id": container_id, "access_token": token}
        if not self.may_send("publish"):
            plan.add("POST", publish_url, params=publish_params)
            where = plan.write()
            set_status(
                post_id, STATUS_DRY_RUN, db_path=self.db_path,
                error_message=f"dry run ({self.dry_run_mode()}); plan: {where}",
            )
            db.log_stage(post["item_id"], "posting", "success",
                         message=f"instagram dry run, {len(plan.steps)} planned requests", db_path=self.db_path)
            return STATUS_DRY_RUN

        published = self.client.check(
            self.client.post(publish_url, data=publish_params, timeout=60, retry=False), "media_publish")
        media_id = published["id"]
        link, when = self._ig_media_details(media_id, token)
        set_status(
            post_id, STATUS_LIVE, db_path=self.db_path,
            remote_id=media_id, remote_url=link, posted_at=when or db.now(), error_message=None,
        )
        db.log_stage(post["item_id"], "posting", "success", message=f"instagram live: {media_id}", db_path=self.db_path)
        return STATUS_LIVE

    def _wait_for_container(self, container_id: str, token: str) -> str:
        """Poll the container until it is FINISHED, or until the deadline. Returns the last status."""
        deadline = self.clock() + self.poll_max_minutes * 60
        status = "UNKNOWN"
        while self.clock() < deadline:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{container_id}",
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30,
                ),
                "container status",
            )
            status = str(data.get("status_code") or "UNKNOWN")
            if status in ("FINISHED", "ERROR", "EXPIRED", "PUBLISHED"):
                return status
            self.sleep(self.poll_seconds)
        return status

    def _ig_media_details(self, media_id: str, token: str) -> tuple[str | None, str | None]:
        try:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{media_id}",
                    params={"fields": "permalink,timestamp", "access_token": token},
                    timeout=30,
                ),
                "media details",
            )
            return data.get("permalink"), graph_time(data.get("timestamp"))
        except (PublishError, RateLimitError, requests.RequestException) as e:
            log.info("instagram: could not read the permalink (%s); the post is live either way", redact(e))
            return None, None

    # ------------------------------------------------------------- facebook
    def _publish_facebook(self, post: dict[str, Any], item: dict[str, Any]) -> str:
        post_id = post["id"]
        page = config.secret("FB_PAGE_ID")
        token = fb_token()
        caption = post.get("caption_text") or caption_for_item(item)
        video = video_for_post(item, post)
        size = video.stat().st_size
        plan = Plan(post_id, PLATFORM_FACEBOOK)
        base = self.client.base()

        if not self.dry_run() and not (page and token):
            return self.fail(post, "FB_PAGE_ID / page access token missing")
        page = page or "<FB_PAGE_ID>"

        video_id = post.get("remote_container_id")
        reels_url = f"{base}/{page}/video_reels"

        # ---- Phase 1: the upload session, saved BEFORE anything else happens.
        if post["status"] in (STATUS_PENDING, STATUS_UPLOADING) and not video_id:
            set_status(post_id, STATUS_UPLOADING, db_path=self.db_path, error_message=None)
            params = {"upload_phase": "start", "access_token": token}
            if self.may_send("container"):
                data = self.client.check(self.client.post(reels_url, data=params, timeout=60), "FB reels start")
                video_id = data["video_id"]
            else:
                plan.add("POST", reels_url, params=params)
                video_id = f"<dry-run-video:{post_id}>"
            update_post(post_id, db_path=self.db_path, remote_container_id=video_id)
            set_status(post_id, STATUS_CONTAINER_READY, db_path=self.db_path)
            post = get_post(post_id, db_path=self.db_path) or post

        # ---- Phase 2a: the bytes.
        upload_url = f"{self.client.rupload_base}/video-upload/{self.client.api_version}/{video_id}"
        headers = {"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}
        if self.may_send("upload"):
            with open(video, "rb") as fh:
                resp = self.client.post(upload_url, headers=headers, data=fh, timeout=600)
            body = self.client.check(resp, "FB rupload")
            if not body.get("success", True):
                return self.fail(post, f"FB rupload refused the file: {body}")
        else:
            plan.add("POST", upload_url, headers=headers, body=str(video), file_size=size)

        # ---- Phase 2b: finish with video_state=PUBLISHED is the publish; `publishing` first.
        set_status(post_id, STATUS_PUBLISHING, db_path=self.db_path, publish_started_at=db.now())
        finish_params = {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "description": caption,
            "access_token": token,
        }
        if not self.may_send("publish"):
            plan.add("POST", reels_url, params=finish_params)
            where = plan.write()
            set_status(
                post_id, STATUS_DRY_RUN, db_path=self.db_path,
                error_message=f"dry run ({self.dry_run_mode()}); plan: {where}",
            )
            db.log_stage(post["item_id"], "posting", "success",
                         message=f"facebook dry run, {len(plan.steps)} planned requests", db_path=self.db_path)
            return STATUS_DRY_RUN

        finished = self.client.check(
            self.client.post(reels_url, data=finish_params, timeout=60, retry=False), "FB reels finish")
        if not finished.get("success", True):
            return self.fail(post, f"FB reels finish refused: {finished}")
        published_at = db.now()
        link = self._fb_permalink(video_id, token)
        set_status(
            post_id, STATUS_LIVE, db_path=self.db_path,
            remote_id=video_id, remote_url=link, posted_at=published_at, error_message=None,
        )
        db.log_stage(post["item_id"], "posting", "success", message=f"facebook live: {video_id}", db_path=self.db_path)
        return STATUS_LIVE

    def _fb_permalink(self, video_id: str, token: str) -> str:
        try:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{video_id}",
                    params={"fields": "status,permalink_url", "access_token": token},
                    timeout=30,
                ),
                "FB reel status",
            )
            link = data.get("permalink_url")
            if link:
                return "https://www.facebook.com" + link if str(link).startswith("/") else str(link)
        except (PublishError, RateLimitError, requests.RequestException) as e:
            log.info("facebook: could not read the permalink (%s)", redact(e))
        return f"https://www.facebook.com/reel/{video_id}"

    # ------------------------------------------------------------- crash recovery
    def remote_state(self, post: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """What the remote says about a row left in `publishing`.

        Returns ('published'|'not_published'|'unknown', detail). 'not_published' is only returned on
        positive proof that publishing did not and cannot have happened: the Instagram container is
        ERROR / EXPIRED, or the Facebook video failed. A container that merely reads FINISHED is
        'unknown', because it cannot tell a publish that never happened from one whose media has not
        yet surfaced in /media - and an unknown state is never published again.
        """
        container_id = post.get("remote_container_id")
        if not container_id:
            # No id was ever saved, so Phase 1 never completed and no publish call was possible.
            return "not_published", {"why": "no container id was saved"}

        if post["platform"] == PLATFORM_INSTAGRAM:
            return self._ig_remote_state(post, container_id)
        if post["platform"] == PLATFORM_FACEBOOK:
            return self._fb_remote_state(post, container_id)
        return "unknown", {"why": f"unknown platform {post['platform']}"}

    def _ig_remote_state(self, post: dict[str, Any], container_id: str) -> tuple[str, dict[str, Any]]:
        token = ig_token()
        ig_user = config.secret("IG_USER_ID")
        try:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{container_id}",
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30,
                ),
                "container status",
            )
        except (PublishError, requests.RequestException) as e:
            return "unknown", {"why": f"container status could not be read: {redact(e)[:200]}"}

        status = str(data.get("status_code") or "UNKNOWN")
        if status == "PUBLISHED":
            media = self._find_recent_media(post, ig_user, token)
            if media:
                return "published", {"status_code": status, **media}
            return "published", {"status_code": status, "why": "container says PUBLISHED, media id not found"}
        if status in ("ERROR", "EXPIRED"):
            return "not_published", {"status_code": status, "status": data.get("status")}

        # FINISHED / IN_PROGRESS: check the account's recent media before judging.
        media = self._find_recent_media(post, ig_user, token)
        if media:
            return "published", {"status_code": status, **media}
        return "unknown", {
            "status_code": status,
            "why": "container is not PUBLISHED and no matching recent media was found; "
                   "a publish that already landed cannot be ruled out",
        }

    def _find_recent_media(
        self, post: dict[str, Any], ig_user: str, token: str
    ) -> dict[str, Any] | None:
        """Look for this post in the account's recent media: caption match, after the publish began."""
        caption = (post.get("caption_text") or "").strip()
        try:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{ig_user}/media",
                    params={
                        "fields": "id,caption,permalink,timestamp,media_type",
                        "limit": self.recent_media_limit,
                        "access_token": token,
                    },
                    timeout=30,
                ),
                "recent media",
            )
        except (PublishError, requests.RequestException) as e:
            log.info("instagram: recent media could not be read: %s", redact(e))
            return None

        started = post.get("publish_started_at") or post.get("created_at")
        needle = caption.splitlines()[0].strip() if caption else ""
        for media in data.get("data") or []:
            when = graph_time(media.get("timestamp"))
            if started and when and when < started:
                continue
            got = (media.get("caption") or "").strip()
            if needle and got.startswith(needle):
                return {"remote_id": media.get("id"), "remote_url": media.get("permalink"), "posted_at": when}
        return None

    def _fb_remote_state(self, post: dict[str, Any], video_id: str) -> tuple[str, dict[str, Any]]:
        token = fb_token()
        try:
            data = self.client.check(
                self.client.get(
                    f"{self.client.base()}/{video_id}",
                    params={"fields": "status,permalink_url", "access_token": token},
                    timeout=30,
                ),
                "FB reel status",
            )
        except (PublishError, requests.RequestException) as e:
            return "unknown", {"why": f"reel status could not be read: {redact(e)[:200]}"}

        status = data.get("status") or {}
        video_status = str(status.get("video_status") or "")
        phases = [status.get(k) or {} for k in ("uploading_phase", "processing_phase", "publishing_phase")]
        publish_status = str((status.get("publishing_phase") or {}).get("publish_status") or "")
        link = data.get("permalink_url")

        if publish_status == "published" or video_status in ("ready", "published"):
            url = ("https://www.facebook.com" + link) if link and str(link).startswith("/") else link
            return "published", {"remote_id": video_id, "remote_url": url or f"https://www.facebook.com/reel/{video_id}"}
        if video_status in ("error", "upload_failed", "expired") or any(p.get("status") == "error" for p in phases):
            return "not_published", {"video_status": video_status, "errors": [p.get("errors") for p in phases if p.get("errors")]}
        return "unknown", {
            "video_status": video_status or "(empty)",
            "publish_status": publish_status or "(empty)",
            "why": "the reel is neither published nor failed; a publish that already landed cannot be ruled out",
        }

    def settle(self, post: dict[str, Any], item: dict[str, Any] | None = None) -> str:
        """Resolve a row found in `publishing` after a crash. Never publishes a second time."""
        post_id = post["id"]
        if self.dry_run() and self.dry_run_mode() != "up_to_publish":
            # Nothing was ever sent, so there is nothing to ask the remote about.
            set_status(post_id, STATUS_DRY_RUN, db_path=self.db_path,
                       error_message="dry run (plan_only); settled without any remote call")
            return STATUS_DRY_RUN

        state, detail = self.remote_state(post)
        log.info("settle post %s (%s): remote says %s %s", post_id, post["platform"], state, detail)

        if state == "published":
            set_status(
                post_id, STATUS_LIVE, db_path=self.db_path,
                remote_id=detail.get("remote_id") or post.get("remote_id"),
                remote_url=detail.get("remote_url") or post.get("remote_url"),
                posted_at=detail.get("posted_at") or post.get("posted_at") or db.now(),
                error_message=None,
            )
            db.log_stage(post["item_id"], "posting", "success",
                         message=f"{post['platform']} was already live; recovered after a crash", db_path=self.db_path)
            return STATUS_LIVE

        if state == "not_published":
            # The remote proves nothing was published: start a fresh container, safely.
            set_status(
                post_id, STATUS_PENDING, db_path=self.db_path,
                remote_container_id=None, publish_started_at=None,
                error_message=f"restarted after a crash: {json.dumps(detail)[:400]}",
            )
            db.log_stage(post["item_id"], "posting", "start",
                         message=f"{post['platform']} container was dead; restarting", db_path=self.db_path)
            item = item or db.get_item(post["item_id"], db_path=self.db_path)
            if item:
                return self.publish(post_id, item)
            return STATUS_PENDING

        # Unknown: never publish again - not now, and not on any later pass either (no_retry).
        return self.fail(
            post,
            f"remote state unknown after a crash in 'publishing'; not re-published. {json.dumps(detail)[:400]}",
            terminal=True,
        )


# ---------------------------------------------------------------------------
# Stage entry points
# ---------------------------------------------------------------------------

def settle_publishing_rows(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    publisher: Publisher | None = None,
) -> list[tuple[int, str]]:
    """Resolve every row left in `publishing` by a crash. Runs before anything is published."""
    cfg = cfg or config.get_config()
    rows = get_posts_by_status(STATUS_PUBLISHING, db_path=db_path)
    if not rows:
        return []
    publisher = publisher or Publisher(cfg=cfg, db_path=db_path)
    out: list[tuple[int, str]] = []
    for post in rows:
        try:
            out.append((post["id"], publisher.settle(post)))
        except RateLimitError:
            log.warning("meta paused while settling post %s; leaving it in publishing", post["id"])
            out.append((post["id"], STATUS_PUBLISHING))
            break
        except (PublishError, requests.RequestException, OSError) as e:
            out.append((post["id"], publisher.fail(post, f"settle failed: {e}")))
        except sqlite3.IntegrityError as e:
            fail_refused_item(post["item_id"], e, db_path=db_path)
            out.append((post["id"], STATUS_FAILED))
    return out


def run_posting_for_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    publisher: Publisher | None = None,
    slot_datetime: str | None = None,
    rng: Any = None,
) -> dict[str, str]:
    """Post one approved item to every enabled platform that is not live yet.

    The A/B variant is drawn here, as the post begins (decide_variant), not when it was queued.

    A platform whose row is already `live` is skipped, so a failed Facebook row retries Facebook
    alone. The item becomes `posted` only when every enabled platform row is `live` (or `dry_run`).
    """
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        raise PublishError(f"no item {item_id}")
    refusal = refusal_reason(item, cfg)
    if refusal:
        # Explicit, before any row is written: posting never relies on the trigger to say no.
        raise PublishError(refusal)

    ensure_post_rows(item, slot_datetime=slot_datetime, cfg=cfg, db_path=db_path)
    decide_variant(item_id, cfg=cfg, db_path=db_path, rng=rng)
    item = db.get_item(item_id, db_path=db_path) or item
    publisher = publisher or Publisher(cfg=cfg, db_path=db_path)
    wanted = set(enabled_platforms(cfg))
    results: dict[str, str] = {}

    for post in get_posts_for_item(item_id, db_path=db_path):
        if post["platform"] not in wanted:
            continue
        if post["status"] in (STATUS_LIVE, STATUS_DRY_RUN) or is_terminal_failure(post, cfg):
            results[post["platform"]] = post["status"]
            continue
        try:
            results[post["platform"]] = publisher.publish(post["id"], item)
        except RateLimitError:
            results[post["platform"]] = post["status"]
            break

    final = {p: results.get(p) for p in wanted}
    rows = [r for r in get_posts_for_item(item_id, db_path=db_path) if r["platform"] in wanted]
    done_for_good = bool(rows) and len(rows) == len(wanted) and all(
        r["status"] == STATUS_LIVE or is_terminal_failure(r, cfg) for r in rows
    )
    if final and all(s == STATUS_LIVE for s in final.values()):
        db.transition_stage(item_id, "posted", db_path=db_path)
        db.log_stage(item_id, "posting", "success", message=f"posted: {final}", db_path=db_path)
    elif final and all(s == STATUS_DRY_RUN for s in final.values()):
        # A dry run must not advance the item: it still has to post for real once dry_run is off.
        log.info("item %s completed the flow in dry run; its stage is unchanged: %s", item_id, final)
    elif done_for_good and any(r["status"] == STATUS_LIVE for r in rows):
        # Live on one platform, given up on the other: the item is out, and it is measured.
        db.transition_stage(item_id, "posted", db_path=db_path)
        db.log_stage(item_id, "posting", "success",
                     message=f"posted on some platforms only; the rest failed for good: {final}", db_path=db_path)
    elif done_for_good:
        # Every platform failed for good: take the item out of the queue so it frees its slot
        # and is not retried every cycle forever.
        db.mark_failed(item_id, "posting", f"every platform failed for good: {final}", db_path=db_path)
        db.log_stage(item_id, "posting", "failure", message=f"gave up: {final}", db_path=db_path)
    else:
        log.info("item %s not fully posted yet: %s", item_id, final)
    return results


def due_items(now: datetime, db_path: Any = None) -> list[dict[str, Any]]:
    """Queued, releasable items whose slot has come, earliest slot first.

    Items whose rows are all `dry_run` already walked the flow in a dry run and are left alone.
    """
    with db.db(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT i.*, MIN(p.slot_datetime) AS slot
            FROM items i JOIN posts p ON p.item_id = i.id
            WHERE i.stage = 'queued' AND {db.RELEASABLE_SQL.replace("id NOT LIKE", "i.id NOT LIKE")}
            GROUP BY i.id
            HAVING slot <= ? AND SUM(CASE WHEN p.status != 'dry_run' THEN 1 ELSE 0 END) > 0
            ORDER BY slot ASC, i.discovery_score DESC
            """,
            (clock.iso(now),),
        ).fetchall()
        return [dict(r) for r in rows]


def last_live_at(exclude_item_id: str | None = None, db_path: Any = None) -> str | None:
    """When the most recent post of any OTHER item went live."""
    with db.db(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(posted_at) AS last FROM posts WHERE status = 'live' AND item_id != ?",
            (exclude_item_id or "",),
        ).fetchone()
        return row["last"] if row else None


def run_posting_stage(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    publisher: Publisher | None = None,
    limit: int | None = 1,
    now: datetime | None = None,
    rng: Any = None,
) -> dict[str, dict[str, str]]:
    """One posting pass: settle crashed rows, then post the queued item(s) whose slot has come.

    Only `queued` items are posted, and only at their slot (the scheduler turns `approved` into
    `queued`). Three rules keep a sleeping laptop from double- or burst-posting:
      * a post that is already underway (a row `publishing` or `live`) is finished, never restarted;
      * a post that has not started and whose slot is more than `posting.missed_slot_grace_minutes`
        old is NOT sent late - it is left for the scheduler to move to the next free slot;
      * at most `limit` not-yet-started items are begun per pass, and none within
        `posting.min_gap_hours` of another item's live post.
    """
    cfg = cfg or config.get_config()
    now = now or clock.utcnow()
    paused, why = meta_paused(db_path)
    if paused:
        log.warning("meta is paused; no posting this pass: %s", why)
        return {}

    publisher = publisher or Publisher(cfg=cfg, db_path=db_path)
    reset_dry_run_rows(cfg=cfg, db_path=db_path)
    settle_publishing_rows(cfg=cfg, db_path=db_path, publisher=publisher)

    min_gap_s = float((cfg.get("posting") or {}).get("min_gap_hours", 3.0)) * 3600.0
    out: dict[str, dict[str, str]] = {}
    begun = 0
    for item in due_items(now, db_path=db_path):
        rows = get_posts_for_item(item["id"], db_path=db_path)
        underway = any(r["status"] in STARTED_STATUSES for r in rows)
        if not underway:
            if slot_is_missed(rows, now, cfg):
                log.info("item %s missed its slot %s; the scheduler will re-slot it", item["id"], item["slot"])
                continue
            if limit is not None and begun >= limit:
                continue
            last = last_live_at(exclude_item_id=item["id"], db_path=db_path)
            if last and (now - clock.parse(last)).total_seconds() < min_gap_s:
                log.info("item %s held back: another post went live at %s (min gap)", item["id"], last)
                continue
            begun += 1
        try:
            out[item["id"]] = run_posting_for_item(item["id"], cfg=cfg, db_path=db_path, publisher=publisher,
                                                   rng=rng)
        except RateLimitError:
            break
        except PublishError as e:
            log.warning("posting failed for %s: %s", item["id"], redact(e))
        except sqlite3.IntegrityError as e:
            fail_refused_item(item["id"], e, db_path=db_path)
    return out


def fail_refused_item(item_id: str, exc: BaseException, db_path: Any = None) -> None:
    """A database trigger (preview_only) refused a posting write: fail that ONE item, cleanly.

    `failed` is not a release stage, so the trigger allows it; the item leaves the posting queue
    and the next pass does not meet it again - the runner cannot crash-loop on it.
    """
    msg = f"refused by the database: {exc}"
    log.error("posting: item %s %s", item_id, msg)
    try:
        db.mark_failed(item_id, "posting", msg, db_path=db_path)
        db.log_stage(item_id, "posting", "failure", message=msg, db_path=db_path)
    except sqlite3.Error as e:
        log.error("posting: could not mark %s failed: %s", item_id, e)
