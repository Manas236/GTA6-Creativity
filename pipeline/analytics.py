"""Stage 11: 24h / 72h metric snapshots and the ab_learning rebuild (CP4, PLAN.md section 10).

SNAPSHOTS
  Every live post gets one snapshot per `analytics.snapshot_hours` entry (24 and 72). A snapshot is
  taken on the first run at or after `posted_at + H`, and only up to `max_snapshot_late_hours`
  after that: insights are cumulative, so a "24h" number read at hour 60 would be a 60h number and
  would flatter that post against the rest. A snapshot that can no longer be taken in its window
  is skipped for good and the post is ranked on the snapshots it has.

METRIC NAMES ARE CONFIG
  Graph API insight names change often, so the endpoint, the metric list and the mapping to
  views / likes / comments / shares all live in `config.yaml -> analytics`. If Graph rejects the
  combined request (one unknown metric fails the whole call) each metric is asked for on its own,
  the ones Graph refuses are dropped and alerted once, and the snapshot is still recorded.

LEARNING
  `ab_learning` is deleted and rebuilt from `metrics` on every run - never incremented - in one
  transaction. Each post is percentile-ranked WITHIN its platform (and within its snapshot window,
  so a 72h number never competes with a 24h one) before the platforms are averaged per item; that
  keeps Instagram's larger raw numbers from drowning Facebook. Dimensions: variant, hook_style,
  source, authenticity (official vs unverified) and style_preset. ab_excluded items ("Approve A
  only") are left out of the variant dimension only.

All requests go through posting.MetaClient (pacing, retries, the shared 429 pause) and are GETs,
so they are allowed in a dry run - but a dry run has no live posts, so nothing is fetched.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

import requests

from . import clock, config, db, posting, scheduler, telegram_bot
from .errors import PublishError, RateLimitError

log = logging.getLogger(__name__)

# Code defaults; config.yaml -> analytics overrides any key. Names as of Graph API v25.0 - see
# PLAN.md "Calls I made" for where each one comes from and what could not be verified.
DEFAULTS: dict[str, Any] = {
    "snapshot_hours": [24, 72],
    "max_snapshot_late_hours": 12,
    "score_weights": {"views": 1.0, "likes": 0.0, "comments": 0.0, "shares": 0.0},
    "instagram": {
        "endpoint": "{remote_id}/insights",
        "metrics": ["views", "likes", "comments", "shares", "saved", "reach", "total_interactions"],
        "map": {"views": ["views", "plays"], "likes": "likes", "comments": "comments", "shares": "shares"},
    },
    "facebook": {
        "endpoint": "{remote_id}/video_insights",
        "metrics": [
            "fb_reels_total_plays", "blue_reels_play_count", "post_impressions_unique",
            "post_video_likes_by_reaction_type", "post_video_social_actions",
        ],
        "map": {
            "views": ["fb_reels_total_plays", "blue_reels_play_count"],
            "likes": "post_video_likes_by_reaction_type",      # {"LIKE": 3, "LOVE": 1} -> summed
            "comments": "post_video_social_actions.COMMENT",
            "shares": "post_video_social_actions.SHARE",
        },
    },
}

FIELDS = ("views", "likes", "comments", "shares")
DIMENSIONS = ("variant", "hook_style", "source", "authenticity", "style_preset")


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """DEFAULTS with config.yaml's `analytics:` block merged over them (one level deep)."""
    user = ((cfg or {}).get("analytics") or {})
    out: dict[str, Any] = {}
    for key, default in DEFAULTS.items():
        if isinstance(default, dict):
            out[key] = {**default, **(user.get(key) or {})}
        else:
            out[key] = user.get(key, default)
    return out


# ---------------------------------------------------------------------------
# Reading an insights answer
# ---------------------------------------------------------------------------

def parse_insights(payload: dict[str, Any]) -> dict[str, Any]:
    """{metric name: value} from a Graph insights answer.

    Handles both shapes Graph uses: `values: [{"value": N}]` (the last entry is the newest) and
    `total_value: {"value": N}`. A value may be a number or a breakdown dict.
    """
    out: dict[str, Any] = {}
    for entry in payload.get("data") or []:
        name = entry.get("name")
        if not name:
            continue
        if isinstance(entry.get("total_value"), dict) and "value" in entry["total_value"]:
            out[name] = entry["total_value"]["value"]
        elif entry.get("values"):
            out[name] = (entry["values"][-1] or {}).get("value")
    return out


