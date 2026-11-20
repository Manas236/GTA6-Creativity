"""The dashboard -> runner -> posting path: quick Post now, go-live cleanup, worker survival flags,
the calendar, and the live posting-progress panel. Zero network: Meta is a fake transport."""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from dashboard import commands, queue_controls, views, workers
from dashboard.app import create_app
from pipeline import clock, config, db, post_progress, posting, runner, scheduler
from test_posting import FB_PAGE, IG_USER, FakeResponse, FakeTransport, make_item, make_publisher

BASE = "http://localhost"
TOKEN = "ig-token-secret"


@pytest.fixture
def secrets(monkeypatch):
    values = {"IG_USER_ID": IG_USER, "IG_ACCESS_TOKEN": TOKEN, "FB_PAGE_ID": FB_PAGE,
              "FB_PAGE_ACCESS_TOKEN": "fb-page-token-secret", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
    monkeypatch.setattr(config, "secret", lambda key, default="": values.get(key, default))


@pytest.fixture
def cfg():
    base = copy.deepcopy(config.get_config(reload=True))
    base["app"]["dry_run"] = False
    base["posting"].update({
        "platforms": {"instagram": True, "facebook": False}, "dry_run_mode": None,
        "meta_min_gap_seconds": 0, "graph_base": "https://graph.test", "rupload_base": "https://rupload.test",
        "api_version": "v25.0", "max_retries": 1, "retry_backoff_s": 0.0, "max_post_attempts": 3,
        "container_poll_seconds": 0.0, "container_poll_max_minutes": 1.0, "retry_delay_minutes": 10,
    })
    return base


def reading_upload(method, url, kwargs):
    """rupload that really reads the body in chunks, so the upload reports its progress."""
    body = kwargs["data"]
    while body.read(1024):
        pass
    return FakeResponse({"success": True})


def meta_routes(upload=reading_upload):
    return {
        f"/{IG_USER}/media_publish": FakeResponse({"id": "media-999"}),
        f"/{IG_USER}/media": FakeResponse({"id": "container-abc"}),
        "/ig-api-upload/": upload,
        "/container-abc": [FakeResponse({"status_code": "IN_PROGRESS", "status": "Processing"}),
                           FakeResponse({"status_code": "FINISHED"})],
        "/media-999": FakeResponse({"permalink": "https://www.instagram.com/reel/xyz/",
                                    "timestamp": "2026-11-19T10:00:00+0000"}),
    }


def hold_loop_lock(path, stage="cut"):
    """Pretend the runner loop holds the lock (this pid, fresh heartbeat), busy in `stage`."""
    db.kv_set(runner.LOCK_KEY, json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                           "heartbeat": time.time(), "started_at": time.time(),
                                           "token": "loop", "kind": "loop"}), path)
    db.kv_set("runner:stage", stage, path)


def dashboard(tmp_db, cfg, tmp_path):
    app = create_app(tmp_db, cfg, tmp_path / "media")
    client = app.test_client()
    csrf = client.get("/api/state", base_url=BASE).get_json()["csrf"]
    return client, csrf


def post_json(client, csrf, url, body):
    return client.post(url, base_url=BASE, headers={"X-CSRF-Token": csrf}, json=body)


def confirm_post_now(client, csrf, item_id):
    plan = post_json(client, csrf, "/api/prepare", {"action": "post_now", "item_id": item_id})
    assert plan.status_code == 200, plan.get_json()
    return post_json(client, csrf, "/api/confirm", {"ticket": plan.get_json()["ticket"]}).get_json()["command"]


def panel(client, item_id):
    response = client.get(f"/api/items/{item_id}/posting", base_url=BASE)
    assert response.status_code == 200
    return response.get_json()


# ---------------------------------------------------------------------------
# Event recording
# ---------------------------------------------------------------------------

