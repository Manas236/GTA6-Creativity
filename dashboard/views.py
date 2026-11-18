"""Read models shared by the five pages; pipeline calculations run on a RAM snapshot."""

import copy
import json
import socket
import time
import re
from pathlib import Path

from pipeline import binding, clock, db, posting, rate_limit, scheduler, telegram_bot
from .commands import available_actions, display_settings
from .store import snapshot
from .presentation import posting_destination


def decode(raw):
    try:
        result = json.loads(raw or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def lock_status(path, cfg):
    from pipeline.runner import is_pid_alive

    raw = db.kv_get("runner:lock", db_path=path)
    lock = decode(raw)
    if not raw:
        return {"status": "none", "age": None, "host": None}
    try:
        age = max(0, time.time() - float(lock["heartbeat"]))
        stale = age > float(
            cfg.get("orchestrator", {}).get("heartbeat_stale_seconds", 120)
        )
        if lock.get("host") == socket.gethostname():
            stale = stale or not is_pid_alive(int(lock.get("pid") or 0))
        return {
            "status": "stale" if stale else "alive",
            "age": round(age),
            "host": lock.get("host"),
            "pid": lock.get("pid"),
            "remote": lock.get("host") != socket.gethostname(),
            "started_at": lock.get("started_at", 0),
            "token": lock.get("token"),
            "kind": lock.get("kind"),
        }
    except (KeyError, ValueError, TypeError):
        return {"status": "stale", "age": None, "host": None}


def state(path, cfg, media_root=None):
    with snapshot(path) as conn:
        counts = {
            r["stage"]: r["n"]
            for r in conn.execute("SELECT stage, COUNT(*) n FROM items GROUP BY stage")
        }
        items = [
            dict(r)
            for r in conn.execute(
                """SELECT id, source, author_name, original_title, stage,
            discovery_score, hook_text, hook_style, authenticity, chosen_variant, preview_only,
            error_stage, error_message, rejection_reason, created_at, stage_updated_at,
            cut_signal, source_platform, creation_job, creation_phase, cut_path, cut_start, source_url, voiceover_script
            FROM items ORDER BY created_at DESC"""
            )
        ]
        posts = [
            dict(r)
            for r in conn.execute(
                """SELECT id, item_id, platform, variant, slot_datetime, status,
            remote_id, remote_url, posted_at, hook_style, source, error_message, remote_container_id,
            attempt_count, publish_started_at FROM posts ORDER BY slot_datetime"""
            )
        ]
        metrics = [
            dict(r)
            for r in conn.execute("""SELECT m.*, p.item_id, p.platform, p.variant,
            p.hook_style, p.source, p.posted_at FROM metrics m JOIN posts p ON m.post_id=p.id
            ORDER BY m.fetched_at, m.post_id""")
        ]
        for metric in metrics:
            metric.pop("raw_json", None)
        commands = [
            dict(r)
            for r in conn.execute(
                "SELECT id, action, item_id, status, result, created_at, completed_at "
                "FROM dashboard_commands ORDER BY created_at DESC, rowid DESC"
            )
        ]
        grouped = {}
        for post in posts:
            grouped.setdefault(post["item_id"], []).append(post)
        for item in items:
            item["actions"] = available_actions(item, grouped.get(item["id"], []), path)
            item.pop("cut_path")
        runtime = decode(db.kv_get("dashboard:runtime", db_path=path))
        lock = lock_status(path, cfg)
        runtime_known = bool(
            lock["status"] == "alive"
            and runtime.get("at")
            and clock.parse(runtime["at"]).timestamp()
            >= float(lock.get("started_at") or 0)
        )
        from pipeline.runner import STOP_KEY
        stop = decode(db.kv_get(STOP_KEY, db_path=path))
        lock["stage"] = db.kv_get("runner:stage", db_path=path)
        lock["stopping"] = bool(lock["status"] == "alive" and stop.get("token") == lock.get("token")
                                and stop.get("at"))
        lock["force"] = bool(lock["stopping"] and not lock.get("remote")
                             and time.time() - stop["at"] >= 120)
        config_changed = runtime_known and runtime.get("settings") != display_settings(
            cfg
        )
        if runtime_known and isinstance(runtime.get("settings"), dict):
            cfg = copy.deepcopy(cfg)
            for section, values in runtime["settings"].items():
                cfg.setdefault(section, {}).update(values)
        lock.pop("token", None)
        profile, tz, slots = scheduler.active_profile(cfg)
        probabilities, reason = scheduler.variant_probabilities(cfg, path)
        limits = []
        for row in conn.execute("SELECT * FROM rate_limits"):
            record = dict(row)
            record["paused"], record["reason"] = rate_limit.is_source_paused(
                row["source"], path
            )
            limits.append(record)
        dashboard_paused = rate_limit.dashboard_meta_paused(path)
        meta_until, meta_reason = rate_limit.active_cooldown(posting.META_SOURCE, path)
        warnings = analytics_warnings(posts, metrics, cfg)
        next_at = min((p["slot_datetime"] for p in posts if p["slot_datetime"]
                       and p["status"] in ("pending", "uploading", "processing", "publishing")
                       and clock.parse(p["slot_datetime"]) >= clock.utcnow()), default=None)
        disk_bytes = 0
        if media_root:
            for file in media_root.rglob("*"):
                try:
                    if not file.is_symlink() and file.is_file():
                        disk_bytes += file.stat().st_size
                except OSError:
                    continue
        return {
            "status_line": {"dry_run": posting.is_dry_run(cfg),
                            "dry_run_mode": cfg.get("posting", {}).get("dry_run_mode"),
                            "runner": {"alive": "running", "stale": "stale"}.get(lock["status"], "stopped"),
                            "next_post_at": next_at,
                            "problems": counts.get("failed", 0) + len(warnings) + int(bool(meta_until))},
            "todo": {"to_review": counts.get("rendered", 0) + counts.get("awaiting_approval", 0),
                     "needs_decision": counts.get("needs_review", 0),
                     "failed": counts.get("failed", 0), "next_post_at": next_at},
            "analytics_warnings": warnings,
            "disk": {"media_bytes": disk_bytes},
            "runtime_known": runtime_known,
            "config_changed": config_changed,
            "at": db.now(),
            "stages": [{"stage": s, "count": counts.get(s, 0)} for s in db.STAGES],
            "ready": db.get_ready_buffer_count(path),
            "target": cfg.get("buffer", {}).get("target_ready_items", 6),
            "approval_mode": telegram_bot.effective_mode(cfg, path),
            "days_live": db.days_since_first_live(path),
            "dry_run": posting.is_dry_run(cfg),
            "posting_destination": posting_destination(cfg),
            "profile": profile,
            "timezone": str(tz),
            "slots": [s.strftime("%H:%M") for s in slots],
            "lock": lock,
            "runtime": runtime,
            "limits": limits,
            "meta_paused": dashboard_paused or bool(meta_until),
            "dashboard_paused": dashboard_paused,
            "meta_cooldown_until": meta_until,
            "meta_cooldown_reason": meta_reason,
            "variants": scheduler.variant_stats(path),
            "probabilities": probabilities,
            "variant_reason": reason,
            "cut_stats": db.cut_outcome_stats(path, hours=24),
            "items": items,
            "posts": posts,
            "metrics": metrics,
            "commands": commands,
            "interval": cfg.get("orchestrator", {}).get("loop_interval_seconds", 60),
            "demo": bool(db.kv_get("dashboard:demo", db_path=path)),
        }


def item_detail(path, item_id, cfg=None):
    with snapshot(path) as conn:
        item = db.get_item(item_id, path)
        if item is None:
            return None
        posts = posting.get_posts_for_item(item_id, path)
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM stage_log WHERE item_id=? ORDER BY created_at, id",
                (item_id,),
            )
        ]
        media = {
            v: bool(item.get(key) and Path(item[key]).is_file())
            for v, key in (
                ("a", "render_a_path"),
                ("b", "render_b_path"),
                ("cut", "cut_path"),
            )
        }
        version_rows = versions_for(item_id, path)
        media.update({version["variant"]: True for version in version_rows})
        voice = {**(cfg or {}).get("voiceover", {}), **decode(item.get("voiceover_override"))}
        for post in posts:
            metrics = [dict(row) for row in conn.execute(
                "SELECT * FROM metrics WHERE post_id=? ORDER BY fetched_at, id", (post["id"],)
            )]
            post["post_id"] = post["id"]
            post["metrics"] = [
                {"hours": m["age_hours"] if m["late"] else m["snapshot_hours"],
                 "window_h": m["snapshot_hours"] if m["snapshot_hours"] > 0 else None,
                 "late": bool(m["late"]), "age_hours": m["age_hours"],
                 **{key: m[key] for key in ("views", "likes", "comments", "shares")},
                 "saves": m["saves"] if m["saves"] is not None else decode(m["raw_json"]).get("saved"),
                 "reach": m["reach"] if m["reach"] is not None else decode(m["raw_json"]).get("reach")}
                for m in metrics]
            post["missed_windows"] = [warning["window_h"] for warning in analytics_warnings([post], metrics, cfg)]
        return {
            "versions": version_rows,
            "voiceover": {"script": item.get("voiceover_script") or "", "voice": voice.get("voice", "am_adam"),
                          "speed": float(voice.get("speed", 1.1))},
            "item": {
                k: v
                for k, v in item.items()
                if k not in ("source_transcript", "transcript_words")
            },
            "posts": posts,
            "caption": posting.caption_for_item(item, cfg),
            "caption_credit": posting.caption_credit(item),
            "caption_hashtags": item.get("caption_hashtags") if item.get("caption_body") is not None
                                else " ".join(re.findall(r"(?<!\w)#[\w]+", posting.caption_for_item(item, cfg))),
            "logs": logs,
            "media": media,
            "review": decode(item.get("hook_review")),
            "actions": available_actions(item, posts, path),
            "binding": {
                "hook": binding.hook_binding_problem(item),
                "voiceover": binding.voiceover_binding_problem(item),
            },
        }