def _number(value: Any, subkey: str | None = None) -> int | None:
    """A metric value as an int. A breakdown dict gives its `subkey`, or its sum without one."""
    if isinstance(value, dict):
        if subkey is not None:
            hit = next((v for k, v in value.items() if str(k).lower() == subkey.lower()), None)
            return _number(hit) if hit is not None else 0
        return sum(int(v) for v in value.values() if isinstance(v, (int, float)))
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def map_metrics(raw: dict[str, Any], mapping: dict[str, Any]) -> dict[str, int]:
    """views / likes / comments / shares from raw insight values, per the config mapping.

    A mapping value is "metric", "metric.SUBKEY" (one key of a breakdown) or a list of
    alternatives where the first one present in the answer wins (old and new metric names).
    """
    out: dict[str, int] = {}
    for field in FIELDS:
        spec = mapping.get(field)
        choices = spec if isinstance(spec, list) else ([spec] if spec else [])
        value: int | None = None
        for choice in choices:
            name, _, subkey = str(choice).partition(".")
            if name in raw:
                value = _number(raw[name], subkey or None)
                if value is not None:
                    break
        out[field] = int(value or 0)
    return out


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

class InsightsFetcher:
    """Reads the insights of one live post through the shared, paced Meta client."""

    def __init__(self, client: posting.MetaClient, cfg: dict[str, Any], db_path: Any = None, alert: Any = None):
        self.client = client
        self.cfg = cfg
        self.db_path = db_path
        self.settings = settings(cfg)
        self._alert = alert

    def token(self, platform: str) -> str:
        return posting.ig_token() if platform == posting.PLATFORM_INSTAGRAM else posting.fb_token()

    def _get(self, url: str, metrics: list[str], token: str, what: str) -> dict[str, Any]:
        resp = self.client.get(url, params={"metric": ",".join(metrics), "access_token": token}, timeout=30)
        return parse_insights(self.client.check(resp, what))

    def fetch(self, post: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
        """(mapped fields, raw insight values) for one post. Raises PublishError if nothing at all
        could be read, RateLimitError if Meta is limiting us."""
        platform = post["platform"]
        plat = self.settings.get(platform) or {}
        metrics = [str(m) for m in plat.get("metrics") or []]
        if not metrics:
            raise PublishError(f"analytics.{platform}.metrics is empty")
        token = self.token(platform)
        if not token:
            raise PublishError(f"no access token for {platform} insights")
        url = f"{self.client.base()}/{str(plat['endpoint']).format(remote_id=post['remote_id'])}"
        what = f"{platform} insights"
        try:
            raw = self._get(url, metrics, token, what)
        except PublishError as combined_error:
            # One unknown metric fails the whole request. Ask one at a time and keep what works.
            raw, refused = {}, []
            for metric in metrics:
                try:
                    raw.update(self._get(url, [metric], token, f"{what} ({metric})"))
                except PublishError:
                    refused.append(metric)
            if not raw:
                raise combined_error
            self.alert(
                f"Meta refused {platform} insight metric(s) {refused}; they were skipped. "
                f"Update analytics.{platform}.metrics in config.yaml.",
                key=f"analytics_metrics:{platform}:{','.join(sorted(refused))}",
            )
        return map_metrics(raw, plat.get("map") or {}), raw

    def alert(self, text: str, key: str) -> None:
        if self._alert:
            self._alert(text, key)
        else:
            telegram_bot.send_alert(text, key=key, cfg=self.cfg, db_path=self.db_path, repeat_gap_s=None)


def due_snapshots(
    now: datetime, cfg: dict[str, Any] | None = None, db_path: Any = None
) -> list[tuple[dict[str, Any], int]]:
    """(post, hours) for every live post whose snapshot window is open and not yet recorded."""
    conf = settings(cfg)
    late = timedelta(hours=float(conf["max_snapshot_late_hours"]))
    with db.db(db_path) as conn:
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE status = 'live' AND remote_id IS NOT NULL AND posted_at IS NOT NULL "
            "ORDER BY posted_at ASC"
        ).fetchall()]
        have = {(r["post_id"], r["snapshot_hours"]) for r in conn.execute(
            "SELECT post_id, snapshot_hours FROM metrics").fetchall()}
    due: list[tuple[dict[str, Any], int]] = []
    for post in posts:
        posted = clock.parse(post["posted_at"])
        for hours in sorted(int(h) for h in conf["snapshot_hours"]):
            opens = posted + timedelta(hours=hours)
            if (post["id"], hours) not in have and opens <= now <= opens + late:
                due.append((post, hours))
    return due


