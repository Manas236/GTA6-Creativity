"""Make jobs use the normal item stages, without scheduling or posting."""

import json
import logging
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from . import config, cut, db, discover, download, render, runner

log = logging.getLogger(__name__)
ITEM_JOBS = {"remake_cut", "remake_hook", "make_from_discovered", "edit_caption_words", "edit_voiceover"}
ACTIONS = {"make", "clip_url"} | ITEM_JOBS


def source_from_url(value):
    try:
        url = urlsplit(value.strip())
        if url.scheme != "https" or url.username or url.password or url.port is not None:
            raise ValueError()
    except (AttributeError, ValueError):
        raise ValueError("Paste an HTTPS YouTube or Twitch URL.") from None
    host, path = url.hostname, url.path.rstrip("/")
    if host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"):
        video = parse_qs(url.query).get("v", [""])[0] if path == "/watch" and host != "youtu.be" else ""
        match = re.fullmatch(r"/(?:shorts/)?([\w-]{11})", path, flags=re.ASCII)
        if match and (host == "youtu.be" or path.startswith("/shorts/")):
            video = match[1]
        if re.fullmatch(r"[a-zA-Z0-9_-]{11}", video):
            return "youtube", video, f"https://www.youtube.com/watch?v={video}"
    if host in ("twitch.tv", "www.twitch.tv"):
        vod = re.fullmatch(r"/videos/(\d+)", path, flags=re.ASCII)
        if vod:
            video = str(int(vod[1]))
            return "twitch", f"vod:{video}", f"https://www.twitch.tv/videos/{video}"
        match = re.fullmatch(r"/[a-zA-Z0-9_]+/clip/([a-zA-Z0-9_-]+)", path)
    elif host == "clips.twitch.tv":
        match = re.fullmatch(r"/([a-zA-Z0-9_-]+)", path)
    else:
        match = None
    if match:
        return "twitch", match[1], f"https://clips.twitch.tv/{match[1]}"
    raise ValueError("Use a YouTube video or Short, Twitch clip, or Twitch VOD URL.")


def hint(value):
    if not value:
        return None
    match = re.fullmatch(r"([0-9]{1,5}):([0-5][0-9])", value) if isinstance(value, str) else None
    if not match:
        raise ValueError("Enter time hints as mm:ss.")
    return int(match[1]) * 60 + int(match[2])


def existing(platform, source_id, path):
    with db.db(path) as conn:
        row = conn.execute(
            "SELECT id FROM items WHERE (source=? AND source_id=?) "
            "OR (source='manual' AND source_platform=? AND source_id IN (?, ?))",
            (platform, source_id, platform, source_id, f"{platform}:{source_id}"),
        ).fetchone()
    return row["id"] if row else None


def insert_manual(payload, job_id, path):
    platform, source_id, url = source_from_url(payload["url"])
    item_id = "item_" + uuid.uuid4().hex[:12]
    from dashboard.store import transaction
    with transaction(path):
        prior = existing(platform, source_id, path)
        if prior:
            raise ValueError(f"Source already seen: /items/{prior}")
        db.insert_discovered_item({"id": item_id, "source": "manual", "source_id": f"{platform}:{source_id}",
                                   "source_url": url, "original_title": url}, path)
        db.transition_stage(item_id, "discovered", {"source_platform": platform, "creation_job": job_id,
                                                     "hint_start": hint(payload.get("start")),
                                                     "hint_end": hint(payload.get("end"))}, path)
    return item_id