def test_events_are_recorded_deduped_and_grouped_into_runs(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path)
    assert post_progress.run_is_open("item-1", tmp_db) is False
    post_progress.open_run("item-1", "post_now", "Post now confirmed", command_id="cmd1", db_path=tmp_db)
    assert post_progress.run_is_open("item-1", tmp_db)
    assert post_progress.ensure_run("item-1", "scheduled", "Slot reached", db_path=tmp_db) is False
    for _ in range(3):   # a held post is checked every few seconds: one row, not three
        post_progress.record("item-1", "checks", "waiting", "Minimum gap", db_path=tmp_db, dedupe=True)
    post_progress.record("item-1", "container", "running", "Creating", post_id=7, db_path=tmp_db)
    post_progress.record("item-1", "container", "done", "Container c1", post_id=7, db_path=tmp_db)
    post_progress.finish("item-1", "live", "Live", post_id=7, db_path=tmp_db, permalink="https://x/reel/1")
    run = post_progress.latest_run("item-1", tmp_db)
    assert [(e["step"], e["state"]) for e in run] == [
        ("requested", "done"), ("checks", "waiting"), ("container", "running"), ("container", "done"),
        ("outcome", "live")]
    folded = post_progress.fold(run)
    stages = {s["key"]: s for s in folded["stages"]}
    assert folded["outcome"] == "live" and folded["command_id"] == "cmd1" and folded["post_id"] == 7
    assert stages["container"]["state"] == "done" and stages["container"]["started_at"]
    assert stages["checks"]["state"] == "done"            # finished once a later stage started
    assert stages["live"]["state"] == "done" and folded["permalink"] == "https://x/reel/1"
    assert post_progress.ensure_run("item-1", "retry", "Retrying", db_path=tmp_db) is True   # new run


def test_recording_never_raises_even_without_the_table(tmp_path):
    bare = tmp_path / "bare.db"
    import sqlite3
    sqlite3.connect(bare).close()
    assert post_progress.record("x", "checks", "done", db_path=bare) is None


def test_meta_error_text_has_code_subcode_and_message():
    error = post_progress.meta_error_from({"code": 100, "error_subcode": 2207026, "type": "OAuthException",
                                           "message": "Unsupported video", "fbtrace_id": "AbC"}, 400)
    assert post_progress.error_text(error) == (
        "Meta error code 100, subcode 2207026 (OAuthException), HTTP 400: Unsupported video [fbtrace_id AbC]")


# ---------------------------------------------------------------------------
# Defect 1 + 6: Post now while the runner is busy, with the full stage sequence
# ---------------------------------------------------------------------------