def record_snapshot(
    post_id: int, hours: int, fields: dict[str, int], raw: dict[str, Any], db_path: Any = None
) -> None:
    with db.db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO metrics (post_id, snapshot_hours, views, likes, comments, shares, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id, snapshot_hours) DO NOTHING
            """,
            (post_id, hours, fields["views"], fields["likes"], fields["comments"], fields["shares"],
             json.dumps(raw)[:20000], db.now()),
        )


# ---------------------------------------------------------------------------
# Learning: rebuilt from metrics every run
# ---------------------------------------------------------------------------

def percentile_ranks(values: dict[Any, float]) -> dict[Any, float]:
    """Mid-rank percentile of each value within the group, in (0, 1): ties share a rank, and a
    group of one scores 0.5 (no information either way)."""
    n = len(values)
    out: dict[Any, float] = {}
    for key, v in values.items():
        below = sum(1 for o in values.values() if o < v)
        equal = sum(1 for o in values.values() if o == v)
        out[key] = (below + 0.5 * equal) / n
    return out


def authenticity_bucket(value: str | None) -> str:
    """The learning dimension is official vs unverified; leaks, rumours and fan-made all count as
    unverified."""
    return "official" if str(value or "").lower() == "official" else "unverified"


def item_scores(cfg: dict[str, Any] | None = None, db_path: Any = None) -> dict[str, dict[str, Any]]:
    """{item_id: {"score": combined percentile, "posts": [...], attributes...}} from `metrics`."""
    weights = settings(cfg)["score_weights"]
    with db.db(db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """
            SELECT m.post_id, m.snapshot_hours, m.views, m.likes, m.comments, m.shares,
                   p.item_id, p.platform, p.variant, p.hook_style, p.source, p.authenticity,
                   p.style_preset, p.ab_excluded
            FROM metrics m JOIN posts p ON p.id = m.post_id
            """
        ).fetchall()]

    # 1. rank within each (platform, snapshot window)
    cohorts: dict[tuple[str, int], dict[int, float]] = defaultdict(dict)
    for r in rows:
        raw = sum(float(weights.get(f, 0.0)) * float(r[f] or 0) for f in FIELDS)
        cohorts[(r["platform"], int(r["snapshot_hours"]))][r["post_id"]] = raw
    ranked = {cohort: percentile_ranks(vals) for cohort, vals in cohorts.items()}

    # 2. one score per post: its longest window (72h beats 24h)
    best: dict[int, tuple[int, float, dict[str, Any]]] = {}
    for r in rows:
        hours = int(r["snapshot_hours"])
        rank = ranked[(r["platform"], hours)][r["post_id"]]
        if r["post_id"] not in best or hours > best[r["post_id"]][0]:
            best[r["post_id"]] = (hours, rank, r)

    # 3. one score per item: the mean over its platforms
    items: dict[str, dict[str, Any]] = {}
    for hours, rank, r in best.values():
        entry = items.setdefault(r["item_id"], {
            "ranks": [], "variant": r["variant"], "hook_style": r["hook_style"], "source": r["source"],
            "authenticity": authenticity_bucket(r["authenticity"]),
            "style_preset": r["style_preset"] or "P0", "ab_excluded": int(r["ab_excluded"] or 0),
        })
        entry["ranks"].append(rank)
    for entry in items.values():
        entry["score"] = sum(entry["ranks"]) / len(entry["ranks"])
    return items


def rebuild_ab_learning(cfg: dict[str, Any] | None = None, db_path: Any = None) -> dict[str, dict[str, dict[str, float]]]:
    """Wipe and rebuild `ab_learning` from `metrics`. Returns {dimension: {value: {n, score, weight}}}."""
    items = item_scores(cfg, db_path)
    table: dict[str, dict[str, list[float]]] = {d: defaultdict(list) for d in DIMENSIONS}
    for entry in items.values():
        for dim in DIMENSIONS:
            if dim == "variant" and entry["ab_excluded"]:
                continue   # "Approve A only" was not a fair A/B draw: out of the variant comparison only
            table[dim][str(entry[dim] or "unknown")].append(entry["score"])

    out: dict[str, dict[str, dict[str, float]]] = {}
    ts = db.now()
    with db.db(db_path) as conn:
        conn.execute("DELETE FROM ab_learning")
        for dim, values in table.items():
            total = sum(sum(s) / len(s) for s in values.values()) or 0.0
            out[dim] = {}
            for value, scores in sorted(values.items()):
                mean = sum(scores) / len(scores)
                weight = (mean / total) if total > 0 else 1.0 / max(len(values), 1)
                out[dim][value] = {"n": len(scores), "score": mean, "weight": weight}
                conn.execute(
                    "INSERT INTO ab_learning (dimension, dimension_value, sample_count, normalized_score, weight, rebuilt_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (dim, value, len(scores), mean, weight, ts),
                )
    # The variant rows carry the allocation the scheduler will actually use (floor included).
    probs, _why = scheduler.variant_probabilities(cfg, db_path)
    with db.db(db_path) as conn:
        for value, p in probs.items():
            conn.execute(
                "UPDATE ab_learning SET weight = ? WHERE dimension = 'variant' AND dimension_value = ?", (p, value))
            if value in out.get("variant", {}):
                out["variant"][value]["weight"] = p
    return out


def mark_measured(now: datetime, cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[str]:
    """`posted` -> `measured` once every live row has its last snapshot, or its window has lapsed."""
    conf = settings(cfg)
    last_h = max(int(h) for h in conf["snapshot_hours"])
    lapse = timedelta(hours=last_h + float(conf["max_snapshot_late_hours"]))
    done: list[str] = []
    for item in db.get_items_by_stage("posted", db_path=db_path):
        live = [p for p in posting.get_posts_for_item(item["id"], db_path=db_path) if p["status"] == "live"]
        if not live:
            continue
        with db.db(db_path) as conn:
            have = {r["post_id"] for r in conn.execute(
                "SELECT post_id FROM metrics WHERE snapshot_hours = ?", (last_h,)).fetchall()}
        complete = all(p["id"] in have for p in live)
        lapsed = all(p["posted_at"] and now > clock.parse(p["posted_at"]) + lapse for p in live)
        if complete or lapsed:
            db.transition_stage(item["id"], "measured", db_path=db_path)
            db.log_stage(item["id"], "analytics", "success",
                         message="measured" if complete else f"measurement window lapsed without a {last_h}h snapshot",
                         db_path=db_path)
            done.append(item["id"])
    return done


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------

def run_analytics_stage(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    client: posting.MetaClient | None = None,
    now: datetime | None = None,
    alert: Any = None,
) -> dict[str, Any]:
    """Take every due snapshot, rebuild ab_learning, and move finished items to `measured`."""
    cfg = cfg or config.get_config()
    now = now or clock.utcnow()
    fetched: list[tuple[int, int]] = []
    failed: list[tuple[int, int, str]] = []

    due = due_snapshots(now, cfg, db_path)
    paused, why = posting.meta_paused(db_path)
    if due and paused:
        log.warning("analytics: meta is paused, %d snapshot(s) wait: %s", len(due), why)
    elif due:
        fetcher = InsightsFetcher(client or posting.MetaClient(cfg=cfg, db_path=db_path), cfg, db_path, alert=alert)
        for post, hours in due:
            try:
                fields, raw = fetcher.fetch(post)
            except RateLimitError as e:
                log.warning("analytics: meta rate limited; stopping this pass: %s", posting.redact(e))
                break
            except (PublishError, requests.RequestException) as e:
                # Left unrecorded: retried next cycle for as long as the snapshot window is open.
                failed.append((post["id"], hours, posting.redact(e)[:200]))
                log.warning("analytics: %s post %s %dh snapshot failed: %s", post["platform"], post["id"], hours,
                            posting.redact(e)[:200])
                continue
            record_snapshot(post["id"], hours, fields, raw, db_path=db_path)
            fetched.append((post["id"], hours))
            log.info("analytics: %s post %s %dh -> %s", post["platform"], post["id"], hours, fields)

    learning = rebuild_ab_learning(cfg, db_path)        # every run, from scratch
    measured = mark_measured(now, cfg, db_path)
    return {"snapshots": fetched, "failed": failed, "measured": measured,
            "learning_rows": sum(len(v) for v in learning.values()), "variant": learning.get("variant", {})}
