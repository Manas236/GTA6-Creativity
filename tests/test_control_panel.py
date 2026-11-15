"""Control panel integration tests. Every connection resolves to an isolated DB."""
import copy
import json
import os
import socket
import time
from datetime import timedelta
from pathlib import Path

import pytest

from dashboard import commands, execution, jobs, local_settings
from dashboard.app import create_app
from dashboard.store import initialize
from pipeline import clock, config, creation, db, discover, download, hooks, posting, runner, scheduler, telegram_bot
from pipeline import cut, render, sentence_windows
from test_sentence_windows import ES, SW, flat_scorer, region, talker

BASE = "http://127.0.0.1"


@pytest.fixture(autouse=True)
def isolated_db(tmp_db, monkeypatch):
    connect = db.get_connection

    def checked(path=None):
        assert path and Path(path).resolve() == tmp_db.resolve()
        return connect(path)

    monkeypatch.setattr(db, "get_connection", checked)
    initialize(tmp_db)


@pytest.fixture
def panel(tmp_db):
    cfg = copy.deepcopy(config.get_config(reload=True))
    app = create_app(tmp_db, cfg)
    app.config["TESTING"] = True
    client = app.test_client()
    token = client.get("/api/state", base_url=BASE).json["csrf"]

    def post(path, data):
        return client.post(path, base_url=BASE, json=data, headers={"X-CSRF-Token": token})

    return cfg, client, post


def record(tmp_db, id="item-example", stage="approved", **fields):
    db.insert_discovered_item({
        "id": id, "source": "youtube", "source_id": id, "source_url": "https://youtu.be/" + id,
        "author_name": "The creator", "original_title": id,
    }, tmp_db)
    db.transition_stage(id, stage, fields, tmp_db)
    return id


def prepare(post, action, id=None, **payload):
    response = post("/api/prepare", {"item_id": id, "action": action, "payload": payload})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.json


def execute(post, action, id=None, **payload):
    plan = prepare(post, action, id, **payload)
    result = post("/api/execute", {"ticket": plan["ticket"]})
    assert result.status_code in (200, 202), result.get_data(as_text=True)
    return result.json["command"]


def forbidden(*args, **kwargs):
    pytest.fail("A Flask request or creation-only worker called an excluded operation")


def test_flask_never_calls_external_services_and_releases_on_failed_command(tmp_db, panel, monkeypatch):
    _, _, post = panel
    for mod, name in [(posting.MetaClient, "request"), (telegram_bot.ApprovalBot, "send_message"),
                      (telegram_bot.ApprovalBot, "send_video"), (hooks, "_call_gemini")]:
        monkeypatch.setattr(mod, name, forbidden)
    item = record(tmp_db)
    assert execute(post, "edit_caption", item, body="Edited", hashtags="#gta")["status"] == "applied"
    monkeypatch.setattr(commands, "apply", lambda *args: {}["simulated error"])
    assert execute(post, "unapprove", item)["status"] == "failed"
    assert not runner.lock_state(tmp_db)


def test_make_worker_only_runs_creation_stages(tmp_db, panel, monkeypatch):
    cfg, _, post = panel
    spawned = []
    monkeypatch.setattr(execution, "spawn", lambda path, *args: spawned.append(args))
    command = execute(post, "clip_url", url="https://youtu.be/abcdefghijk", start="0:05", end="1:20")
    assert spawned == [("make", command["id"])]
    assert command["status"] == "pending"
    called = []

    def stage(name, next_stage):
        def fn(item, *args, **kwargs):
            called.append(name)
            item_id = item["id"] if isinstance(item, dict) else item
            db.transition_stage(item_id, next_stage, db_path=tmp_db)
            return True, "success"
        return fn

    for operation in ("_stage_post", "_stage_analytics", "_stage_cleanup", "_stage_schedule", "_stage_settle"):
        monkeypatch.setattr(runner, operation, forbidden)
    monkeypatch.setattr(runner, "STAGES", [(name, forbidden) for name, _ in runner.STAGES])
    monkeypatch.setattr(download, "download_single_item", stage("download", "downloaded"))
    monkeypatch.setattr(cut, "run_cut_item", stage("cut", "cut"))
    monkeypatch.setattr(render, "run_render_item", stage("render", "rendered"))
    runner.run_make(command["id"], cfg, tmp_db)
    assert called == ["download", "cut", "render"]
    item, = db.get_items_by_stage("awaiting_approval", db_path=tmp_db)
    assert item["manual_kind"] == "youtube" and item["hint_start"] == 5
    assert item["hint_end"] == 80 and item["dashboard_step"] == "approval hand-off"
    assert not runner.lock_state(tmp_db)
    duplicate = post("/api/prepare", {"action": "clip_url", "payload": {"url": "https://www.youtube.com/shorts/abcdefghijk"}})
    assert duplicate.status_code == 409 and "/items/" + item["id"] in duplicate.json["error"]