def test_post_now_while_runner_busy_publishes_on_the_quick_check_and_panel_shows_every_stage(
        tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    hold_loop_lock(tmp_db, "cut")
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    command = confirm_post_now(client, csrf, item["id"])
    assert command["status"] == "pending"                  # the runner holds the lock
    waiting = panel(client, item["id"])
    stages = {s["key"]: s for s in waiting["stages"]}
    assert stages["requested"]["state"] == "done"
    assert stages["waiting"]["state"] == "running" and "busy in cut" in stages["waiting"]["message"]
    assert waiting["active"] is True and waiting["dry_run"] is False

    transport = FakeTransport(meta_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    started = time.monotonic()
    runner.quick_post_tick(tmp_db, cfg, publisher=publisher)   # what the runner's thread does every 5 s
    assert time.monotonic() - started < 30

    row = posting.get_posts_for_item(item["id"], tmp_db)[0]
    assert row["status"] == "live" and row["remote_id"] == "media-999"
    done = panel(client, item["id"])
    states = {s["key"]: s["state"] for s in done["stages"]}
    assert states == dict.fromkeys(post_progress.STEP_KEYS, "done")
    assert all(s["started_at"] and s["finished_at"] for s in done["stages"])
    by_key = {s["key"]: s for s in done["stages"]}
    assert "busy in cut" in by_key["waiting"]["message"]
    assert "LIVE" in by_key["checks"]["message"] and "Meta not paused" in by_key["checks"]["message"]
    assert "container-abc" in by_key["container"]["message"]
    assert by_key["upload"]["detail"]["percent"] == 100
    assert by_key["processing"]["detail"]["polls"] == 2 and by_key["processing"]["detail"]["meta_status"] == "FINISHED"
    assert done["outcome"] == "live" and done["permalink"] == "https://www.instagram.com/reel/xyz/"
    assert done["active"] is False and done["error"] is None
    run = post_progress.latest_run(item["id"], tmp_db)
    upload_progress = [e for e in run if e["step"] == "upload" and e["state"] == "running"]
    assert len(upload_progress) >= 2                       # start, plus at least one byte count


def test_failure_at_upload_is_highlighted_with_meta_error_retry_and_copy_text(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    hold_loop_lock(tmp_db, "render")
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    confirm_post_now(client, csrf, item["id"])
    refused = FakeResponse({"error": {"message": f"Bad file (access_token={TOKEN})", "type": "OAuthException",
                                      "code": 100, "error_subcode": 2207026, "fbtrace_id": "Trace1"}}, 400)
    transport = FakeTransport(meta_routes(upload=refused))
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    runner.quick_post_tick(tmp_db, cfg, publisher=publisher)

    data = panel(client, item["id"])
    states = {s["key"]: s["state"] for s in data["stages"]}
    assert states["container"] == "done" and states["upload"] == "failed"
    assert states["processing"] == states["publishing"] == states["live"] == "waiting"
    assert data["outcome"] == "failed" and data["failed_step"] == "upload" and data["active"] is False
    assert data["error"]["code"] == 100 and data["error"]["subcode"] == 2207026
    assert "Meta error code 100, subcode 2207026" in data["error_text"]
    assert TOKEN not in json.dumps(data)                    # secrets masked everywhere
    assert data["retry"]["will_retry"] is True and "attempt 2 of 3" in data["retry"]["text"]
    text = data["copy_text"]
    assert f"Post id: {data['post_id']}" in text and "Failed at: Uploading video" in text
    assert "subcode 2207026" in text and "Retry: Yes." in text and TOKEN not in text
    # The next quick check does not burn the remaining attempts at once: it waits retry_delay.
    runner.quick_post_tick(tmp_db, cfg, publisher=publisher)
    assert posting.get_posts_for_item(item["id"], tmp_db)[0]["attempt_count"] == 1
    # ...and waiting out that delay never replaces the failure on the panel with an empty retry run.
    still = panel(client, item["id"])
    assert still["outcome"] == "failed" and still["failed_step"] == "upload" and len(still["runs"]) == 1


def test_dry_run_panel_has_the_banner_and_nothing_is_sent(tmp_db, tmp_path, cfg, secrets):
    cfg["app"]["dry_run"] = True
    cfg["posting"]["dry_run_mode"] = "plan_only"
    item = make_item(tmp_db, tmp_path)
    hold_loop_lock(tmp_db)
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    confirm_post_now(client, csrf, item["id"])
    transport = FakeTransport(meta_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    runner.quick_post_tick(tmp_db, cfg, publisher=publisher)
    data = panel(client, item["id"])
    assert transport.calls == []                           # plan_only: no request at all
    assert data["dry_run"] is True and data["outcome"] == "dry_run"
    states = {s["key"]: s["state"] for s in data["stages"]}
    assert states["container"] == states["upload"] == states["publishing"] == "skipped"
    assert "DRY RUN" in data["copy_text"]


def test_quick_poster_thread_publishes_within_seconds_while_the_cycle_is_busy(
        tmp_db, tmp_path, cfg, secrets, monkeypatch):
    transport = FakeTransport(meta_routes())
    monkeypatch.setattr(posting.RequestsTransport, "__call__", lambda self, method, url, **kw: transport(method, url, **kw))
    item = make_item(tmp_db, tmp_path)
    hold_loop_lock(tmp_db, "cut")
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    confirm_post_now(client, csrf, item["id"])
    poster = runner.QuickPoster(tmp_db, 0.05, cfg).start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and [r["status"] for r in posting.get_posts_for_item(item["id"], tmp_db)] != ["live"]:
            time.sleep(0.05)
    finally:
        poster.stop()
    assert posting.get_posts_for_item(item["id"], tmp_db)[0]["status"] == "live"


def test_post_now_with_no_runner_is_published_by_a_one_off_worker_under_the_lock(
        tmp_db, tmp_path, cfg, secrets, monkeypatch):
    transport = FakeTransport(meta_routes())
    monkeypatch.setattr(posting.RequestsTransport, "__call__", lambda self, method, url, **kw: transport(method, url, **kw))
    monkeypatch.setattr(config, "get_config", lambda reload=False: cfg)
    spawned = []
    monkeypatch.setattr(workers, "spawn_post_worker", lambda path, root, job, item_id: spawned.append(job) or 99)
    item = make_item(tmp_db, tmp_path)
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    command = confirm_post_now(client, csrf, item["id"])
    assert command["status"] == "applied" and spawned == [command["id"]]   # slot set, worker started
    assert runner.active_lock(tmp_db) is None
    assert runner.run_post_now_worker(command["id"], tmp_db, poll_s=0, sleep=lambda s: None) == 0
    assert posting.get_posts_for_item(item["id"], tmp_db)[0]["status"] == "live"
    assert runner.active_lock(tmp_db) is None              # released after the one-off publish
    states = {s["key"]: s["state"] for s in panel(client, item["id"])["stages"]}
    assert set(states.values()) == {"done"}


def test_post_now_worker_steps_aside_when_the_runner_starts(tmp_db, tmp_path, cfg, secrets, monkeypatch):
    monkeypatch.setattr(config, "get_config", lambda reload=False: cfg)
    monkeypatch.setattr(workers, "spawn_post_worker", lambda *args: 99)
    item = make_item(tmp_db, tmp_path)
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    command = confirm_post_now(client, csrf, item["id"])
    hold_loop_lock(tmp_db)
    assert runner.run_post_now_worker(command["id"], tmp_db, poll_s=0, sleep=lambda s: None) == 0
    assert posting.get_posts_for_item(item["id"], tmp_db)[0]["status"] == "pending"   # the runner posts it


def test_post_now_slot_is_never_moved_by_the_missed_slot_grace(tmp_db, tmp_path, cfg, secrets, fake_clock):
    late = clock.iso(fake_clock.now - timedelta(hours=3))
    post_now = make_item(tmp_db, tmp_path, "item-now")
    posting.ensure_post_rows(post_now, late, cfg, tmp_db)
    queue_controls.set_slot("item-now", late, tmp_db, post_now=True)
    db.transition_stage("item-now", "queued", db_path=tmp_db)
    regular = make_item(tmp_db, tmp_path, "item-grid")
    posting.ensure_post_rows(regular, late, cfg, tmp_db)
    db.transition_stage("item-grid", "queued", db_path=tmp_db)
    moved = scheduler.reschedule_missed(fake_clock.now, cfg, tmp_db)
    assert [m[0] for m in moved] == ["item-grid"]          # the regular one moves, Post now never does
    assert posting.item_slot(posting.get_posts_for_item("item-now", tmp_db)) == late
    transport = FakeTransport(meta_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    posting.run_posting_stage(cfg, tmp_db, publisher=publisher, now=fake_clock.now)
    assert posting.get_posts_for_item("item-now", tmp_db)[0]["status"] == "live"


def test_holds_are_logged_and_shown_gap_and_cap(tmp_db, tmp_path, cfg, secrets, fake_clock):
    live = make_item(tmp_db, tmp_path, "item-live")
    posting.ensure_post_rows(live, clock.iso(fake_clock.now), cfg, tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status='live', posted_at=?, remote_id='m1' WHERE item_id='item-live'",
                     (clock.iso(fake_clock.now - timedelta(hours=1)),))
    due = make_item(tmp_db, tmp_path, "item-due")
    posting.ensure_post_rows(due, clock.iso(fake_clock.now), cfg, tmp_db)
    db.transition_stage("item-due", "queued", db_path=tmp_db)
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(meta_routes()))
    assert posting.run_posting_stage(cfg, tmp_db, publisher=publisher, now=fake_clock.now) == {}
    last = post_progress.latest_run("item-due", tmp_db)[-1]
    assert last["step"] == "checks" and last["state"] == "waiting" and "Minimum gap" in last["message"]


# ---------------------------------------------------------------------------
# Defect 2: go live cleans up dry-run state; go dry damages nothing
# ---------------------------------------------------------------------------

def go(client, csrf, action):
    payload = {"dry_run_mode": None} if action == "go_live" else {}
    plan = post_json(client, csrf, "/api/prepare", {"action": action, "payload": payload})
    assert plan.status_code == 200, plan.get_json()
    return post_json(client, csrf, "/api/confirm", {"ticket": plan.get_json()["ticket"]}).get_json()["command"]


def test_go_live_resets_dry_run_rows_and_reslots_past_slots_in_the_same_flow(
        tmp_db, tmp_path, cfg, fake_clock, monkeypatch):
    import connection_checks
    cfg["app"]["dry_run"] = True
    env = {"IG_ACCESS_TOKEN": "t", "IG_USER_ID": "u"}
    monkeypatch.setattr(connection_checks, "current_environment", lambda: env.copy())
    monkeypatch.setattr(connection_checks, "run_checks", lambda *a, **k: {
        s: {"status": "ok", "checked_at": clock.iso(fake_clock.now), "reason": "fixture"}
        for s in ("instagram_token", "instagram_user")})
    item = make_item(tmp_db, tmp_path)
    past = clock.iso(fake_clock.now - timedelta(hours=5))
    posting.ensure_post_rows(item, past, cfg, tmp_db)
    db.transition_stage(item["id"], "queued", db_path=tmp_db)
    with db.db(tmp_db) as conn:   # exactly what a plan_only dry run leaves behind
        conn.execute("UPDATE posts SET status='dry_run', remote_container_id='<dry-run-container:1>', "
                     "attempt_count=1, publish_started_at=?, error_message='dry run (plan_only)'", (past,))
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    assert post_json(client, csrf, "/api/connections/test", {"service": "all"}).status_code == 200
    command = go(client, csrf, "go_live")
    assert command["status"] == "applied" and "Reset 1 dry-run post row" in command["result"]
    row = posting.get_posts_for_item(item["id"], tmp_db)[0]
    assert row["status"] == "pending" and row["remote_container_id"] is None
    assert row["attempt_count"] == 0 and row["publish_started_at"] is None
    assert clock.parse(row["slot_datetime"]) > fake_clock.now     # moved off the past slot, right away
    assert "post_now" in commands.available_actions(db.get_item(item["id"], tmp_db), [row])


def test_go_dry_leaves_live_rows_and_a_real_mid_publish_container_alone(tmp_db, tmp_path, cfg, secrets):
    live, mid = make_item(tmp_db, tmp_path, "item-live"), make_item(tmp_db, tmp_path, "item-mid")
    for item in (live, mid):
        posting.ensure_post_rows(item, db.now(), cfg, tmp_db)
        db.transition_stage(item["id"], "queued", db_path=tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status='live', remote_id='m1', remote_url='https://x/1', variant='B', "
                     "variant_decided_at=created_at WHERE item_id='item-live'")
        conn.execute("UPDATE posts SET status='publishing', remote_container_id='container-abc', "
                     "publish_started_at=created_at, variant='B', variant_decided_at=created_at WHERE item_id='item-mid'")
    client, csrf = dashboard(tmp_db, cfg, tmp_path)
    assert go(client, csrf, "go_dry")["status"] == "applied"
    before = posting.get_posts_for_item("item-live", tmp_db)
    dry = {**cfg, "app": {"dry_run": True}, "posting": {**cfg["posting"], "dry_run_mode": "plan_only"}}
    routes = {"/container-abc": FakeResponse({"status_code": "PUBLISHED"}),
              f"/{IG_USER}/media": FakeResponse({"data": [{"id": "m2", "caption": before[0]["caption_text"],
                                                          "permalink": "https://x/2"}]})}
    publisher, _ = make_publisher(tmp_db, dry, FakeTransport(routes))
    posting.settle_publishing_rows(dry, tmp_db, publisher)
    assert posting.get_posts_for_item("item-live", tmp_db) == before
    # A real container is checked (reads only), never written off as dry_run (that would re-post it).
    assert posting.get_posts_for_item("item-mid", tmp_db)[0]["status"] == "live"


# ---------------------------------------------------------------------------
# Defect 5: rows that could never publish
# ---------------------------------------------------------------------------

def test_moving_a_dry_run_row_clears_its_fake_container(tmp_db, tmp_path, cfg):
    item = make_item(tmp_db, tmp_path)
    posting.ensure_post_rows(item, db.now(), cfg, tmp_db)
    db.transition_stage(item["id"], "queued", db_path=tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status='dry_run', remote_container_id='<dry-run-container:1>', "
                     "attempt_count=1, publish_started_at=created_at")
    rows = posting.get_posts_for_item(item["id"], tmp_db)
    assert commands.unstarted(rows)                        # a dry run does not lock the clip
    queue_controls.set_slot(item["id"], db.now(), tmp_db)
    row = posting.get_posts_for_item(item["id"], tmp_db)[0]
    assert (row["status"], row["remote_container_id"], row["attempt_count"], row["publish_started_at"]) == \
        ("pending", None, 0, None)


def test_a_retried_failed_row_gets_a_fresh_container_never_the_dead_one(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    posting.ensure_post_rows(item, db.now(), cfg, tmp_db)
    db.transition_stage(item["id"], "queued", db_path=tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status='failed', remote_container_id='container-dead', attempt_count=1, "
                     "error_message='container is ERROR, not FINISHED'")
    transport = FakeTransport(meta_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    row = posting.get_posts_for_item(item["id"], tmp_db)[0]
    assert publisher.publish(row["id"]) == "live"
    created = [c for c in transport.calls if c[0] == "POST" and c[1].endswith(f"/{IG_USER}/media")]
    assert len(created) == 1                               # a new container was created
    assert not any("container-dead" in url for url in transport.urls())


def test_older_pending_controls_apply_first_when_the_dashboard_takes_the_lock(tmp_db, tmp_path, cfg):
    from dashboard.store import initialize
    initialize(tmp_db)
    item = make_item(tmp_db, tmp_path)
    hold_loop_lock(tmp_db)                                 # e.g. a Make worker held the lock
    plan = commands.prepare("unapprove", item["id"], {}, tmp_db, cfg)
    assert commands.submit(plan, "older", tmp_db, cfg)["status"] == "pending"
    db.kv_set(runner.LOCK_KEY, "", tmp_db)                 # it ended without applying it
    plan = commands.prepare("pause", None, {}, tmp_db, cfg)
    commands.submit(plan, "newer", tmp_db, cfg)
    with db.db(tmp_db) as conn:
        rows = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM dashboard_commands")}
    assert rows == {"older": "applied", "newer": "applied"}
    assert db.get_item(item["id"], tmp_db)["stage"] == "awaiting_approval"


def test_force_stop_is_refused_while_the_quick_check_publishes(tmp_db, tmp_path, cfg):
    hold_loop_lock(tmp_db, "cut")
    db.kv_set(runner.STOP_KEY, json.dumps({"token": "loop", "at": time.time() - 200}), tmp_db)
    db.kv_set(runner.PUBLISHING_KEY, json.dumps({"pid": os.getpid(), "at": time.time()}), tmp_db)
    with pytest.raises(commands.ControlError, match="publishing a post"):
        workers.control("force", tmp_db, cfg, tmp_path, "cut")


# ---------------------------------------------------------------------------
# Defect 3: workers never share the dashboard's console
# ---------------------------------------------------------------------------

def test_spawn_has_no_console_window_and_breaks_away_from_jobs(tmp_db, tmp_path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        calls.append(kwargs)
        if os.name == "nt" and kwargs["creationflags"] & workers.CREATE_BREAKAWAY_FROM_JOB and len(calls) == 1:
            raise PermissionError(5, "Access is denied")   # a job that forbids breakaway
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    assert workers.spawn(tmp_db, tmp_path, "loop") == 4321
    assert calls[-1]["env"]["FOR_DISABLE_CONSOLE_CTRL_HANDLER"] == "1"
    if os.name == "nt":
        assert len(calls) == 2                             # tried breakaway, then fell back
        for kwargs in calls:
            flags = kwargs["creationflags"]
            assert flags & subprocess.CREATE_NO_WINDOW and flags & subprocess.CREATE_NEW_PROCESS_GROUP
            assert not flags & subprocess.DETACHED_PROCESS   # DETACHED gave the child a new, closable console
        assert calls[0]["creationflags"] & workers.CREATE_BREAKAWAY_FROM_JOB


# ---------------------------------------------------------------------------
# Defect 4: the calendar shows clips between grid times
# ---------------------------------------------------------------------------

def test_calendar_shows_a_post_now_slot_between_grid_times(tmp_db, tmp_path, cfg):
    item = make_item(tmp_db, tmp_path)
    exact = "2026-11-19T10:09:34Z"                         # 15:39 IST, between 13:00 and 18:00
    posting.ensure_post_rows(item, exact, cfg, tmp_db)
    queue_controls.set_slot(item["id"], exact, tmp_db, post_now=True)
    db.transition_stage(item["id"], "queued", db_path=tmp_db)
    from datetime import date
    cells = views.schedule(tmp_db, cfg, date(2026, 11, 19), 1)["slots"]
    assert [c["item_id"] for c in cells] == [None, None]
    holder = next(c for c in cells if c["extra"])
    assert holder["at"] == "2026-11-19T12:30:00Z"          # 18:00 IST is the nearer grid time
    assert holder["extra"] == [{"item_id": item["id"], "title": item["original_title"], "at": exact,
                                "post_now": True}]


def test_state_lists_items_with_posting_progress(tmp_db, tmp_path, cfg):
    item = make_item(tmp_db, tmp_path)
    post_progress.open_run(item["id"], "post_now", "Post now confirmed", db_path=tmp_db)
    client, _ = dashboard(tmp_db, cfg, tmp_path)
    assert item["id"] in client.get("/api/state", base_url=BASE).get_json()["progress_items"]
    assert client.get("/api/items/nope/posting", base_url=BASE).status_code == 404
