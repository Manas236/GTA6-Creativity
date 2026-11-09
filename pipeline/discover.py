"""Discovery stage: search YouTube and Reddit for viral GTA 6 video content."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
import yt_dlp

from . import config, db, rate_limit
from .errors import DiscoveryError, RateLimitError

log = logging.getLogger(__name__)


def _age_hours(upload_dt: datetime, now_dt: datetime | None = None) -> float:
    now_dt = now_dt or datetime.now(timezone.utc)
    diff = (now_dt - upload_dt).total_seconds()
    return max(diff / 3600.0, 0.1)


def _compute_percentile(val: float, pool: list[float]) -> float:
    if not pool:
        return 0.5
    count_below = sum(1 for p in pool if p <= val)
    return max(0.01, min(1.0, count_below / float(len(pool))))


# ---------------------------------------------------------------------------
# YouTube Discovery
# ---------------------------------------------------------------------------

def discover_youtube(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[dict[str, Any]]:
    cfg = cfg or config.get_config()
    yt_cfg = cfg.get("sources", {}).get("youtube", {})
    if not yt_cfg.get("enabled", True):
        return []

    paused, why = rate_limit.is_source_paused("youtube", db_path)
    if paused:
        log.warning("YouTube discovery skipped: %s", why)
        return []

    queries = yt_cfg.get("queries", ["GTA 6 gameplay", "GTA 6 leak"])
    max_per_query = yt_cfg.get("max_results_per_query", 10)
    max_duration = yt_cfg.get("max_duration_seconds", 600)
    min_age_h = cfg.get("discovery", {}).get("min_age_hours", 0.5)

    ydl_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "ignoreerrors": True,
    }

    discovered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    now_dt = datetime.now(timezone.utc)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for q in queries:
                search_target = f"ytsearch{max_per_query}:{q}"
                try:
                    res = ydl.extract_info(search_target, download=False)
                except Exception as e:
                    err_msg = str(e)
                    if "429" in err_msg or "Too Many Requests" in err_msg or "Sign in" in err_msg:
                        rate_limit.record_error("youtube", f"Rate limited: {err_msg}", pause_hours=3.0, db_path=db_path)
                        raise RateLimitError("youtube", pause_seconds=10800.0, message=err_msg)
                    log.warning("YouTube search '%s' failed: %s", q, e)
                    continue

                if not res or "entries" not in res:
                    continue

                for entry in res["entries"]:
                    if not entry:
                        continue
                    vid_id = entry.get("id")
                    if not vid_id or vid_id in seen_ids:
                        continue
                    seen_ids.add(vid_id)

                    duration = float(entry.get("duration") or 0.0)
                    if duration > max_duration or (duration > 0 and duration < 8.0):
                        continue

                    # Parse upload timestamp
                    upload_ts = entry.get("timestamp")
                    if upload_ts:
                        upload_dt = datetime.fromtimestamp(upload_ts, timezone.utc)
                    else:
                        date_str = entry.get("upload_date")
                        if date_str and len(date_str) == 8:
                            upload_dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                        else:
                            upload_dt = now_dt

                    age_h = max(_age_hours(upload_dt, now_dt), min_age_h)
                    views = int(entry.get("view_count") or 0)
                    raw_vel = views / age_h

                    discovered.append({
                        "id": f"item_{uuid.uuid4().hex[:12]}",
                        "source": "youtube",
                        "source_id": vid_id,
                        "source_url": entry.get("url") or f"https://www.youtube.com/watch?v={vid_id}",
                        "author_name": entry.get("channel") or entry.get("uploader") or "Unknown Channel",
                        "original_title": entry.get("title") or "GTA 6 Video",
                        "original_duration": duration,
                        "view_count": views,
                        "upvote_count": int(entry.get("like_count") or 0),
                        "comment_count": int(entry.get("comment_count") or 0),
                        "upload_time": upload_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "raw_velocity": raw_vel,
                        "age_hours": age_h,
                    })

        rate_limit.record_success("youtube", db_path)
    except RateLimitError:
        raise
    except Exception as e:
        rate_limit.record_error("youtube", str(e), pause_hours=1.0, db_path=db_path)
        log.error("YouTube discovery error: %s", e)

    return discovered


# ---------------------------------------------------------------------------
# Reddit Discovery
# ---------------------------------------------------------------------------

def discover_reddit(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[dict[str, Any]]:
    cfg = cfg or config.get_config()
    reddit_cfg = cfg.get("sources", {}).get("reddit", {})
    if not reddit_cfg.get("enabled", True):
        return []

    paused, why = rate_limit.is_source_paused("reddit", db_path)
    if paused:
        log.warning("Reddit discovery skipped: %s", why)
        return []

    subreddits = reddit_cfg.get("subreddits", ["GTA6", "GTA"])
    limit = reddit_cfg.get("limit", 25)
    listing = reddit_cfg.get("listing", "hot")
    min_age_h = cfg.get("discovery", {}).get("min_age_hours", 0.5)
    user_agent = config.secret("REDDIT_USER_AGENT", "GTA6ClippingBot/1.0 (public JSON)")

    headers = {"User-Agent": user_agent}
    discovered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    now_dt = datetime.now(timezone.utc)

    for sub in subreddits:
        url = f"https://www.reddit.com/r/{sub}/{listing}.json?limit={limit}"
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 429:
                rate_limit.record_error("reddit", "HTTP 429 Too Many Requests", pause_hours=2.0, db_path=db_path)
                raise RateLimitError("reddit", pause_seconds=7200.0, message="HTTP 429")
            if resp.status_code != 200:
                log.warning("Reddit r/%s returned status %d", sub, resp.status_code)
                continue

            data = resp.json().get("data", {}).get("children", [])
            for child in data:
                post = child.get("data", {})
                post_id = post.get("id")
                if not post_id or post_id in seen_ids or post.get("over_18"):
                    continue
                seen_ids.add(post_id)

                # We want video clips: is_video or v.redd.it or youtube links
                is_video = post.get("is_video", False)
                url_field = post.get("url", "")
                media_info = post.get("media") or {}
                has_video_media = bool(media_info.get("reddit_video") or "v.redd.it" in url_field or "youtube.com" in url_field or "youtu.be" in url_field)

                if not (is_video or has_video_media):
                    continue

                duration = 0.0
                if media_info.get("reddit_video"):
                    duration = float(media_info["reddit_video"].get("duration") or 0.0)

                created_utc = float(post.get("created_utc", now_dt.timestamp()))
                upload_dt = datetime.fromtimestamp(created_utc, timezone.utc)
                age_h = max(_age_hours(upload_dt, now_dt), min_age_h)

                score = int(post.get("score") or 0)
                comments = int(post.get("num_comments") or 0)
                # Raw velocity formula for Reddit: (upvotes + comments * 2) / age_h
                raw_vel = (score + (comments * 2.0)) / age_h

                discovered.append({
                    "id": f"item_{uuid.uuid4().hex[:12]}",
                    "source": "reddit",
                    "source_id": post_id,
                    "source_url": f"https://www.reddit.com{post.get('permalink')}" if post.get("permalink") else url_field,
                    "author_name": post.get("author") or f"r/{sub}",
                    "original_title": post.get("title") or "GTA 6 Reddit Clip",
                    "original_duration": duration,
                    "view_count": score + comments,
                    "upvote_count": score,
                    "comment_count": comments,
                    "upload_time": upload_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "raw_velocity": raw_vel,
                    "age_hours": age_h,
                    "subreddit": sub,
                })

            rate_limit.record_success("reddit", db_path)
        except RateLimitError:
            raise
        except Exception as e:
            rate_limit.record_error("reddit", str(e), pause_hours=1.0, db_path=db_path)
            log.warning("Reddit r/%s query error: %s", sub, e)

    return discovered


# ---------------------------------------------------------------------------
# Aggregation & Scoring
# ---------------------------------------------------------------------------

def run_discovery(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[dict[str, Any]]:
    """Execute discovery across enabled sources, compute percentile discovery scores, and write to DB."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    yt_items = discover_youtube(cfg, db_path)
    reddit_items = discover_reddit(cfg, db_path)
    all_candidates = yt_items + reddit_items

    if not all_candidates:
        return []

    disc_cfg = cfg.get("discovery", {})
    formula_str = disc_cfg.get(
        "score_formula",
        "percentile_rank * (0.5 ** (age_hours / half_life_hours)) * source_weight",
    )
    half_life = float(disc_cfg.get("half_life_hours", 36.0))
    sources_cfg = cfg.get("sources", {})

    # Group candidates by source to compute percentile ranks within each source
    candidates_by_source: dict[str, list[dict[str, Any]]] = {}
    for item in all_candidates:
        candidates_by_source.setdefault(item["source"], []).append(item)

    scored_items: list[dict[str, Any]] = []

    for src, items in candidates_by_source.items():
        src_weight = float(sources_cfg.get(src, {}).get("weight", 1.0))
        # Fetch historical velocities from DB for rolling pool
        db_velocities = db.get_recent_source_velocities(src, limit=100, db_path=db_path)
        combined_pool = db_velocities + [i["raw_velocity"] for i in items]

        for item in items:
            p_rank = _compute_percentile(item["raw_velocity"], combined_pool)
            ctx = {
                "percentile_rank": p_rank,
                "age_hours": item["age_hours"],
                "half_life_hours": half_life,
                "source_weight": src_weight,
            }
            score = config.evaluate_formula(formula_str, ctx)
            item["percentile_rank"] = p_rank
            item["discovery_score"] = score
            scored_items.append(item)

    # Insert into database (idempotent ON CONFLICT)
    inserted_items: list[dict[str, Any]] = []
    for item in scored_items:
        inserted = db.insert_discovered_item(item, db_path=db_path)
        if inserted:
            inserted_items.append(item)

    log.info("Discovery complete: %d fetched, %d recorded in DB", len(scored_items), len(inserted_items))
    return scored_items