def test_alive_runner_consumes_creation_only_at_discover(tmp_db, panel, monkeypatch):
    cfg, _, post = panel
    monkeypatch.setattr(execution, "spawn", forbidden)
    assert runner.acquire_lock(tmp_db)
    try:
        command = execute(post, "make", count=2)
        seen = []
        monkeypatch.setattr(jobs, "run", lambda id, *_: seen.append(id))
        commands.runner_boundary(cfg, tmp_db, "post")
        assert seen == []
        commands.runner_boundary(cfg, tmp_db, "discover")
        assert seen == [command["id"]]
    finally:
        runner.release_lock(tmp_db)


def test_preview_only_and_started_rows_accept_no_mutations(tmp_db, panel):
    cfg, client, post = panel
    preview = record(tmp_db, "preview", "cut", preview_only=1)
    started = record(tmp_db, "started", "queued")
    rows = posting.ensure_post_rows(db.get_item(started, tmp_db), cfg=cfg, db_path=tmp_db)
    posting.set_status(rows[0]["id"], posting.STATUS_PUBLISHING, tmp_db, publish_started_at=db.now())
    for item in (preview, started):
        assert client.get("/api/items/" + item, base_url=BASE).json["actions"] == []
        for action in ("approve", "delete", "edit_caption", "unapprove", "post_now", "remake"):
            assert post("/api/prepare", {"action": action, "item_id": item}).status_code == 409
    with pytest.raises(posting.PublishError, match="preview_only"):
        posting.ensure_post_rows(db.get_item(preview, tmp_db), cfg=cfg, db_path=tmp_db)


def test_caption_override_updates_pending_and_future_post_rows_with_locked_credit(tmp_db, panel):
    cfg, _, post = panel
    item = record(tmp_db)
    original = db.get_item(item, tmp_db)
    posting.ensure_post_rows(original, cfg=cfg, db_path=tmp_db)
    assert execute(post, "edit_caption", item, body="New caption", hashtags="#gaming")["status"] == "applied"
    saved = db.get_item(item, tmp_db)
    caption = posting.caption_for_item(saved, cfg)
    assert caption == "New caption\n\nCredit: The creator\n\nUnofficial / unverified footage\n\n#gaming"
    assert all(row["caption_text"] == caption for row in posting.get_posts_for_item(item, tmp_db))
    assert execute(post, "unapprove", item)["status"] == "applied"
    assert posting.get_posts_for_item(item, tmp_db) == []
    assert all(row["caption_text"] == caption for row in posting.ensure_post_rows(saved, cfg=cfg, db_path=tmp_db))
    for body, tags in [("x" * 2200, ""), ("caption", " ".join(f"#tag{i}" for i in range(31)))]:
        response = post("/api/prepare", {"action": "edit_caption", "item_id": item,
                                        "payload": {"body": body, "hashtags": tags}})
        assert response.status_code == 409


def test_guarded_delete_preserves_identity_and_releases_card(tmp_db, tmp_path, panel, monkeypatch):
    _, _, post = panel
    media = tmp_path / "media"
    media.mkdir()
    safe, outside = media / "cut.mp4", tmp_path / "source.mp4"
    safe.touch()
    outside.touch()
    monkeypatch.setattr(config, "_MEDIA_ROOT", media)
    item = record(tmp_db, cut_path=str(safe), download_path=str(outside))
    telegram_bot.set_card_state({"mid": 42, "item_id": item}, tmp_db)
    assert execute(post, "delete", item)["status"] == "applied"
    assert not safe.exists() and outside.exists()
    assert not telegram_bot.card_state(tmp_db)
    assert db.get_item(item, tmp_db)["rejection_reason"] == "deleted from dashboard"
    assert discover.source_item("youtube", item, tmp_db)["id"] == item


