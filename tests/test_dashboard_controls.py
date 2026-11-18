"""Dashboard controls use isolated SQLite, files and stubbed external services."""

import copy
import json
import os
import socket
import time

import pytest

from dashboard import commands, queue_controls, settings, workers
from dashboard.app import create_app
from dashboard.store import initialize
from pipeline import clock, config, creation, db, endings, hooks, posting, runner, sentence_windows, telegram_bot
from test_dashboard import BASE, clip, confirmed, post, result
from test_sentence_windows import ES, SW, region, talker


@pytest.fixture
def local(tmp_db, tmp_path, monkeypatch):
    cfg = copy.deepcopy(config.get_config())
    cfg["app"]["dry_run"] = True
    app = create_app(tmp_db, cfg, tmp_path)
    app.config["TESTING"] = True
    client = app.test_client()
    csrf = client.get("/api/state", base_url=BASE).get_json()["csrf"]
    for target, name in ((posting.MetaClient, "request"), (telegram_bot.ApprovalBot, "send_message"),
                         (telegram_bot.ApprovalBot, "send_video"), (hooks, "_call_gemini")):
        monkeypatch.setattr(target, name, lambda *args, **kwargs: pytest.fail("Flask called an external service"))
    return app, client, cfg, csrf


def test_quick_actions_caption_credit_and_post_rows(local, clip, tmp_db):
    _, _, cfg, _ = local
    item_id = clip["id"]
    _, command_id = confirmed(local, "aonly", item_id)
    assert result(tmp_db, command_id)["status"] == "applied"
    approved = db.get_item(item_id, tmp_db)
    assert approved["stage"] == "approved" and approved["chosen_variant"] == "A"
    posting.ensure_post_rows(approved, cfg=cfg, db_path=tmp_db)
    db.transition_stage(item_id, "queued", db_path=tmp_db)
    _, command_id = confirmed(local, "edit_caption", item_id, {"body": "A better caption", "hashtags": "#GTA"})
    assert result(tmp_db, command_id)["status"] == "applied"
    item = db.get_item(item_id, tmp_db)
    caption = posting.caption_for_item(item, cfg)
    assert caption.split("\n\n") == ["A better caption", posting.caption_credit(item), "#GTA"]
    assert "Credit: " + item["author_name"] in caption
    assert all(row["caption_text"] == caption for row in posting.get_posts_for_item(item_id, tmp_db))
    submitted = []

    class Recorder:
        def publish(self, post_id, item):
            submitted.append(posting.get_post(post_id, tmp_db)["caption_text"])
            return "pending"

    posting.run_posting_for_item(item_id, cfg, tmp_db, publisher=Recorder())
    assert submitted and all(text == caption for text in submitted)
    _, command_id = confirmed(local, "unapprove", item_id)
    assert result(tmp_db, command_id)["status"] == "applied"
    assert db.get_item(item_id, tmp_db)["stage"] == "awaiting_approval"
    assert posting.get_posts_for_item(item_id, tmp_db) == []
    assert runner.active_lock(tmp_db) is None


@pytest.mark.parametrize("action", ["delete", "remake_cut", "remake_hook", "edit_caption",
                                    "unapprove", "up", "down", "post_now"])
def test_new_actions_refuse_items_that_started_posting(local, clip, tmp_db, action):
    _, client, cfg, csrf = local
    db.transition_stage(clip["id"], "queued", db_path=tmp_db)
    rows = posting.ensure_post_rows(clip, cfg=cfg, db_path=tmp_db)
    posting.set_status(rows[0]["id"], "publishing", tmp_db, publish_started_at=db.now())
    response = post(client, csrf, "/api/prepare", {"action": action, "item_id": clip["id"]})
    assert response.status_code == 409
    assert client.get("/api/items/" + clip["id"], base_url=BASE).json["actions"] == []