def library_page(path, cfg, filters):
    """Return one page of matching item IDs in the dashboard's display timezone."""
    from datetime import date

    def positive_int(value, default):
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return default

    def date_filter(name):
        try:
            return date.fromisoformat(filters.get(name, ""))
        except ValueError:
            return None

    size = positive_int(filters.get("size"), 25)
    if size not in (25, 50, 100):
        size = 25
    _, tz, *_ = scheduler.active_profile(cfg)
    first, last = date_filter("from"), date_filter("to")
    search = filters.get("q", "").casefold()
    with db.db(path) as conn:
        rows = conn.execute(
            """SELECT i.id, i.created_at, i.original_title, i.author_name, i.hook_text,
            i.stage, COALESCE(NULLIF(i.source_platform, ''), i.source) AS platform,
            COALESCE(NULLIF(i.authenticity, ''), 'unverified') AS authenticity, i.cut_signal AS signal,
            EXISTS(SELECT 1 FROM posts p WHERE p.item_id=i.id AND p.posted_at IS NOT NULL) AS posted
            FROM items i ORDER BY i.created_at DESC, i.id"""
        )
        matched = []
        for row in rows:
            if any(filters.get(key) and filters[key] != row[key] for key in ("stage", "platform", "authenticity", "signal")):
                continue
            if filters.get("posted") in ("yes", "no") and bool(row["posted"]) != (filters["posted"] == "yes"):
                continue
            if search and search not in " ".join(str(row[key] or "") for key in (
                "original_title", "author_name", "hook_text", "id"
            )).casefold():
                continue
            if first or last:
                day = clock.parse(row["created_at"]).astimezone(tz).date()
                if (first and day < first) or (last and day > last):
                    continue
            matched.append(row["id"])
    pages = max(1, (len(matched) + size - 1) // size)
    page = min(pages, positive_int(filters.get("page"), 1))
    return {"ids": matched[(page - 1) * size:page * size], "total": len(matched), "page": page, "pages": pages}


def analytics_warnings(posts, metrics, cfg, now=None):
    from datetime import timedelta
    from pipeline import analytics

    now = now or clock.utcnow()
    conf = analytics.settings(cfg)
    recorded = {(m["post_id"], m["snapshot_hours"]): m for m in metrics}
    warnings = []
    for post in posts:
        if post["status"] != "live" or not post.get("posted_at"):
            continue
        for hours in conf["snapshot_hours"]:
            missed_at = clock.parse(post["posted_at"]) + timedelta(
                hours=hours + conf["max_snapshot_late_hours"])
            metric = recorded.get((post["id"], hours))
            if now > missed_at and (metric is None or metric["late"]):
                warnings.append({"item_id": post["item_id"], "post_id": post["id"], "window_h": hours,
                                 "missed_at": clock.iso(missed_at), "late_snapshot_taken": metric is not None})
    return warnings


def versions_for(item_id, path):
    labels = {"P1": "Clean", "P2": "Punchy", "P3": "Full hype"}
    versions = []
    for render in db.renders_for_item(item_id, path):
        try:
            with Path(render["meta_path"]).open(encoding="utf-8") as stream:
                meta = json.load(stream)
        except (OSError, ValueError, TypeError):
            meta = {}
        preset = render["preset"]
        variant = render["variant"]
        versions.append({"variant": variant, "preset": preset, "label": labels.get(preset, preset),
                         "url": f"/media/{item_id}/{variant}", "meta": meta if isinstance(meta, dict) else {}})
    return versions


def schedule(path, cfg, first, days):
    from datetime import datetime, timedelta
    profile, zone, slots = scheduler.active_profile(cfg)
    with snapshot(path) as conn:
        scheduled = {clock.iso(clock.parse(row["slot_datetime"])): dict(row)
                     for row in conn.execute("""
                         SELECT p.slot_datetime, i.id AS item_id, i.original_title AS title
                         FROM posts p JOIN items i ON p.item_id = i.id
                         WHERE i.stage = 'queued' AND p.slot_datetime IS NOT NULL
                     """)}
        return {"slots": [{"at": at, "profile": profile,
                           "item_id": scheduled.get(at, {}).get("item_id"),
                           "title": scheduled.get(at, {}).get("title")}
                          for offset in range(days)
                          for slot in slots
                          for at in [clock.iso(datetime.combine(first + timedelta(days=offset), slot, zone))]]}


def discovered(path):
    now = clock.utcnow()
    with snapshot(path):
        return [{"item_id": item["id"], "title": item["original_title"], "source": item["source"],
                 "score": item["discovery_score"],
                 "age_h": max(0, (now - clock.parse(item.get("upload_time") or item["created_at"]))
                              .total_seconds() / 3600),
                 "url": item["source_url"]}
                for item in db.get_items_by_stage("discovered", db_path=path)
                if not item.get("creation_job") and db.is_releasable(item)]