def test_post_now_and_reorder_preserve_gap_daily_limit_and_meta_pause(tmp_db, panel, monkeypatch):
    cfg, _, post = panel
    monkeypatch.setattr(execution, "spawn", lambda *_: 123)
    target = record(tmp_db, "target", "approved")
    future = clock.utcnow() + timedelta(days=4)
    a, b = (record(tmp_db, id, "queued") for id in ("a", "b"))
    for id, moment in ((a, future), (b, future + timedelta(hours=6))):
        posting.ensure_post_rows(db.get_item(id, tmp_db), clock.iso(moment), cfg, tmp_db)
    assert execute(post, "down", a)["status"] == "applied"
    assert posting.get_posts_for_item(a, tmp_db)[0]["slot_datetime"] == clock.iso(future + timedelta(hours=6))
    assert execute(post, "pause")["status"] == "applied"
    assert post("/api/prepare", {"action": "post_now", "item_id": target}).status_code == 409
    assert execute(post, "resume")["status"] == "applied"
    plan = prepare(post, "post_now", target)
    assert "Start runner and post" in plan["description"] and "+05:30" in plan["description"]
    assert post("/api/execute", {"ticket": plan["ticket"]}).json["command"]["status"] == "applied"
    slot = posting.get_posts_for_item(target, tmp_db)[0]["slot_datetime"]
    assert slot == plan["payload"]["slot"] and db.get_item(target, tmp_db)["stage"] == "queued"
    day = scheduler.local_day(clock.parse(slot), cfg)
    _, tz, _ = scheduler.active_profile(cfg)
    local = clock.parse(slot).astimezone(tz)
    taken = [local.replace(hour=0), local.replace(hour=12)]
    assert all(scheduler.local_day(t, cfg) == day for t in taken)
    assert not scheduler.slot_allowed(local, taken, cfg)


def test_stop_waits_for_posting_and_force_refuses_it(tmp_db, panel, monkeypatch):
    cfg, _, post = panel
    monkeypatch.setattr(runner.os, "kill", forbidden)
    # Remote host avoids the PID liveness check so os.kill detects a real force signal.
    db.kv_set(runner.LOCK_KEY, json.dumps(dict(pid=os.getpid(), host="remote",
              heartbeat=time.time(), stage="post", token="lease")), tmp_db)
    assert "finishing post" in post("/api/runner/stop", {}).json["message"]
    db.kv_set(runner.STOP_KEY, str(time.time() - 121), tmp_db)
    result = post("/api/runner/force", {"token": "lease", "stage": "post"})
    assert result.status_code == 409 and "publishing" in result.json["error"]


def test_stop_exits_between_stages_and_releases_the_lease(tmp_db, monkeypatch):
    called = []

    def post_stage(cfg, path):
        called.append("post started")
        runner.request_stop(path)
        called.append("post finished")

    monkeypatch.setattr(runner, "STAGES", [("post", post_stage), ("cleanup", forbidden)])
    monkeypatch.setattr(runner.healthcheck, "ping", lambda *_, **__: None)
    assert runner.run_loop(cfg={}, db_path=tmp_db, max_cycles=3, interval_s=0) == 1
    assert called == ["post started", "post finished"]
    assert not runner.lock_state(tmp_db)


def test_settings_backup_and_next_cycle_reload(tmp_db, tmp_path, panel, monkeypatch):
    cfg, _, post = panel
    original = (config.ROOT_DIR / "config.yaml").read_bytes()
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    (tmp_path / "config.yaml").write_bytes(original)
    values = local_settings.editable(cfg)
    values.update(buffer=8, twitch=False)
    assert post("/api/settings", values).status_code == 200
    first = (tmp_path / "config.local.yaml").read_bytes()
    values["buffer"] = 9
    assert post("/api/settings", values).status_code == 200
    assert first in [file.read_bytes() for file in tmp_path.glob("*.bak")]
    assert (tmp_path / "config.yaml").read_bytes() == original
    called = []
    monkeypatch.setattr(runner.healthcheck, "ping", lambda *_, **__: None)
    monkeypatch.setattr(runner, "STAGES", [("record", lambda c, _: called.append(c["buffer"]["target_ready_items"]))])
    runner.run_loop(db_path=tmp_db, max_cycles=2, interval_s=0,
                    sleep=lambda _: local_settings.save({**values, "buffer": 10}, cfg))
    assert called == [9, 10]
    assert not config.get_config()["sources"]["twitch"]["enable_from"]


def test_old_sentence_fixture_scoring_is_identical_without_engagement():
    total = 120.0
    ends = [31.0, 41.0, 51.0, 69.5, 79.5, 95.0]
    loud = [(t / 10, -15.0 if 400 <= t <= 620 else -35.0) for t in range(1200)]
    regions = [region(talker(total, ends), total)]
    baseline = sentence_windows.candidates(regions, flat_scorer(total, loud), 18, 22, ES, SW)
    absent = sentence_windows.Scorer(total, loud, [], {}, engagement=[], weight=.2)
    assert sentence_windows.candidates(regions, absent, 18, 22, ES, SW) == baseline
    boosted = sentence_windows.Scorer(total, loud, [], {}, engagement=[(65, 95, 1)], weight=1)
    ranked, ends_count = sentence_windows.candidates(regions, boosted, 18, 22, ES, SW)
    assert ends_count == baseline[1] and ranked[0].end > baseline[0][0].end