def test_preview_only_cannot_be_approved_queued_or_posted(local, clip, tmp_db):
    _, client, _, csrf = local
    db.transition_stage(clip["id"], "cut", {"preview_only": 1}, tmp_db)
    for action in ("approve", "aonly", "bonly", "post_now"):
        response = post(client, csrf, "/api/prepare", {"action": action, "item_id": clip["id"]})
        assert response.status_code == 409
    assert client.get("/api/items/" + clip["id"], base_url=BASE).json["actions"] == []


def test_delete_releases_card_and_keeps_external_file(local, clip, tmp_db, tmp_path):
    item_id = clip["id"]
    guarded_root = tmp_path / "guarded"
    app = create_app(tmp_db, local[2], guarded_root)
    client = app.test_client()
    csrf = client.get("/api/state", base_url=BASE).json["csrf"]
    guarded_root.mkdir()
    inside = guarded_root / "clip.mp4"
    inside.write_bytes(b"video")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")
    db.transition_stage(item_id, "rendered", {"render_a_path": str(inside),
                                            "render_b_path": str(outside)}, tmp_db)
    telegram_bot.set_card_state({"mid": 1, "item_id": item_id}, tmp_db)
    _, command_id = confirmed((app, client, local[2], csrf), "delete", item_id)
    assert result(tmp_db, command_id)["status"] == "applied"
    assert db.get_item(item_id, tmp_db)["rejection_reason"] == "deleted from dashboard"
    assert not inside.exists() and outside.exists()
    assert not telegram_bot.card_state(tmp_db)


def test_real_atomic_lock_contention_queues_then_applies(local, clip, tmp_db):
    _, _, cfg, _ = local
    assert runner.acquire_lock(tmp_db, kind="loop")
    try:
        _, cid = confirmed(local, "aonly", clip["id"])
        assert result(tmp_db, cid)["status"] == "pending"
        assert db.get_item(clip["id"], tmp_db)["stage"] == "rendered"
        commands.runner_boundary(cfg, tmp_db, "approve")
        assert result(tmp_db, cid)["status"] == "applied"
    finally:
        runner.release_lock(tmp_db)


def test_make_cli_worker_uses_creation_stages_only(local, tmp_db, monkeypatch):
    _, _, cfg, _ = local
    monkeypatch.setattr(workers, "spawn", lambda *args: 111)
    _, job = confirmed(local, "clip_url", payload={"url": "https://youtu.be/dQw4w9WgXcQ", "start": "00:20"})
    calls = []

    def advance(stage, target):
        def run(item, *_args, **_kwargs):
            calls.append(stage)
            item_id = item["id"] if isinstance(item, dict) else item
            db.transition_stage(item_id, target, db_path=tmp_db)
            return (True, stage, {}) if stage == "render" else (True, stage)
        return run

    monkeypatch.setattr(creation.download, "download_single_item", advance("download", "downloaded"))
    monkeypatch.setattr(creation.cut, "run_cut_item", advance("cut", "cut"))
    monkeypatch.setattr(creation.render, "run_render_item", advance("render", "rendered"))
    monkeypatch.setattr(runner, "run_stages", lambda *a, **k: pytest.fail("make ran the normal cycle"))
    for module, name in ((runner.posting, "run_posting_stage"), (runner.analytics, "run_analytics_stage"),
                         (runner.cleanup, "run_cleanup_stage"), (runner.scheduler, "run_scheduler_stage")):
        monkeypatch.setattr(module, name, lambda *a, **k: pytest.fail("make entered a non-creation stage"))
    monkeypatch.setattr("sys.argv", ["runner", "--db", str(tmp_db), "make", "--job", job])
    runner.main()
    assert calls == ["download", "cut", "render"]
    assert result(tmp_db, job)["status"] == "applied"
    item = db.get_items_by_stage("awaiting_approval", db_path=tmp_db)[0]
    assert item["source"] == "manual" and item["hint_start"] == 20
    _, client, _, csrf = local
    response = post(client, csrf, "/api/prepare", {"action": "clip_url",
                   "payload": {"url": "https://www.youtube.com/shorts/dQw4w9WgXcQ"}})
    assert response.status_code == 409 and "/items/" + item["id"] in response.json["error"]


