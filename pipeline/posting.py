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

from . import config, db, rate_limit
from .errors import PublishError, RateLimitError

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
RUPLOAD_BASE = "https://rupload.facebook.com"
API_VERSION = "v25.0"
META_SOURCE = "meta_graph"          # the key `rate_limits` pauses under
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
    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """One paced request, retried on 5xx and network errors with capped exponential backoff."""
        attempt = 0
        while True:
            self.wait_turn()
            try:
                resp = self.transport(method, url, **kwargs)
            except requests.RequestException as e:
                e.args = tuple(redact(a) for a in e.args) or (redact(e),)
                if attempt >= self.max_retries:
                    raise
                self._backoff(attempt, f"network error: {redact(e)}")
                attempt += 1
                continue
            status = int(getattr(resp, "status_code", 200))
            if status >= 500 and attempt < self.max_retries:
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
    """One `pending` row per enabled platform, created once (UNIQUE(item_id, platform))."""
    cfg = cfg or config.get_config()
    if not db.is_releasable(item):
        raise PublishError(f"item {item['id']} is preview_only and is never posted")
    variant = item.get("chosen_variant") or "B"
    slot = slot_datetime or db.now()
    caption = caption_for_item(item, cfg)
    ts = db.now()
    with db.db(db_path) as conn:
        for platform in enabled_platforms(cfg):
            conn.execute(
                """
                INSERT INTO posts (
                    item_id, platform, variant, hook_style, source, authenticity,
                    slot_datetime, status, ab_excluded, caption_text, created_at, updated_at
                ) VALUES (
                    :item_id, :platform, :variant, :hook_style, :source, :authenticity,
                    :slot_datetime, :status, :ab_excluded, :caption_text, :created_at, :updated_at
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
                    "slot_datetime": slot,
                    "status": STATUS_PENDING,
                    "ab_excluded": int(item.get("ab_excluded") or 0),
                    "caption_text": caption,
                    "created_at": ts,
                    "updated_at": ts,
                },
            )
    return get_posts_for_item(item["id"], db_path=db_path)


def video_for_post(item: dict[str, Any], post: dict[str, Any]) -> Path:
    variant = (post.get("variant") or "B").upper()
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
    ):
        self.cfg = cfg or config.get_config()
        self.db_path = db_path
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

    def fail(self, post: dict[str, Any], message: str, alert: bool = True) -> str:
        msg = redact(message)[:900]
        set_status(post["id"], STATUS_FAILED, db_path=self.db_path, error_message=msg)
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

        paused, why = meta_paused(self.db_path)
        if paused:
            log.warning("meta is paused, %s not sent: %s", post["platform"], why)
            return post["status"]

        item = item or db.get_item(post["item_id"], db_path=self.db_path)
        if not item:
            return self.fail(post, f"item {post['item_id']} is gone")

        attempts = int(post.get("attempt_count") or 0) + 1
        if attempts > self.max_attempts:
            return self.fail(
                post, f"gave up after {self.max_attempts} attempts (last status {post['status']})"
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

        published = self.client.check(self.client.post(publish_url, data=publish_params, timeout=60), "media_publish")
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

        finished = self.client.check(self.client.post(reels_url, data=finish_params, timeout=60), "FB reels finish")
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

        # Unknown: never publish again.
        return self.fail(
            post,
            f"remote state unknown after a crash in 'publishing'; not re-published. {json.dumps(detail)[:400]}",
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
) -> dict[str, str]:
    """Post one approved item to every enabled platform that is not live yet.

    A platform whose row is already `live` is skipped, so a failed Facebook row retries Facebook
    alone. The item becomes `posted` only when every enabled platform row is `live` (or `dry_run`).
    """
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        raise PublishError(f"no item {item_id}")
    if not db.is_releasable(item):
        # Explicit, before any row is written: posting never relies on the trigger to say no.
        raise PublishError(f"item {item_id} is preview_only and is never posted")

    ensure_post_rows(item, slot_datetime=slot_datetime, cfg=cfg, db_path=db_path)
    publisher = publisher or Publisher(cfg=cfg, db_path=db_path)
    wanted = set(enabled_platforms(cfg))
    results: dict[str, str] = {}

    for post in get_posts_for_item(item_id, db_path=db_path):
        if post["platform"] not in wanted:
            continue
        if post["status"] in (STATUS_LIVE, STATUS_DRY_RUN):
            results[post["platform"]] = post["status"]
            continue
        try:
            results[post["platform"]] = publisher.publish(post["id"], item)
        except RateLimitError:
            results[post["platform"]] = post["status"]
            break

    final = {p: results.get(p) for p in wanted}
    if final and all(s == STATUS_LIVE for s in final.values()):
        db.transition_stage(item_id, "posted", db_path=db_path)
        db.log_stage(item_id, "posting", "success", message=f"posted: {final}", db_path=db_path)
    elif final and all(s == STATUS_DRY_RUN for s in final.values()):
        # A dry run must not advance the item: it still has to post for real once dry_run is off.
        log.info("item %s completed the flow in dry run; its stage is unchanged: %s", item_id, final)
    else:
        log.info("item %s not fully posted yet: %s", item_id, final)
    return results


def run_posting_stage(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    publisher: Publisher | None = None,
    limit: int | None = None,
) -> dict[str, dict[str, str]]:
    """One posting pass: settle crashed rows, then post approved / queued items."""
    cfg = cfg or config.get_config()
    paused, why = meta_paused(db_path)
    if paused:
        log.warning("meta is paused; no posting this pass: %s", why)
        return {}

    publisher = publisher or Publisher(cfg=cfg, db_path=db_path)
    settle_publishing_rows(cfg=cfg, db_path=db_path, publisher=publisher)

    out: dict[str, dict[str, str]] = {}
    todo: list[dict[str, Any]] = []
    for stage in ("approved", "queued"):
        todo.extend(db.get_items_by_stage(stage, db_path=db_path, releasable_only=True))
    for item in todo[: limit or len(todo)]:
        try:
            out[item["id"]] = run_posting_for_item(item["id"], cfg=cfg, db_path=db_path, publisher=publisher)
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
