"""Discovery stage: search YouTube for viral GTA 6 video content with real metadata and recency decay."""
from __future__ import annotations

import logging
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any

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
# YouTube Discovery with Real Metadata & Official Channel Down-Weighting
# ---------------------------------------------------------------------------

def discover_youtube(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[dict[str, Any]]:
    cfg = cfg or config.get_config()
    sources_cfg = cfg.get("sources", {})
    yt_cfg = sources_cfg.get("youtube", {})
    if not yt_cfg.get("enabled", True):
        return []

    paused, why = rate_limit.is_source_paused("youtube", db_path)
    if paused:
        log.warning("YouTube discovery skipped: %s", why)
        return []

    queries = yt_cfg.get("queries", ["GTA 6 gameplay", "GTA 6 leak"])
    date_queries = yt_cfg.get("date_queries", ["GTA 6", "GTA VI gameplay"])
    max_per_query = yt_cfg.get("max_results_per_query", 10)
    max_duration = yt_cfg.get("max_duration_seconds", 600)
    gap_s = float(yt_cfg.get("metadata_fetch_gap_seconds", 0.3))

    official_cfg = sources_cfg.get("official_channels", {})
    official_channel_ids = set(official_cfg.get("channel_ids", []))
    official_weight = float(official_cfg.get("weight", 0.20))
    standard_yt_weight = float(yt_cfg.get("weight", 1.0))

    ydl_flat_opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 15,
        "ignoreerrors": True,
    }

    ydl_meta_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "ignoreerrors": True,
    }

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()

    try:
        with yt_dlp.YoutubeDL(ydl_flat_opts) as ydl:
            # 1. Relevance queries
            for q in queries:
                search_target = f"ytsearch{max_per_query}:{q}"
                try:
                    res = ydl.extract_info(search_target, download=False)
                    if res and "entries" in res:
                        for entry in res["entries"]:
                            if not entry:
                                continue
                            vid_id = entry.get("id")
                            if vid_id and vid_id not in seen_ids:
                                seen_ids.add(vid_id)
                                candidate_ids.append(vid_id)
                except Exception as e:
                    log.warning("Relevance search for '%s' failed: %s", q, e)

            # 2. Date-sorted queries
            for dq in date_queries:
                encoded = urllib.parse.quote_plus(dq)
                # YouTube search filter sp=CAI%253D sorts by upload date
                date_url = f"https://www.youtube.com/results?search_query={encoded}&sp=CAI%253D"
                try:
                    res = ydl.extract_info(date_url, download=False)
                    if res and "entries" in res:
                        added = 0
                        for entry in res["entries"]:
                            if not entry or added >= max_per_query:
                                break
                            vid_id = entry.get("id")
                            if vid_id and vid_id not in seen_ids:
                                seen_ids.add(vid_id)
                                candidate_ids.append(vid_id)
                                added += 1
                except Exception as e:
                    log.warning("Date-sorted search for '%s' failed: %s", dq, e)

        rate_limit.record_success("youtube", db_path)
    except Exception as e:
        rate_limit.record_error("youtube", str(e), pause_hours=1.0, db_path=db_path)
        log.error("YouTube candidate search error: %s", e)
        return []

    log.info("Harvested %d unique video candidate IDs. Fetching real metadata...", len(candidate_ids))

    discovered: list[dict[str, Any]] = []
    now_dt = datetime.now(timezone.utc)
    min_age_h = cfg.get("discovery", {}).get("min_age_hours", 0.5)

    with yt_dlp.YoutubeDL(ydl_meta_opts) as ydl_meta:
        for vid_id in candidate_ids:
            try:
                time.sleep(gap_s)
                info = ydl_meta.extract_info(f"https://www.youtube.com/watch?v={vid_id}", download=False)
                if not info:
                    continue

                duration = float(info.get("duration") or 0.0)
                if duration > max_duration or duration < 5.0:
                    continue

                upload_ts = info.get("timestamp")
                if upload_ts:
                    upload_dt = datetime.fromtimestamp(upload_ts, timezone.utc)
                else:
                    date_str = info.get("upload_date")
                    if date_str and len(date_str) == 8:
                        upload_dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
                    else:
                        upload_dt = now_dt

                age_h = max(_age_hours(upload_dt, now_dt), min_age_h)
                views = int(info.get("view_count") or 0)
                raw_vel = views / age_h

                channel_id = info.get("channel_id") or ""
                author = info.get("uploader") or info.get("channel") or "Unknown"

                # Check if official channel
                is_official = channel_id in official_channel_ids
                effective_weight = official_weight if is_official else standard_yt_weight

                discovered.append({
                    "id": f"item_{uuid.uuid4().hex[:12]}",
                    "source": "youtube",
                    "source_id": vid_id,
                    "channel_id": channel_id,
                    "source_url": f"https://www.youtube.com/watch?v={vid_id}",
                    "author_name": author,
                    "original_title": info.get("title") or "GTA 6 Video",
                    "original_duration": duration,
                    "view_count": views,
                    "upvote_count": int(info.get("like_count") or 0),
                    "comment_count": int(info.get("comment_count") or 0),
                    "upload_time": upload_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "raw_velocity": raw_vel,
                    "age_hours": age_h,
                    "is_official": is_official,
                    "effective_weight": effective_weight,
                })
            except Exception as e:
                # E.g. age-restricted, private, or deleted
                log.debug("Skipping candidate %s: %s", vid_id, e)
                continue

    return discovered


# ---------------------------------------------------------------------------
# Scoring & Insertion
# ---------------------------------------------------------------------------

def run_discovery(cfg: dict[str, Any] | None = None, db_path: Any = None) -> list[dict[str, Any]]:
    """Execute discovery, compute percentile discovery scores with real decay, and write to DB."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    all_candidates = discover_youtube(cfg, db_path)
    if not all_candidates:
        log.info("No candidates discovered.")
        return []

    disc_cfg = cfg.get("discovery", {})
    formula_str = disc_cfg.get(
        "score_formula",
        "percentile_rank * (0.5 ** (age_hours / half_life_hours)) * effective_weight",
    )
    half_life = float(disc_cfg.get("half_life_hours", 36.0))

    # Rolling pool for percentile rank calculation
    db_velocities = db.get_recent_source_velocities("youtube", limit=100, db_path=db_path)
    combined_pool = db_velocities + [i["raw_velocity"] for i in all_candidates]

    scored_items: list[dict[str, Any]] = []
    for item in all_candidates:
        p_rank = _compute_percentile(item["raw_velocity"], combined_pool)
        age_h = item["age_hours"]
        eff_weight = item["effective_weight"]
        decay = 0.5 ** (age_h / half_life)

        ctx = {
            "percentile_rank": p_rank,
            "age_hours": age_h,
            "half_life_hours": half_life,
            "effective_weight": eff_weight,
            "source_weight": eff_weight,
        }
        score = config.evaluate_formula(formula_str, ctx)
        item["percentile_rank"] = p_rank
        item["decay_factor"] = decay
        item["discovery_score"] = score
        scored_items.append(item)

    # Sort candidates by discovery_score DESC
    scored_items.sort(key=lambda x: x["discovery_score"], reverse=True)

    # Insert into database
    inserted_count = 0
    for item in scored_items:
        if db.insert_discovered_item(item, db_path=db_path):
            inserted_count += 1

    log.info("Discovery complete: %d scored, %d newly inserted to DB", len(scored_items), inserted_count)
    return scored_items