def test_graceful_stop_leaves_current_post_to_finish(tmp_db, monkeypatch):
    calls = []

    def current_post(cfg, path):
        calls.append("start")
        lock = runner.active_lock(path)
        db.kv_set(runner.STOP_KEY, json.dumps({"at": time.time(), "token": lock["token"]}), path)
        calls.append("complete")

    monkeypatch.setattr(runner, "STAGES", [("post", current_post),
                                          ("cleanup", lambda *_: pytest.fail("stop ignored"))])
    assert runner.run_loop(db_path=tmp_db, interval_s=0, max_cycles=2) == 1
    assert calls == ["start", "complete"]
    assert runner.active_lock(tmp_db) is None


def test_force_stop_refuses_posting_and_requires_delay(local, tmp_db):
    _, client, _, csrf = local
    lock = {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat": time.time(),
            "token": "test", "kind": "loop"}
    db.kv_set(runner.LOCK_KEY, json.dumps(lock), tmp_db)
    db.kv_set("runner:stage", "post", tmp_db)
    db.kv_set(runner.STOP_KEY, json.dumps({"token": "test", "at": time.time() - 60}), tmp_db)
    response = post(client, csrf, "/api/runner", {"operation": "force", "stage": "post"})
    assert "two minutes" in response.json["error"]
    db.kv_set(runner.STOP_KEY, json.dumps({"token": "test", "at": time.time() - 125}), tmp_db)
    response = post(client, csrf, "/api/runner", {"operation": "force", "stage": "post"})
    assert "publishing" in response.json["error"]