def _produce(item_id, cfg, path, *, remake_cut=False):
    item = db.get_item(item_id, path)
    phase = item["stage"]

    def progress(stage):
        if runner.stopping(path):
            raise StopRequested()
        log.info("Make item %s stage %s", item_id, stage)
        runner.set_stage(stage, path)
        db.transition_stage(item_id, db.get_item(item_id, path)["stage"], {"creation_phase": stage}, path)

    try:
        if phase == "discovered":
            progress("download")
            ok, message = download.download_single_item(item, cfg, path)
            if not ok:
                if db.get_item(item_id, path)["stage"] == "discovered":
                    db.mark_failed(item_id, "download", message, db_path=path)
                return
            phase = "downloaded"
        if phase == "downloaded":
            progress("cut")
            ok, _ = cut.run_cut_item(item_id, cfg, path, exclude_current=remake_cut)
            if not ok:
                return
            phase = "cut"
        if phase == "cut":
            progress("render")
            ok, message, _ = render.run_render_item(item_id, cfg, path, progress=progress)
            stage = db.get_item(item_id, path)["stage"]
            if ok:
                db.transition_stage(item_id, "awaiting_approval", db_path=path)
            elif stage == "cut":
                db.mark_failed(item_id, "render", message, db_path=path)
    except StopRequested:
        raise
    except Exception as exc:
        log.exception("Make item %s failed", item_id)
        names = {"discovered": "download", "downloaded": "cut", "cut": "render"}
        db.mark_failed(item_id, names.get(phase, phase), str(exc), db_path=path)
    finally:
        with db.db(path) as conn:
            conn.execute("UPDATE items SET creation_phase=NULL WHERE id=?", (item_id,))


class StopRequested(Exception):
    pass


def run_jobs(cfg, path, only=None):
    from dashboard import commands
    from dashboard.store import transaction
    with db.db(path) as conn:
        jobs = conn.execute(
            "SELECT * FROM dashboard_commands WHERE status IN ('pending','running') "
            "AND action IN ('make','clip_url','remake_cut','remake_hook','make_from_discovered',"
            "'edit_caption_words','edit_voiceover') AND (? IS NULL OR id=?) "
            "ORDER BY rowid", (only, only),
        ).fetchall()
    demo = db.kv_get("dashboard:demo", db_path=path)
    for job in jobs:
        if runner.stopping(path):
            break
        job_id, action = job["id"], job["action"]
        payload = json.loads(job["payload"])
        try:
            with transaction(path) as conn:
                conn.execute("UPDATE dashboard_commands SET status='running' WHERE id=?", (job_id,))
                if action in ITEM_JOBS and job["status"] == "pending":
                    commands.apply(dict(job), cfg, path)
            with db.db(path) as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM items WHERE creation_job=?", (job_id,))]
            if not ids:
                if action == "make":
                    runner.set_stage("discover", path)
                    if demo:
                        from scripts.dashboard_demo_db import stub_discovery
                        ids = stub_discovery(payload["count"], job_id, path)
                    else:
                        ids = [x["id"] for x in discover.run_discovery(cfg, path, limit=payload["count"], job_id=job_id)]
                elif action == "clip_url":
                    ids = [insert_manual(payload, job_id, path)]
            for item_id in ids:
                if runner.stopping(path):
                    return
                if demo:
                    from scripts.dashboard_demo_db import stub_produce
                    stub_produce(item_id, path)
                else:
                    _produce(item_id, cfg, path, remake_cut=action == "remake_cut")
            status, message = "applied", f"Processed {len(ids)} item(s)." if ids else "No new sources found."
        except StopRequested:
            return
        except ValueError as exc:
            status, message = "refused", str(exc)
        except Exception as exc:
            log.exception("Make job %s failed", job_id)
            status, message = "failed", str(exc)
        with db.db(path) as conn:
            conn.execute("UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
                         (status, message[:500], db.now(), job_id))


def make_worker(job_id, db_path=None):
    from dashboard.store import initialize
    initialize(db_path)
    cfg = config.get_config(reload=True)
    # A runner that won the startup race can consume the same job at discover.
    while not runner.acquire_lock(db_path, *runner._lock_settings(cfg), "make"):
        with db.db(db_path) as conn:
            row = conn.execute("SELECT status FROM dashboard_commands WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] not in ("pending", "running"):
            return
        time.sleep(2)
    try:
        run_jobs(cfg, db_path, only=job_id)
    finally:
        runner.release_lock(db_path)
