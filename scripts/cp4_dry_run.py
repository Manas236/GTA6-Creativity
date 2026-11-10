"""CP4 end-to-end dry run: ~10 simulated days in seconds, nothing real touched.

One command, no prompts:

    python scripts/cp4_dry_run.py            # 10 days, prints a report, exit 1 on any invariant break
    python scripts/cp4_dry_run.py --days 12 --seed 3

What is real and what is fake
  * REAL: every CP3/CP4 stage function (settle, Telegram approval bot, scheduler, the two-phase
    Meta publisher, analytics, cleanup, the runner's stage isolation, the healthcheck ping logic),
    the SQLite schema and triggers, and pipeline/clock.py, driven by a FakeClock in 30-minute ticks.
  * FAKE: the network. Meta Graph (Instagram + Facebook Reels, insights, recent media), Telegram
    (a scripted reviewer who taps a button ~45 minutes after each card) and Healthchecks.io are
    in-process fakes; every other HTTP request raises. Secrets are fake values - .env is never read.
  * ISOLATED: a temporary database and a temporary media folder; the real pipeline.db and media/
    are checked to be untouched at the end.
  * Inside the simulation `app.dry_run` is false (so the live state machine, the first live post,
    week 1, the bandit and cleanup can all happen); config.yaml is not modified and still says true.

What it simulates
  approvals over Telegram (approve / reject / approve-A-only) -> the switch to auto approval 7 days
  after the first live post; two slots a day; the laptop asleep for ~34 h (missed slots re-slotted,
  no burst); a crash right after Instagram published (settled from the remote, never re-published);
  variant allocation drawn at the slot across the week-1 boundary; 24h/72h insights; the bandit
  shifting to the better variant; cleanup after 7 days; a preview_only item and a rejected item that
  must never be posted.

Invariants (any break -> exit code 1)
  1. no double post: at most one publish per item and platform, as seen by the fake Meta;
  2. no burst: two different items' first publishes are >= posting.min_gap_hours apart and no local
     day has more than posting.daily_post_target items;
  3. nothing posted that may not be: every publish is for a `queued`, approved, non-preview_only item;
  4. cleanup never deletes a file an open post still needs;
  5. the uploaded video is the variant the row says, and no undecided ("?") row is ever uploaded;
  6. the real pipeline.db and media/ are untouched.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import random
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import (  # noqa: E402
    analytics, cleanup, clock, config, db, healthcheck, posting, runner, scheduler, telegram_bot,
)

START = "2026-11-12T00:00:00Z"
IG_USER, FB_PAGE, CHAT = "17841400000000000", "100000000000000", "424242"
FAKE_SECRETS = {
    "IG_USER_ID": IG_USER, "IG_ACCESS_TOKEN": "fake-ig-token", "FB_PAGE_ID": FB_PAGE,
    "FB_PAGE_ACCESS_TOKEN": "fake-fb-token", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": CHAT,
    "HEALTHCHECK_URL": "https://hc-ping.invalid/fake-check", "GEMINI_API_KEY": "",
    "TWITCH_CLIENT_ID": "", "TWITCH_CLIENT_SECRET": "",
}


class CrashError(RuntimeError):
    """Stands in for the process dying mid-request (not an error the publisher handles)."""


class Resp:
    def __init__(self, payload: Any, status: int = 200):
        self._p, self.status_code = payload, status
        self.text = json.dumps(payload)

    def json(self) -> Any:
        return self._p


def graph_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


# ---------------------------------------------------------------------------------------------
# Fake Meta Graph API
# ---------------------------------------------------------------------------------------------

class FakeMeta:
    """Instagram Reels (resumable container + publish) and Facebook Page Reels (start / upload /
    finish), insights, recent media and status reads - and a ledger of every publish."""

    def __init__(self, db_path: Path, rng: random.Random, crash_item: str | None, lost_item: str | None = None):
        self.db_path, self.rng, self.crash_item, self.lost_item = db_path, rng, crash_item, lost_item
        self.attempts: Counter = Counter()                  # publish CALLS per (item, platform)
        self.n = 0
        self.containers: dict[str, dict[str, Any]] = {}     # IG container / FB video id -> info
        self.media: dict[str, dict[str, Any]] = {}          # published id -> info
        self.publishes: list[dict[str, Any]] = []           # the ledger the invariants read
        self.violations: list[str] = []

    def _id(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def _item_state(self, item_id: str) -> dict[str, Any]:
        return db.get_item(item_id, db_path=self.db_path) or {}

    def _record_publish(self, platform: str, cid: str) -> str:
        info = self.containers[cid]
        item = self._item_state(info["item_id"])
        rows = [r for r in posting.get_posts_for_item(info["item_id"], db_path=self.db_path)
                if r["platform"] == platform]
        row_variant = rows[0]["variant"] if rows else "?"
        if item.get("stage") != "queued" or int(item.get("preview_only") or 0) or not item.get("approval_type"):
            self.violations.append(f"published {info['item_id']} on {platform} while it was "
                                   f"stage={item.get('stage')} preview_only={item.get('preview_only')}")
        if info.get("variant") not in ("A", "B") or info.get("variant") != row_variant:
            self.violations.append(f"{info['item_id']} {platform}: uploaded variant {info.get('variant')} "
                                   f"but the row says {row_variant}")
        mid = self._id("m" if platform == "instagram" else "fbv")
        when = clock.utcnow()
        self.media[mid if platform == "instagram" else cid] = {**info, "platform": platform, "at": when}
        info["published"] = True
        self.publishes.append({"item_id": info["item_id"], "platform": platform, "at": when,
                               "variant": info.get("variant"), "remote_id": mid if platform == "instagram" else cid})
        return mid

    def __call__(self, method: str, url: str, **kw: Any) -> Resp:
        data = kw.get("data") if isinstance(kw.get("data"), dict) else {}
        params = kw.get("params") or {}
        path = url.split("://", 1)[-1].split("/", 2)[-1]      # "v25.0/..." or "ig-api-upload/..."
        # --- uploads (rupload): remember which render went into which container
        if "/ig-api-upload/" in url or "/video-upload/" in url:
            cid = url.rstrip("/").rsplit("/", 1)[-1]
            fh = kw.get("data")
            name = Path(getattr(fh, "name", "")).name
            item_id, _, var = name.rpartition("_")
            self.containers.setdefault(cid, {})
            self.containers[cid].update(item_id=item_id, variant=var.split(".")[0])
            return Resp({"success": True})
        # --- Instagram
        if method == "POST" and path.endswith(f"{IG_USER}/media"):
            cid = self._id("c")
            self.containers[cid] = {"caption": data.get("caption", ""), "published": False}
            return Resp({"id": cid})
        if method == "POST" and path.endswith(f"{IG_USER}/media_publish"):
            cid = data.get("creation_id")
            item_id = self.containers.get(cid, {}).get("item_id")
            self.attempts[(item_id, "instagram")] += 1
            if self.containers.get(cid, {}).get("published"):  # what Instagram does with a used container
                return Resp({"error": {"message": "this media has already been published", "code": 9007}}, 400)
            mid = self._record_publish("instagram", cid)
            self.containers[cid]["media_id"] = mid
            if self.crash_item and item_id == self.crash_item:
                self.crash_item = None                       # crash once, AFTER Meta published
                raise CrashError("the runner died right after Instagram published")
            if self.lost_item and item_id == self.lost_item:
                self.lost_item = None                        # published, but the answer is lost
                raise requests.ConnectionError("connection reset by peer")
            return Resp({"id": mid})
        if method == "GET" and path.endswith(f"{IG_USER}/media"):
            items = [{"id": k, "caption": self.containers_caption(v), "timestamp": graph_ts(v["at"]),
                      "permalink": f"https://instagram.invalid/reel/{k}", "media_type": "VIDEO"}
                     for k, v in self.media.items() if v["platform"] == "instagram"]
            return Resp({"data": sorted(items, key=lambda m: m["timestamp"], reverse=True)[:25]})
        # --- Facebook
        if method == "POST" and path.endswith(f"{FB_PAGE}/video_reels"):
            if data.get("upload_phase") == "start":
                vid = self._id("v")
                self.containers[vid] = {"caption": "", "published": False}
                return Resp({"video_id": vid})
            vid = data.get("video_id")
            self.attempts[(self.containers.get(vid, {}).get("item_id"), "facebook")] += 1
            if self.containers.get(vid, {}).get("published"):
                return Resp({"error": {"message": "video already published", "code": 6000}}, 400)
            self.containers[vid]["caption"] = data.get("description", "")
            self._record_publish("facebook", vid)
            return Resp({"success": True})
        # --- insights
        if method == "GET" and (path.endswith("/insights") or path.endswith("/video_insights")):
            rid = path.rsplit("/", 2)[-2]
            return Resp({"data": self.insights(rid, path.endswith("/video_insights"))})
        # --- status / permalink reads
        if method == "GET":
            rid = path.rsplit("/", 1)[-1]
            if rid in self.containers:                       # IG container or FB video
                c = self.containers[rid]
                if rid.startswith("v"):
                    pub = c.get("published")
                    return Resp({"status": {"video_status": "ready" if pub else "processing",
                                            "publishing_phase": {"publish_status": "published" if pub else "not_started"}},
                                 "permalink_url": f"/reel/{rid}"})
                return Resp({"status_code": "PUBLISHED" if c.get("published") else "FINISHED"})
            if rid in self.media:
                return Resp({"permalink": f"https://instagram.invalid/reel/{rid}", "timestamp": graph_ts(self.media[rid]["at"])})
        return Resp({"error": {"message": f"fake Meta has no route for {method} {path}", "code": 100}}, 400)

    def containers_caption(self, media: dict[str, Any]) -> str:
        return media.get("caption", "")

    def insights(self, rid: str, facebook: bool) -> list[dict[str, Any]]:
        m = self.media.get(rid) or {}
        age_h = max(1.0, (clock.utcnow() - m.get("at", clock.utcnow())).total_seconds() / 3600)
        lift = 1.6 if m.get("variant") == "B" else 1.0           # B is genuinely better here
        base = (900 if not facebook else 300) * lift * (age_h ** 0.5) * self.rng.uniform(0.7, 1.3)
        views = int(base)
        if facebook:
            return [{"name": "fb_reels_total_plays", "values": [{"value": views}]},
                    {"name": "post_video_likes_by_reaction_type", "values": [{"value": {"LIKE": views // 20}}]},
                    {"name": "post_video_social_actions", "values": [{"value": {"COMMENT": views // 200, "SHARE": views // 300}}]}]
        return [{"name": "views", "values": [{"value": views}]}, {"name": "likes", "values": [{"value": views // 15}]},
                {"name": "comments", "values": [{"value": views // 150}]}, {"name": "shares", "values": [{"value": views // 250}]}]


# ---------------------------------------------------------------------------------------------
# Fake Telegram: a reviewer who taps ~45 minutes after each card
# ---------------------------------------------------------------------------------------------

class FakeTelegram:
    def __init__(self, decide_for: dict[str, str], delay: timedelta = timedelta(minutes=45)):
        self.decide_for, self.delay = decide_for, delay
        self.mid, self.update_id = 100, 0
        self.open: dict[int, tuple[str, datetime]] = {}       # message id -> (item id, sent at)
        self.cards: list[str] = []
        self.notices: list[str] = []
        self.taps: Counter = Counter()

    def __call__(self, method: str, params: dict[str, Any], files: Any = None, timeout: float | None = None) -> Any:
        if method in ("sendVideo", "sendMessage"):
            self.mid += 1
            markup = params.get("reply_markup") or {}
            buttons = [b for row in (markup.get("inline_keyboard") or []) for b in row]
            if buttons:
                item_id = buttons[0]["callback_data"].split(":", 2)[2]
                self.open[self.mid] = (item_id, clock.utcnow())
                self.cards.append(item_id)
            elif method == "sendMessage" and "automatic" in str(params.get("text", "")):
                self.notices.append(str(params.get("text", ""))[:80])
            return {"message_id": self.mid}
        if method == "getUpdates":
            ups = []
            for mid, (item_id, sent) in list(self.open.items()):
                if clock.utcnow() - sent >= self.delay:
                    action = self.decide_for.get(item_id, "ok")
                    self.update_id += 1
                    ups.append({"update_id": self.update_id, "callback_query": {
                        "id": f"cq{self.update_id}", "from": {"id": int(CHAT)},
                        "message": {"message_id": mid, "chat": {"id": int(CHAT), "type": "private"}},
                        "data": f"a:{action}:{item_id}"}})
                    self.taps[action] += 1
                    del self.open[mid]
            return ups
        return True                                           # edits, answerCallbackQuery, deletes


# ---------------------------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------------------------

def sim_config(days: int) -> dict[str, Any]:
    cfg = copy.deepcopy(config.get_config())
    cfg["app"]["dry_run"] = False                             # in memory only; config.yaml untouched
    p = cfg.setdefault("posting", {})
    p.update(meta_min_gap_seconds=0, container_poll_seconds=0.0, container_poll_max_minutes=1.0,
             graph_base="https://graph.invalid", rupload_base="https://rupload.invalid",
             retry_backoff_s=0.0, retry_backoff_cap_s=0.0)
    p["platforms"] = {"instagram": True, "facebook": True}
    p["profile"] = p.get("profile", "local")
    cfg.setdefault("telegram", {})["enabled"] = True
    cfg["telegram"]["edit_throttle_gap_s"] = 0.0
    return cfg


def seed_item(db_path: Path, media: Path, n: int, rng: random.Random, *, preview_only: bool = False,
              stage: str = "rendered") -> str:
    item_id = f"sim-{n:02d}"
    db.insert_discovered_item({
        "id": item_id, "source": "youtube", "source_id": f"vid{n}", "source_url": f"https://youtube.invalid/{n}",
        "author_name": f"Creator{n % 4}", "discovery_score": round(rng.uniform(0.2, 0.9), 3),
        "raw_velocity": 5.0, "percentile_rank": 0.7, "upload_time": clock.now_iso(),
    }, db_path=db_path)
    files = {}
    for key, sub, name in (("render_a_path", "renders", f"{item_id}_A.mp4"), ("render_b_path", "renders", f"{item_id}_B.mp4"),
                           ("cut_path", "cuts", f"{item_id}_cut.mp4"), ("voiceover_audio_path", "audio", f"{item_id}_vo.wav"),
                           ("download_path", "downloads", f"{item_id}.mp4")):
        f = media / sub / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(name.encode() * 64)
        files[key] = str(f)
    # A preview_only item lives at `cut` (the triggers refuse to flag a rendered one), like the
    # laptop's style-preview items; it carries renders on disk all the same.
    db.transition_stage(item_id, "cut" if preview_only else "rendered", extra_fields={
        **files, "hook_text": f"WHAT IS HIDDEN IN CLIP {n}?", "hook_style": rng.choice(["question", "curiosity", "hype_analysis"]),
        "hook_grounded": 1, "authenticity": "unverified", "style_preset": "P0",
        "caption_text": posting.build_caption(f"WHAT IS HIDDEN IN CLIP {n}?", f"Creator{n % 4}", "youtube"),
    }, db_path=db_path)
    if preview_only:
        db.set_preview_only(item_id, True, db_path=db_path)
    if stage == "rejected":
        db.mark_rejected(item_id, "user_rejected", db_path=db_path)
    return item_id


def main(argv: list[str] | None = None) -> int:
    """Run the simulation; every global it patches is restored on the way out."""
    saved = (requests.Session.request, config.secret, db.DEFAULT_DB_PATH, telegram_bot.send_alert,
             runner.healthcheck_transport, config._MEDIA_ROOT, os.environ.get("HF_HOME"))
    try:
        return _simulate(argv)
    finally:
        (requests.Session.request, config.secret, db.DEFAULT_DB_PATH, telegram_bot.send_alert,
         runner.healthcheck_transport) = saved[:5]
        config.set_media_root(saved[5])
        if saved[6] is not None:
            os.environ["HF_HOME"] = saved[6]
        clock.set_clock(None)
        logging.disable(logging.NOTSET)


def _simulate(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CP4 end-to-end dry run on a fake clock (no network, no real data).")
    ap.add_argument("--days", type=float, default=10.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep", action="store_true", help="keep the temporary folder for inspection")
    ap.add_argument("--skip-real-check", action="store_true",
                    help="do not compare the real pipeline.db / media/ before and after (the test suite has its own guard)")
    args = ap.parse_args(argv)

    real_db = ROOT / "pipeline.db"
    real_db_sig = (real_db.stat().st_size, real_db.stat().st_mtime_ns) if real_db.exists() else None
    real_media = ROOT / "media"
    real_media_sig = sorted((str(p.relative_to(real_media)), p.stat().st_size, p.stat().st_mtime_ns)
                            for p in real_media.rglob("*") if p.is_file()) if real_media.exists() else []

    # ---- isolation: fake secrets, no real HTTP, temp DB + media, fake clock
    def refuse(self, method, url, *a, **k):
        raise RuntimeError(f"cp4_dry_run: real HTTP refused ({method} {url.split('?')[0]})")

    requests.Session.request = refuse                      # type: ignore[assignment]
    config.secret = lambda key, default="": FAKE_SECRETS.get(key, default)   # type: ignore[assignment]
    os.environ.pop("HF_HOME", None)
    tmp = Path(tempfile.mkdtemp(prefix="gta6_cp4_"))
    media = tmp / "media"
    config.set_media_root(media)
    db_path = tmp / "cp4_dry_run.db"
    db.DEFAULT_DB_PATH = db_path                           # belt and braces: no stray default path
    fake = clock.FakeClock(START)
    clock.set_clock(fake)
    db.init_db(db_path)

    rng = random.Random(args.seed)
    cfg = sim_config(int(args.days))
    min_gap_h = float(cfg["posting"].get("min_gap_hours", 3.0))
    daily_target = int(cfg["posting"].get("daily_post_target", 2))
    switch_days = cfg.get("approval", {}).get("switch_to_auto_after_days")

    # ---- the cast: real items arrive over time; one preview_only and one rejected item never post
    n_items = 0
    decide_for: dict[str, str] = {}

    def ingest(count: int) -> list[str]:
        nonlocal n_items
        out = []
        for _ in range(count):
            n_items += 1
            iid = seed_item(db_path, media, n_items, rng)
            decide_for[iid] = "no" if n_items % 7 == 3 else ("aonly" if n_items % 6 == 5 else "ok")
            out.append(iid)
        return out

    ingest(4)
    preview_only_id = seed_item(db_path, media, 90, rng, preview_only=True)
    rejected_id = seed_item(db_path, media, 91, rng, stage="rejected")
    crash_item = "sim-02"                                  # Instagram publishes, then the runner dies
    lost_item = "sim-04"                                   # Instagram publishes, the answer is lost

    meta = FakeMeta(db_path, rng, crash_item, lost_item)
    tg = FakeTelegram(decide_for)
    client = posting.MetaClient(transport=meta, cfg=cfg, db_path=db_path, clock=fake.time, sleep=fake.sleep)
    alerts: list[str] = []
    publisher = posting.Publisher(client=client, cfg=cfg, db_path=db_path, rng=rng,
                                  alert=lambda text, key=None: alerts.append(text), clock=fake.time, sleep=fake.sleep)
    bot = telegram_bot.ApprovalBot(transport=tg, chat_id=CHAT, cfg=cfg, db_path=db_path, clock=fake.time)
    telegram_bot.send_alert = lambda text, key=None, **kw: alerts.append(text) or True   # type: ignore[assignment]
    pings: list[str] = []
    runner.healthcheck_transport = lambda url, timeout: pings.append(url) or 200
    probe = lambda path: 23.0 if str(path).endswith("_B.mp4") else 20.0   # noqa: E731 - gate durations

    cleanup_breaks: list[str] = []

    def stage_cleanup(c: dict[str, Any], p: Any) -> Any:
        needed = []
        for it in db.get_items_by_stage("queued", db_path=p) + db.get_items_by_stage("posted", db_path=p) + \
                db.get_items_by_stage("measured", db_path=p):
            if posting.item_has_open_posts(it["id"], c, db_path=p):
                needed += [Path(it[k]) for k in ("render_a_path", "render_b_path") if it.get(k) and Path(it[k]).exists()]
        out = cleanup.run_cleanup_stage(c, p, now=clock.utcnow())
        cleanup_breaks.extend(f"cleanup deleted {f.name}, which an open post still needs" for f in needed if not f.exists())
        return out

    stages = [
        ("settle", lambda c, p: posting.settle_publishing_rows(cfg=c, db_path=p, publisher=publisher)),
        ("approve", lambda c, p: telegram_bot.run_approval_stage(cfg=c, db_path=p, bot=bot, poll_timeout=0, probe=probe)),
        ("schedule", lambda c, p: scheduler.run_scheduler_stage(cfg=c, db_path=p, now=clock.utcnow(), rng=rng)),
        ("post", lambda c, p: posting.run_posting_stage(cfg=c, db_path=p, publisher=publisher, now=clock.utcnow(), rng=rng)),
        ("analytics", lambda c, p: analytics.run_analytics_stage(cfg=c, db_path=p, client=client, now=clock.utcnow(),
                                                                 alert=lambda t, k=None: alerts.append(t))),
        ("cleanup", stage_cleanup),
    ]

    # The pipeline's own logs would bury the report (the crash scenario logs a traceback on
    # purpose); stage failures are counted from each cycle's result instead.
    logging.disable(logging.CRITICAL)

    # ---- the clock: 30-minute ticks; 2 new items a day; the laptop sleeps ~34 h on day 4-5
    start = clock.parse(START)
    end = start + timedelta(days=args.days)
    sleep_from, sleep_to = start + timedelta(days=4, hours=6), start + timedelta(days=5, hours=16)
    ticks = cycles = failed_cycles = 0
    learning_by_day: list[str] = []
    stage_errors: Counter = Counter()
    t = start
    while t < end:
        fake.set(t)
        if t.hour == 1 and t.minute == 0 and t > start:
            ingest(2)
        asleep = sleep_from <= t < sleep_to
        if not asleep:
            result = runner.run_stages(cfg, db_path, stages=stages)
            healthcheck.ping(result, cfg=cfg, transport=runner.healthcheck_transport)
            cycles += 1
            if result["status"] != "success":
                failed_cycles += 1
                stage_errors.update(result["errors"].keys())
        if t.hour == 23 and t.minute == 30:
            probs, why = scheduler.variant_probabilities(cfg, db_path)
            stats = scheduler.variant_stats(db_path)
            learning_by_day.append(
                f"  {t.date()}  A {probs['A']:.0%} / B {probs['B']:.0%}   "
                f"(A n={stats.get('A', {}).get('n', 0)} score={stats.get('A', {}).get('score', 0):.2f}, "
                f"B n={stats.get('B', {}).get('n', 0)} score={stats.get('B', {}).get('score', 0):.2f})  {why.split(':')[0]}")
        ticks += 1
        t += timedelta(minutes=30)

    logging.disable(logging.NOTSET)

    # ---- read everything back
    with db.db(db_path) as conn:
        posts = [dict(r) for r in conn.execute("SELECT * FROM posts ORDER BY id")]
        items = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM items")}
        metrics = [dict(r) for r in conn.execute("SELECT post_id, snapshot_hours FROM metrics")]
        moves = [dict(r) for r in conn.execute(
            "SELECT item_id, message, created_at FROM stage_log WHERE stage = 'scheduler' AND message LIKE 'missed slot%' ORDER BY id")]
        draws = [dict(r) for r in conn.execute(
            "SELECT item_id, message, created_at FROM stage_log WHERE stage = 'scheduler' AND message LIKE 'variant % drawn at the slot%' ORDER BY id")]
    first_live = db.first_live_at(db_path)
    auto_since = db.kv_get(telegram_bot.K_AUTO_SINCE, db_path=db_path)
    tz = scheduler.active_profile(cfg)[1]

    breaks: list[str] = list(meta.violations) + cleanup_breaks
    per = Counter((p["item_id"], p["platform"]) for p in meta.publishes)
    breaks += [f"double post: {i} on {pl} published {n} times" for (i, pl), n in per.items() if n > 1]
    breaks += [f"double post attempt: {i} on {pl} publish called {n} times"
               for (i, pl), n in meta.attempts.items() if n > 1]
    firsts: dict[str, datetime] = {}
    for p in meta.publishes:
        firsts.setdefault(p["item_id"], p["at"])
    order = sorted(firsts.items(), key=lambda kv: kv[1])
    for (a, ta), (b, tb) in zip(order, order[1:]):
        if (tb - ta) < timedelta(hours=min_gap_h) - timedelta(seconds=1):
            breaks.append(f"burst: {a} at {clock.iso(ta)} and {b} at {clock.iso(tb)} are {(tb - ta)} apart")
    per_day = Counter(ta.astimezone(tz).date() for _, ta in order)
    breaks += [f"burst: {d} has {n} posts (> {daily_target})" for d, n in per_day.items() if n > daily_target]
    for iid in (preview_only_id, rejected_id):
        if any(p["item_id"] == iid for p in meta.publishes):
            breaks.append(f"{iid} ({items[iid]['stage']}, preview_only={items[iid]['preview_only']}) was posted")
    for p in posts:
        if p["status"] == "live" and p["variant"] not in ("A", "B"):
            breaks.append(f"post {p['id']} is live with variant {p['variant']!r}")
    rejected_by_user = [i for i, it in items.items() if it.get("rejection_reason") == "user_rejected" and i != rejected_id]
    breaks += [f"user-rejected {i} was posted" for i in rejected_by_user if i in firsts]
    now_media = sorted((str(p.relative_to(real_media)), p.stat().st_size, p.stat().st_mtime_ns)
                       for p in real_media.rglob("*") if p.is_file()) if real_media.exists() else []
    if not args.skip_real_check:
        if real_db.exists() and (real_db.stat().st_size, real_db.stat().st_mtime_ns) != real_db_sig:
            breaks.append("the real pipeline.db changed")
        if now_media != real_media_sig:
            breaks.append("the real media/ folder changed")

    # ---- report
    week1_end = clock.parse(first_live) + timedelta(days=float(cfg["autopilot"]["week_one_days"])) if first_live else None
    alloc = defaultdict(Counter)
    draw_lines: list[str] = []
    for d in draws:
        when = clock.parse(d["created_at"])
        v = d["message"].split()[1]
        why = d["message"].split("(", 1)[-1].rstrip(")")
        rule = "bandit 80/20" if why.startswith("bandit") else ("week 1 50/50" if "week 1" in why else "50/50 (too few samples)")
        phase = "week 1" if (week1_end is None or when < week1_end) else "after week 1"
        alloc[phase][f"{v} by {rule}"] += 1
        if phase == "after week 1":
            draw_lines.append(f"    {when.astimezone(scheduler.active_profile(cfg)[1]):%a %d %b %H:%M}  {d['item_id']}  {v}  ({why[:70]})")
    a_only = [i for i, it in items.items() if it.get("approval_type") == "approved_a_only"]
    stages_seen = Counter(it["stage"] for it in items.values())
    snaps = Counter(m["snapshot_hours"] for m in metrics)
    cleaned = [i for i, it in items.items() if it["stage"] == "cleaned"]
    renders_gone = [i for i, it in items.items() if not it.get("render_a_path") and it["stage"] in ("measured", "cleaned")]
    crash_rows = [p for p in posts if p["item_id"] == crash_item]
    lost_rows = [p for p in posts if p["item_id"] == lost_item]

    lines = [
        "CP4 dry run - simulated, no network, temporary database",
        "=" * 56,
        f"window      : {START} -> {clock.iso(end)} ({args.days:g} days, {ticks} ticks of 30 min, seed {args.seed})",
        f"cycles      : {cycles} run, {failed_cycles} with a failed stage {dict(stage_errors) or ''}; "
        f"laptop asleep {clock.iso(sleep_from)} -> {clock.iso(sleep_to)}",
        f"healthcheck : {len(pings)} pings (one per fully successful cycle; none for the {failed_cycles} failed)",
        f"items       : {n_items} arrived + 1 preview_only + 1 rejected; final stages {dict(stages_seen)}",
        f"approval    : {len(tg.cards)} Telegram cards, taps {dict(tg.taps)}; A-only items {len(a_only)}",
        f"auto switch : first live {first_live}; auto approval since {auto_since or 'never'} "
        f"(after {switch_days} days); switch notices sent: {len(tg.notices)}",
        f"posts       : {len(firsts)} items published, {len(meta.publishes)} platform publishes, "
        f"max {max(per_day.values()) if per_day else 0}/local day",
        f"missed slots: {len(moves)} re-slotted after the sleep (earliest first):",
    ]
    lines += [f"  {m['item_id']}: {m['message']}" for m in moves[:8]]
    lines += [
        f"crash       : {crash_item} - the runner died right after Instagram published; rows now "
        f"{[(r['platform'], r['status']) for r in crash_rows]}; Instagram publishes for it: "
        f"{per.get((crash_item, 'instagram'), 0)}",
        f"lost reply  : {lost_item} - Instagram published but the reply was lost; rows now "
        f"{[(r['platform'], r['status']) for r in lost_rows]}; publish calls for it: "
        f"{meta.attempts.get((lost_item, 'instagram'), 0)}",
        "variant     : drawn when each slot came due; week 1 ends "
        f"{clock.iso(week1_end) if week1_end else '-'}",
        f"  draws in week 1     : {dict(alloc['week 1'])}",
        f"  draws after week 1  : {dict(alloc['after week 1'])}",
        *draw_lines,
        "bandit by day (allocation the scheduler would use; learning rebuilt from metrics each cycle):",
        *learning_by_day,
        f"metrics     : {snaps.get(24, 0)} x 24h and {snaps.get(72, 0)} x 72h snapshots",
        f"cleanup     : {len(renders_gone)} items' renders deleted after retention; {len(cleaned)} items 'cleaned'",
        f"alerts      : {len(alerts)} (first: {alerts[0][:90] if alerts else '-'})",
        "",
        "first publishes (local time, variant):",
    ]
    lines += [f"  {ta.astimezone(tz):%a %d %b %H:%M}  {iid}  {next(p['variant'] for p in meta.publishes if p['item_id'] == iid)}"
              for iid, ta in order]
    lines += ["", "invariants:"]
    checks = [
        ("no double post (<= 1 publish call per item and platform)", not any("double post" in b for b in breaks)),
        (f"no burst (>= {min_gap_h:g} h apart, <= {daily_target}/local day)", not any(b.startswith("burst") for b in breaks)),
        ("nothing preview_only / rejected / unapproved posted",
         not any("was posted" in b or "while it was" in b for b in breaks)),
        ("cleanup kept every file an open post needs", not cleanup_breaks),
        ("uploaded variant == row variant, never undecided", not any("variant" in b for b in breaks)),
        ("real pipeline.db and media/ untouched", not any("real " in b for b in breaks)),
    ]
    if args.skip_real_check:
        checks[-1] = (checks[-1][0] + " (skipped: --skip-real-check)", None)
    lines += [f"  [{'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}] {name}" for name, ok in checks]
    if breaks:
        lines += ["", "BREAKS:"] + [f"  - {b}" for b in breaks]
    lines += ["", "RESULT: " + ("PASS" if not breaks else f"FAIL ({len(breaks)} break(s))")]
    print("\n".join(lines))

    if args.keep:
        print(f"\nkept {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not breaks else 1


if __name__ == "__main__":
    raise SystemExit(main())