def test_settings_backup_and_reload(tmp_path, monkeypatch):
    base = config.project_root() / "config.yaml"
    original = base.read_bytes()
    (tmp_path / "config.yaml").write_bytes(original)
    monkeypatch.setattr(config, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(config, "_CONFIG_CACHE", None)
    cfg = config.get_config(reload=True)
    values = settings.values(cfg)
    values.update(heatmap_weight=0.4, twitch=False, telegram=False)
    settings.save(values, cfg, tmp_path)
    first = (tmp_path / "config.local.yaml").read_bytes()
    settings.save({**values, "heatmap_weight": 0.5}, cfg, tmp_path)
    assert first in [path.read_bytes() for path in tmp_path.glob("config.local.yaml.*.bak")]
    merged = config.get_config(reload=True)
    assert merged["cut"]["heatmap_weight"] == 0.5
    assert merged["sources"]["twitch"]["enable_from"] == cfg["sources"]["twitch"]["enable_from"]
    assert merged["approval"]["dashboard_only"]
    assert (tmp_path / "config.yaml").read_bytes() == original


def test_no_heatmap_matches_existing_sentence_window_fixture_exactly():
    total = 120.0
    words = talker(total, [31.0, 41.0, 51.0, 69.5, 79.5, 95.0])
    loudness = [(t / 10, -15.0 if 400 <= t <= 620 else -35.0) for t in range(1200)]
    scene_cuts = [12.5, 31.25, 45.0, 68.75]

    class PreviousScorer(sentence_windows.Scorer):
        def score(self, start, end):
            scenes = min(1.0, sum(start <= t <= end for t in self.cuts) / 5.0)
            mean = self.mean_lufs(start, end)
            audio = 0.5 if mean is None else max(0.0, min(1.0, (mean + 50.0) / 40.0))
            penalty = (0.05 if start < 0.05 * total else 0.0) + (0.05 if end > 0.95 * total else 0.0)
            return 0.75 * audio + 0.25 * scenes - penalty

    score = sentence_windows.Scorer(total, loudness, scene_cuts, {"cut": {"heatmap_weight": 2}}, peaks=[])
    old = PreviousScorer(total, loudness, scene_cuts)
    windows = [region(words, total)]
    current = sentence_windows.candidates(windows, score, 18.0, 22.0, ES, SW)
    assert current == sentence_windows.candidates(windows, old, 18.0, 22.0, ES, SW)
    boosted = sentence_windows.Scorer(total, loudness, scene_cuts, {"cut": {"heatmap_weight": 2}},
                                      peaks=[(0, 31.0, 1.0)])
    selected, _ = sentence_windows.candidates(windows, boosted, 18.0, 22.0, ES, SW)
    assert selected[0] != current[0][0]
    safe = endings.safe_spans(words, 0, total, total, ES)
    assert all(endings.span_at(window.end, safe) for window in selected)


def test_connection_checks_use_worker_for_meta_gemini_and_telegram(local, monkeypatch):
    import connection_checks

    _, client, _, csrf = local
    called = []
    env = {"GEMINI_API_KEY": "demo", "IG_ACCESS_TOKEN": "ig", "TELEGRAM_BOT_TOKEN": "tg",
           "TELEGRAM_CHAT_ID": "123"}
    monkeypatch.setattr(connection_checks, "current_environment", lambda: env)
    monkeypatch.setattr(connection_checks.requests, "request",
                        lambda *a, **k: pytest.fail("Flask made a network request"))

    def worker(payload):
        called.append(payload)
        return {"status": "ok", "reason": "sent"} if payload["operation"] == "telegram" else {
            "status": 200, "text": '{"data":{"is_valid":true}}'}

    monkeypatch.setattr(workers, "connection_request", worker)
    for service in ("gemini", "instagram_token"):
        assert post(client, csrf, "/api/connections/test", {"service": service}).status_code == 200
    assert post(client, csrf, "/api/connections/send-telegram-test", {}).json["result"]["status"] == "ok"
    assert [call["operation"] for call in called] == ["request", "request", "telegram"]


def test_post_now_slot_obeys_daily_cap_and_min_gap(local, clip, tmp_db, fake_clock, monkeypatch):
    _, _, cfg, _ = local
    cfg["posting"]["min_gap_hours"] = 3
    cfg["posting"]["daily_post_target"] = 2
    start = clock.utcnow()
    taken = [start, start.replace(hour=start.hour + 3)]
    monkeypatch.setattr(queue_controls.scheduler, "reserved_times", lambda *_: taken)
    slot = queue_controls.earliest(cfg, tmp_db, clip["id"])
    tz = queue_controls.scheduler.active_profile(cfg)[1]
    assert slot.astimezone(tz).date() > start.astimezone(tz).date()
    assert queue_controls.allowed(slot, taken, cfg)


def test_manual_source_ids_are_separate_by_platform(tmp_db):
    video = "dQw4w9WgXcQ"
    yt_id = creation.insert_manual({"url": f"https://youtu.be/{video}"}, "make-1", tmp_db)
    tw_id = creation.insert_manual({"url": f"https://clips.twitch.tv/{video}"}, "make-2", tmp_db)
    assert yt_id != tw_id
    assert creation.existing("youtube", video, tmp_db) == yt_id
    assert creation.existing("twitch", video, tmp_db) == tw_id
    assert not db.insert_discovered_item({"id": "new", "source_id": video, "source": "youtube",
                                          "source_url": f"https://youtu.be/{video}"}, tmp_db)


def test_post_now_applies_confirmed_slot_and_only_starts_runner(local, clip, tmp_db, fake_clock, monkeypatch):
    _, client, cfg, csrf = local
    item_id = clip["id"]
    db.transition_stage(item_id, "approved", db_path=tmp_db)
    started = []
    monkeypatch.setattr(workers, "control", lambda operation, *_: started.append(operation))
    preparation = post(client, csrf, "/api/prepare", {"action": "post_now", "item_id": item_id})
    assert preparation.status_code == 200
    assert "dry run" in preparation.json["description"] and "Start runner and post" in preparation.json["description"]
    applied = post(client, csrf, "/api/confirm", {"ticket": preparation.json["ticket"]})
    assert applied.json["command"]["status"] == "applied"
    assert db.get_item(item_id, tmp_db)["stage"] == "queued"
    posts = posting.get_posts_for_item(item_id, tmp_db)
    assert posting.item_slot(posts) == preparation.json["payload"]["slot"]
    assert {row["status"] for row in posts} == {"pending"}
    assert started == ["start"]
