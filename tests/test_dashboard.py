"""Dashboard HTTP controls, snapshots, rendering and security. Every DB is temporary."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dashboard.app import create_app
from dashboard import commands, views
from dashboard.store import initialize, snapshot, transaction
from pipeline import (
    binding,
    clock,
    config,
    db,
    hooks,
    posting,
    rate_limit,
    render,
    runner,
    scheduler,
    telegram_bot,
    transcribe,
)

BASE = "http://127.0.0.1"


@pytest.fixture(autouse=True)
def forbid_dashboard_side_effects(monkeypatch, tmp_path):
    original = db.get_connection
    production = config.ROOT_DIR / "pipeline.db"

    def guarded(path=None):
        assert (
            Path(path or db.DEFAULT_DB_PATH).resolve() != production.resolve()
        ), "test touched production database"
        return original(path)

    monkeypatch.setattr(db, "get_connection", guarded)

    def forbidden(*args, **kwargs):
        raise AssertionError("dashboard attempted an external API or runner lock")

    monkeypatch.setattr(runner, "acquire_lock", forbidden)
    monkeypatch.setattr(posting.MetaClient, "request", forbidden)
    monkeypatch.setattr(telegram_bot.ApprovalBot, "send_message", forbidden)
    monkeypatch.setattr(telegram_bot.ApprovalBot, "send_video", forbidden)
    monkeypatch.setattr(hooks, "_call_gemini", forbidden)


@pytest.fixture
def control(tmp_db, tmp_path):
    cfg = copy.deepcopy(config.get_config(reload=True))
    cfg["approval"]["switch_to_auto_after_days"] = None
    app = create_app(tmp_db, cfg, tmp_path)
    app.config["TESTING"] = True
    client = app.test_client()
    data = client.get("/api/state", base_url=BASE).get_json()
    return app, client, cfg, data["csrf"]


@pytest.fixture
def clip(tmp_db, tmp_path):
    item_id = "dashboard-test"
    words = [
        {"word": w, "start": n * 0.4, "end": n * 0.4 + 0.3}
        for n, w in enumerate("Lucia drives through Vice City after dark".split())
    ]
    paths = {}
    for key in (
        "cut_path",
        "download_path",
        "voiceover_audio_path",
        "render_a_path",
        "render_b_path",
    ):
        path = tmp_path / (key + ".mp4")
        path.write_bytes(b"video")
        paths[key] = str(path)
    db.insert_discovered_item(
        {
            "id": item_id,
            "source": "youtube",
            "source_id": item_id,
            "source_url": "https://example.com/demo",
            "author_name": "Test creator",
            "original_title": "Vice City after dark",
            "raw_velocity": 1,
        },
        tmp_db,
    )
    fields = {
        **paths,
        "cut_start": 10.0,
        "cut_end": 30.0,
        "cut_duration": 20.0,
        "hook_text": "VICE CITY AFTER DARK?",
        "voiceover_script": "Lucia drives through Vice City after dark",
        "hook_style": "question",
        "hook_grounded": 1,
    }
    item = {"id": item_id, **fields}
    fields["hook_binding"] = binding.hook_record(
        item,
        fields["hook_text"],
        fields["voiceover_script"],
        binding.transcript_text(words),
        "segment_check",
    )
    fields["voiceover_binding"] = binding.voiceover_record(
        item, fields["voiceover_script"], paths["voiceover_audio_path"]
    )
    db.transition_stage(item_id, "rendered", fields, tmp_db)
    render.store_cut_words(
        item_id,
        db.get_item(item_id, tmp_db),
        words,
        transcribe.cache_key(config.get_config()),
        tmp_db,
    )
    return db.get_item(item_id, tmp_db)


def post(client, csrf, endpoint, body):
    return client.post(
        endpoint, json=body, headers={"X-CSRF-Token": csrf}, base_url=BASE
    )


def confirmed(control, action, item_id=None, payload=None):
    _, client, _, csrf = control
    response = post(
        client,
        csrf,
        "/api/prepare",
        {"action": action, "item_id": item_id, "payload": payload or {}},
    )
    assert response.status_code == 200, response.get_data(as_text=True)
    ticket = response.get_json()["ticket"]
    result = post(client, csrf, "/api/confirm", {"ticket": ticket})
    assert result.status_code == 202, result.get_data(as_text=True)
    return ticket, result.get_json()["command"]["id"]


def result(path, command_id):
    with db.db(path) as conn:
        return dict(
            conn.execute(
                "SELECT * FROM dashboard_commands WHERE id=?", (command_id,)
            ).fetchone()
        )


@pytest.mark.parametrize(
    "url", ["/", "/queue", "/analytics", "/failures", "/items/test"]
)
def test_pages_are_local_and_load_assets(control, url):
    _, client, _, _ = control
    response = client.get(url, base_url=BASE)
    assert response.status_code == 200
    assert b"/static/app.js" in response.data
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]


def test_read_state_does_not_pin_clocks_or_mutate_db(control, clip, tmp_db):
    _, client, cfg, _ = control
    cfg["approval"]["switch_to_auto_after_days"] = 7
    rows = posting.ensure_post_rows(
        clip, slot_datetime="2026-01-01T00:00:00Z", cfg=cfg, db_path=tmp_db
    )
    posting.set_status(rows[0]["id"], "live", tmp_db, posted_at="2026-01-01T00:00:00Z")
    with db.db(tmp_db) as conn:
        before = list(conn.iterdump())
    response = client.get("/api/state", base_url=BASE)
    assert response.get_json()["approval_mode"] == "auto"
    with db.db(tmp_db) as conn:
        assert list(conn.iterdump()) == before
    assert db.kv_get(db.K_FIRST_LIVE, db_path=tmp_db) is None
    assert db.kv_get(telegram_bot.K_AUTO_SINCE, db_path=tmp_db) is None


def test_prepare_is_readonly_confirm_is_idempotent_and_releases_card(
    control, clip, tmp_db, monkeypatch
):
    _, client, cfg, csrf = control
    telegram_bot.set_card_state({"mid": 123, "item_id": clip["id"]}, tmp_db)
    original = telegram_bot.decide
    calls = []

    def decide(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(telegram_bot, "decide", decide)
    ticket, cid = confirmed(control, "approve", clip["id"])
    assert db.get_item(clip["id"], tmp_db)["stage"] == "rendered"
    assert post(client, csrf, "/api/confirm", {"ticket": ticket}).status_code == 202
    commands.runner_boundary(cfg, tmp_db, "approve")
    commands.runner_boundary(cfg, tmp_db, "schedule")
    assert len(calls) == 1
    assert db.get_item(clip["id"], tmp_db)["stage"] == "approved"
    assert telegram_bot.card_state(tmp_db) is None
    assert result(tmp_db, cid)["status"] == "applied"
    assert telegram_bot.decide(clip["id"], "ok", tmp_db)[0] == ""
    bot = telegram_bot.ApprovalBot(
        cfg=cfg, db_path=tmp_db, chat_id="123", transport=lambda *a: None
    )
    assert bot.on_button("a:ok:" + clip["id"], 123) == telegram_bot.STALE


@pytest.mark.parametrize(
    "action,expected", [("aonly", "approved"), ("reject", "rejected")]
)
def test_other_decisions_go_through_existing_api(
    control, clip, tmp_db, action, expected
):
    _, _, cfg, _ = control
    confirmed(control, action, clip["id"])
    commands.runner_boundary(cfg, tmp_db, "approve")
    item = db.get_item(clip["id"], tmp_db)
    assert item["stage"] == expected
    if action == "aonly":
        assert item["chosen_variant"] == "A" and item["ab_excluded"] == 1


def test_review_returns_to_render_and_releases_same_card(control, clip, tmp_db):
    _, _, cfg, _ = control
    db.transition_stage(
        clip["id"],
        "needs_review",
        {
            "hook_grounded": 0,
            "hook_binding": binding.with_check(clip["hook_binding"], "needs_review"),
        },
        tmp_db,
    )
    telegram_bot.set_card_state(
        {"mid": 1, "item_id": clip["id"], "kind": "review"}, tmp_db
    )
    confirmed(control, "hookok", clip["id"])
    commands.runner_boundary(cfg, tmp_db, "render")
    item = db.get_item(clip["id"], tmp_db)
    assert item["stage"] == "cut" and item["hook_grounded"] == 1
    assert binding.hook_binding_problem(item) is None
    assert telegram_bot.card_state(tmp_db) is None


def test_stale_confirmation_and_telegram_wins_race(control, clip, tmp_db):
    _, client, cfg, csrf = control
    _, cid = confirmed(control, "approve", clip["id"])
    telegram_bot.decide(clip["id"], "no", tmp_db)
    commands.runner_boundary(cfg, tmp_db, "approve")
    assert result(tmp_db, cid)["status"] == "refused"
    assert db.get_item(clip["id"], tmp_db)["stage"] == "rejected"
    assert (
        post(
            client, csrf, "/api/prepare", {"action": "approve", "item_id": clip["id"]}
        ).status_code
        == 409
    )


def test_preview_never_approved_or_mutated(control, clip, tmp_db):
    _, client, _, csrf = control
    db.transition_stage(clip["id"], "cut", db_path=tmp_db)
    db.set_preview_only(clip["id"], True, tmp_db)
    for action in ("approve", "aonly", "hookok", "edit_hook"):
        response = post(
            client,
            csrf,
            "/api/prepare",
            {
                "action": action,
                "item_id": clip["id"],
                "payload": {"hook_text": "VICE CITY?"},
            },
        )
        assert (
            response.status_code == 409
            and "Preview-only" in response.get_json()["error"]
        )
    assert db.get_item(clip["id"], tmp_db)["stage"] == "cut"


def test_pause_resume_use_existing_meta_mechanism(control, tmp_db, monkeypatch):
    _, _, cfg, _ = control
    confirmed(control, "pause")
    commands.runner_boundary(cfg, tmp_db, "settle")
    assert rate_limit.is_source_paused(posting.META_SOURCE, tmp_db)[0]
    monkeypatch.setattr(
        posting,
        "settle_publishing_rows",
        lambda **kw: pytest.fail("paused settlement reached Meta"),
    )
    assert runner._stage_settle(cfg, tmp_db) == []
    assert posting.run_posting_stage(cfg, tmp_db) == {}
    confirmed(control, "resume")
    commands.runner_boundary(cfg, tmp_db, "settle")
    assert not posting.meta_paused(tmp_db)[0]


@pytest.mark.parametrize(
    "stage,target",
    [("download", "discovered"), ("cut", "downloaded"), ("render", "cut")],
)
def test_retry_whitelist(control, clip, tmp_db, stage, target):
    _, _, cfg, _ = control
    db.mark_failed(clip["id"], stage, "test error", tmp_db)
    confirmed(control, "retry", clip["id"])
    commands.runner_boundary(cfg, tmp_db, "download")
    assert db.get_item(clip["id"], tmp_db)["stage"] == target


@pytest.mark.parametrize("stage", ["posting", "publishing", "hooks", "voiceover"])
def test_unsafe_retry_has_no_button_and_is_refused(control, clip, tmp_db, stage):
    _, client, _, csrf = control
    db.mark_failed(clip["id"], stage, "test error", tmp_db)
    assert (
        "retry"
        not in client.get("/api/items/" + clip["id"], base_url=BASE).get_json()[
            "actions"
        ]
    )
    assert (
        post(
            client, csrf, "/api/prepare", {"action": "retry", "item_id": clip["id"]}
        ).status_code
        == 409
    )


def test_started_posts_block_all_item_actions(control, clip, tmp_db):
    _, client, cfg, csrf = control
    rows = posting.ensure_post_rows(
        clip, slot_datetime=db.now(), cfg=cfg, db_path=tmp_db
    )
    posting.set_status(
        rows[0]["id"], "publishing", tmp_db, remote_container_id="container"
    )
    db.mark_failed(clip["id"], "render", "misclassified", tmp_db)
    assert (
        client.get("/api/items/" + clip["id"], base_url=BASE).get_json()["actions"]
        == []
    )
    assert (
        post(
            client, csrf, "/api/prepare", {"action": "retry", "item_id": clip["id"]}
        ).status_code
        == 409
    )


def test_edit_invalidates_every_publishable_artifact(control, clip, tmp_db):
    _, client, cfg, csrf = control
    db.transition_stage(clip["id"], "queued", db_path=tmp_db)
    posting.ensure_post_rows(clip, slot_datetime=db.now(), cfg=cfg, db_path=tmp_db)
    confirmed(
        control, "edit_hook", clip["id"], {"hook_text": "WHERE DOES LUCIA DRIVE?"}
    )
    commands.runner_boundary(cfg, tmp_db, "render")
    item = db.get_item(clip["id"], tmp_db)
    assert item["stage"] == "cut" and item["hook_grounded"] == 0
    assert binding.hook_binding_problem(item)
    for field in (
        "render_a_path",
        "render_b_path",
        "caption_text",
        "voiceover_binding",
        "voiceover_audio_path",
        "approval_type",
    ):
        assert item[field] is None
    assert posting.get_posts_for_item(clip["id"], tmp_db) == []
    assert (
        post(
            client, csrf, "/api/prepare", {"action": "approve", "item_id": clip["id"]}
        ).status_code
        == 409
    )


def test_reslot_uses_scheduler_enforces_gap_and_two_per_day(
    control, clip, tmp_db, fake_clock
):
    _, _, cfg, _ = control
    cfg["posting"]["daily_post_target"] = 20
    db.transition_stage(clip["id"], "queued", db_path=tmp_db)
    posting.ensure_post_rows(
        clip, slot_datetime="2026-11-10T07:30:00Z", cfg=cfg, db_path=tmp_db
    )
    confirmed(control, "reslot", clip["id"], {"after": "2026-11-11T06:00:00Z"})
    commands.runner_boundary(cfg, tmp_db, "schedule")
    first = posting.get_posts_for_item(clip["id"], tmp_db)[0]
    assert first["slot_datetime"] == "2026-11-11T07:30:00Z"
    bounded = copy.deepcopy(cfg)
    bounded["posting"]["daily_post_target"] = 2
    for index in range(2):
        item = {**clip, "id": f"other{index}", "source_id": f"other{index}"}
        db.insert_discovered_item(item, tmp_db)
        db.transition_stage(item["id"], "queued", db_path=tmp_db)
        slot = scheduler.next_free_slot(
            clock.parse("2026-11-11T06:00:00Z"), bounded, tmp_db
        )
        posting.ensure_post_rows(
            item, slot_datetime=clock.iso(slot), cfg=cfg, db_path=tmp_db
        )
    rows = []
    with db.db(tmp_db) as conn:
        rows = conn.execute(
            "SELECT DISTINCT item_id,slot_datetime FROM posts ORDER BY slot_datetime"
        ).fetchall()
    from collections import Counter

    days = Counter(
        scheduler.local_day(clock.parse(r["slot_datetime"]), cfg) for r in rows
    )
    assert max(days.values()) <= 2
    moments = [clock.parse(r["slot_datetime"]) for r in rows]
    assert all(
        (b - a).total_seconds() >= cfg["posting"]["min_gap_hours"] * 3600
        for a, b in zip(moments, moments[1:])
    )


def test_csrf_host_origin_and_signed_confirmation(control, clip, tmp_db):
    _, client, _, csrf = control
    assert (
        client.post("/api/prepare", json={"action": "pause"}, base_url=BASE).status_code
        == 403
    )
    assert client.get("/api/state", base_url="http://evil.example").status_code == 403
    assert (
        client.post(
            "/api/prepare",
            json={"action": "pause"},
            base_url=BASE,
            headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    assert post(client, csrf, "/api/confirm", {"ticket": "forged"}).status_code == 409
    assert client.get("/api/confirm", base_url=BASE).status_code == 405
    with db.db(tmp_db) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM dashboard_commands").fetchone()[0] == 0
        )


def test_media_range_and_path_safety(control, clip, tmp_db, tmp_path):
    _, client, _, _ = control
    url = "/media/" + clip["id"] + "/a"
    response = client.get(url, base_url=BASE, headers={"Range": "bytes=0-2"})
    assert response.status_code == 206 and response.data == b"vid"
    db.transition_stage(
        clip["id"],
        "rendered",
        {"render_a_path": str(config.ROOT_DIR / "config.yaml")},
        tmp_db,
    )
    assert client.get(url, base_url=BASE).status_code == 404
    assert (
        client.get(
            "/media/" + clip["id"] + "/../../config.yaml", base_url=BASE
        ).status_code
        == 404
    )


def test_controls_leave_runner_lock_and_dry_run_unchanged(control, clip, tmp_db):
    _, _, cfg, _ = control
    original = config.ROOT_DIR.joinpath("config.yaml").read_bytes()
    lock = json.dumps({"pid": 123, "host": "test", "heartbeat": 1})
    db.kv_set("runner:lock", lock, tmp_db)
    confirmed(control, "aonly", clip["id"])
    commands.runner_boundary(cfg, tmp_db, "schedule")
    assert db.kv_get("runner:lock", db_path=tmp_db) == lock
    assert cfg["app"]["dry_run"] is True
    assert config.ROOT_DIR.joinpath("config.yaml").read_bytes() == original


def test_transaction_rollback_and_thread_context_isolation(tmp_db):
    initialize(tmp_db)
    with pytest.raises(RuntimeError):
        with transaction(tmp_db):
            db.kv_set("rollback", "yes", tmp_db)
            raise RuntimeError("crash")
    assert db.kv_get("rollback", db_path=tmp_db) is None
    with snapshot(tmp_db):
        db.kv_set("ram-only", "yes", tmp_db)
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(db.kv_get, "ram-only", None, tmp_db).result() is None
    assert db.kv_get("ram-only", db_path=tmp_db) is None


def test_concurrent_confirm_and_runner_boundary(control, clip, tmp_db):
    _, _, cfg, _ = control
    plan = commands.prepare("aonly", clip["id"], {}, tmp_db, cfg)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(
            pool.map(lambda _: commands.enqueue(plan, "same-id", tmp_db), range(8))
        )
    assert len({r["id"] for r in rows}) == 1
    runner.run_stages(cfg, tmp_db, stages=[("test", lambda *_: None)])
    assert db.get_item(clip["id"], tmp_db)["stage"] == "approved"
    with db.db(tmp_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM stage_log WHERE stage='approval'"
            ).fetchone()[0]
            == 1
        )
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10000


def test_gemini_runtime_is_observed_not_guessed(control, tmp_db, monkeypatch):
    _, _, cfg, _ = control
    monkeypatch.setattr(hooks, "_now", lambda: 100)
    monkeypatch.setattr(hooks, "_QUOTA_BLOCKED_UNTIL", 700)
    commands.runner_boundary(cfg, tmp_db, "render")
    runtime = json.loads(db.kv_get("dashboard:runtime", db_path=tmp_db))
    assert (
        599
        <= (
            clock.parse(runtime["gemini_blocked_until"]) - clock.utcnow()
        ).total_seconds()
        <= 600
    )


def test_cli_binds_loopback_only(monkeypatch, tmp_db):
    import waitress
    from dashboard import __main__

    called = {}
    monkeypatch.setattr("sys.argv", ["dashboard", "--db", str(tmp_db)])
    monkeypatch.setattr(waitress, "serve", lambda app, **kwargs: called.update(kwargs))
    __main__.main()
    assert called["host"] == "127.0.0.1"


@pytest.mark.parametrize("verdict", ["pass", "claim_fail", "gemini_fail", "outage"])
def test_edited_hook_uses_real_claim_and_binding_path(
    control, clip, tmp_db, monkeypatch, verdict
):
    _, client, cfg, csrf = control
    candidate = (
        "CONFIRMED VICE CITY RELEASE?"
        if verdict == "claim_fail"
        else "VICE CITY AFTER DARK?"
    )
    confirmed(control, "edit_hook", clip["id"], {"hook_text": candidate})
    commands.runner_boundary(cfg, tmp_db, "hooks")
    monkeypatch.setattr(hooks, "extract_sample_frames", lambda *a, **k: [b"frame"])

    def no_generation(*a, **k):
        raise AssertionError("An edited hook must not be silently replaced")

    monkeypatch.setattr(hooks, "generate_hook_and_voiceover", no_generation)
    checked = []

    def verifier(*a, **k):
        checked.append(a)
        if verdict == "outage":
            raise hooks.GeminiTransientError("offline")
        return (
            (False, "FAIL: unsupported scene")
            if verdict == "gemini_fail"
            else (True, "PASS")
        )

    monkeypatch.setattr(hooks, "verify_hook_grounding", verifier)
    success, message = hooks.run_hooks_for_item(clip["id"], cfg, tmp_db)
    item = db.get_item(clip["id"], tmp_db)
    assert item["hook_text"] == candidate
    assert not item["render_a_path"] and not item["render_b_path"]
    assert (
        post(
            client, csrf, "/api/prepare", {"action": "approve", "item_id": clip["id"]}
        ).status_code
        == 409
    )
    if verdict == "pass":
        assert success and checked and item["stage"] == "cut"
        assert binding.hook_binding_problem(item) is None
        assert json.loads(item["hook_binding"])["check"] == "segment_check"
    elif verdict == "outage":
        assert not success and hooks.is_transient_result(message)
        assert item["stage"] == "cut" and not item["hook_grounded"]
        assert json.loads(item["hook_review"])["kind"] == "dashboard_edit"
    else:
        assert not success and item["stage"] == "needs_review"
        assert bool(checked) == (verdict == "gemini_fail")
        assert not item["hook_grounded"]


def test_runtime_settings_win_until_runner_restart(control, tmp_db):
    import time

    _, client, cfg, _ = control
    commands.runner_boundary(cfg, tmp_db, "hooks")
    db.kv_set(
        "runner:lock",
        json.dumps(
            {"host": "other-host", "pid": 1, "heartbeat": time.time(), "started_at": 0}
        ),
        tmp_db,
    )
    cfg["app"]["dry_run"] = False
    cfg["posting"]["profile"] = "server"
    data = client.get("/api/state", base_url=BASE).get_json()
    assert data["config_changed"] and data["runtime_known"]
    assert data["dry_run"] is True and data["profile"] == "local"
    commands.runner_boundary(cfg, tmp_db, "schedule")
    data = client.get("/api/state", base_url=BASE).get_json()
    assert (
        not data["config_changed"]
        and not data["dry_run"]
        and data["profile"] == "server"
    )


def test_bad_payload_is_refused_before_enqueue(control, clip):
    _, client, _, csrf = control
    for value in (42, [], {"text": "wrong"}):
        assert (
            post(
                client,
                csrf,
                "/api/prepare",
                {
                    "action": "edit_hook",
                    "item_id": clip["id"],
                    "payload": {"hook_text": value},
                },
            ).status_code
            == 409
        )


def test_demo_refuses_production_and_covers_all_stages(tmp_path):
    from scripts.dashboard_demo_db import build_demo

    with pytest.raises(ValueError):
        build_demo(tmp_path / "pipeline.db", media=False)
    path = tmp_path / "demo.db"
    build_demo(path, media=False)
    with db.db(path) as conn:
        assert set(
            r[0] for r in conn.execute("SELECT DISTINCT stage FROM items")
        ) == set(db.STAGES)
        first, last = conn.execute(
            "SELECT MIN(fetched_at), MAX(fetched_at) FROM metrics"
        ).fetchone()
        assert (clock.parse(last) - clock.parse(first)).days >= 14
        assert set(
            r[0] for r in conn.execute("SELECT DISTINCT variant FROM posts")
        ) >= {"A", "B"}
    with pytest.raises(ValueError):
        build_demo(path, media=False)


def test_hook_edit_waits_for_initial_script(control, clip, tmp_db):
    _, client, _, csrf = control
    db.transition_stage(clip["id"], "cut", {"voiceover_script": None}, tmp_db)
    detail = client.get("/api/items/" + clip["id"], base_url=BASE).get_json()
    assert "edit_hook" not in detail["actions"]
    response = post(
        client,
        csrf,
        "/api/prepare",
        {
            "action": "edit_hook",
            "item_id": clip["id"],
            "payload": {"hook_text": "VICE CITY?"},
        },
    )
    assert (
        response.status_code == 409 and "original hook" in response.get_json()["error"]
    )
