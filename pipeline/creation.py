"""Targeted creation jobs. Each item uses the ordinary download, cut and render stages."""
from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import cut, db, discover, download, hooks, render

log = logging.getLogger(__name__)


def source_url(value):
    try:
        url = urlsplit(value.strip())
        host = url.hostname
        if url.scheme not in {"http", "https"} or url.netloc.lower() != host:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("Enter a YouTube video or Short, Twitch clip or Twitch VOD URL.") from None
    path = url.path.strip("/").split("/")
    if host == "youtu.be" and len(path) == 1:
        video = path[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        video = (parse_qs(url.query).get("v", [""])[0] if path == ["watch"]
                 else path[1] if len(path) == 2 and path[0] == "shorts" else "")
    else:
        video = ""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        return "youtube", video, f"https://www.youtube.com/watch?v={video}"
    if host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        if len(path) == 2 and path[0] == "videos" and re.fullmatch(r"\d+", path[1]):
            return "twitch_vod", path[1], f"https://www.twitch.tv/videos/{path[1]}"
        if len(path) == 3 and path[1] == "clip":
            host, path = "clips.twitch.tv", path[2:]
    if host == "clips.twitch.tv" and len(path) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", path[0]):
        return "twitch_clip", path[0], f"https://clips.twitch.tv/{path[0]}"
    raise ValueError("Enter a YouTube video or Short, Twitch clip or Twitch VOD URL.")


def hint(value):
    if value in ("", None):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,5}:[0-5]\d", value):
        raise ValueError("Use mm:ss for the start and end hints.")
    minutes, seconds = map(int, value.split(":"))
    return minutes * 60 + seconds


def manual_details(payload, path):
    kind, sid, link = source_url(payload.get("url") or "")
    start, end = hint(payload.get("start")), hint(payload.get("end"))
    if end is not None and end <= (start or 0):
        raise ValueError("The end hint must be after the start.")
    existing = discover.source_item("twitch" if kind == "twitch_clip" else kind, sid, path)
    if existing:
        raise ValueError(f"Source already exists. /items/{existing['id']}")
    return kind, sid, link, start, end


def insert_manual(payload, job_id, path):
    kind, sid, url, start, end = manual_details(payload, path)
    item_id = f"item_{uuid.uuid4().hex[:12]}"
    with db.db(path) as conn:
        db.insert_discovered_item({
            "id": item_id, "source": "manual", "source_id": f"{kind}:{sid}",
            "source_url": url, "original_title": url,
        }, path)
        conn.execute("UPDATE items SET manual_kind=?, hint_start=?, hint_end=?, dashboard_job=? WHERE id=?",
                     (kind, start, end, job_id, item_id))
    return item_id


def process_item(item_id, cfg, path, enter_stage):
    def step(stage):
        if not enter_stage(stage):
            return False
        with db.db(path) as conn:
            conn.execute("UPDATE items SET dashboard_step=? WHERE id=?", (stage, item_id))
        return True

    stages = (
        ("discovered", "download", lambda: download.download_single_item(db.get_item(item_id, path), cfg, path)),
        ("downloaded", "cut", lambda: cut.run_cut_item(item_id, cfg, path)),
        ("cut", "render", lambda: render.run_render_item(item_id, cfg, path, enter_stage=step)),
    )
    for current, stage, fn in stages:
        if db.get_item(item_id, path)["stage"] != current:
            continue
        if not step(stage):
            return False
        try:
            outcome = fn()
            if not outcome[0] and outcome[1] == "creation stopped":
                return False
            if not outcome[0] and stage == "render" and hooks.is_transient_result(outcome[1]):
                with db.db(path) as conn:
                    conn.execute("UPDATE items SET dashboard_step=? WHERE id=?",
                                 (outcome[1], item_id))
            elif not outcome[0] and db.get_item(item_id, path)["stage"] == current:
                db.mark_failed(item_id, stage, outcome[1], path)
        except Exception as exc:
            log.exception("Creation job item %s failed at %s", item_id, stage)
            db.mark_failed(item_id, stage, f"{type(exc).__name__}: {exc}", path)
    item = db.get_item(item_id, path)
    if item["stage"] == "rendered":
        if not step("approval hand-off"):
            return False
        db.transition_stage(item_id, "awaiting_approval", db_path=path)
    return True
