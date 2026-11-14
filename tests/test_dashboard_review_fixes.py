"""Regression coverage for dashboard command isolation and pause ownership."""

import builtins
import sqlite3
from datetime import datetime, timezone

import pytest

from dashboard import commands
from pipeline import clock, db, hooks, keywords, posting, rate_limit, runner
from test_dashboard import BASE, clip, confirmed, control, post, result


@pytest.mark.parametrize("error", [KeyError("broken command"), sqlite3.OperationalError("database is locked")])
def test_failed_command_rolls_back_and_never_retries(control, tmp_db, monkeypatch, caplog, error):
    _, _, cfg, _ = control
    _, broken = confirmed(control, "pause")
    _, following = confirmed(control, "pause")
    calls = []
    original = commands.apply

    def apply(command, cfg, path):
        calls.append(command["id"])
        if command["id"] == broken:
            db.kv_set("partial-command", "written", path)
            raise error
        return original(command, cfg, path)

    monkeypatch.setattr(commands, "apply", apply)
    stages = [("post", lambda *_: "stage ran")]
    first_cycle = runner.run_stages(cfg, tmp_db, stages)
    assert first_cycle["stages"] == {"post": "stage ran"}
    assert not first_cycle["errors"]
    assert result(tmp_db, broken)["status"] == "failed"
    assert str(error) in result(tmp_db, broken)["result"]
    assert f"Dashboard command {broken} failed" in caplog.text
    assert result(tmp_db, following)["status"] == "applied"
    assert db.kv_get("partial-command", db_path=tmp_db) is None
    second_cycle = runner.run_stages(cfg, tmp_db, stages)
    assert second_cycle["stages"] == {"post": "stage ran"}
    assert calls == [broken, following]


def test_boundary_failure_still_runs_stage(tmp_db, test_config, monkeypatch, caplog):
    def bad_boundary(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(commands, "runner_boundary", bad_boundary)
    cycle = runner.run_stages(test_config, tmp_db, [("post", lambda *_: "stage ran")])
    assert cycle["stages"] == {"post": "stage ran"}
    assert not cycle["errors"]
    assert "Dashboard boundary failed before post" in caplog.text


@pytest.mark.parametrize("error", [ModuleNotFoundError(name="dashboard"), RuntimeError("broken import")])
def test_runner_works_without_importable_dashboard(tmp_db, test_config, monkeypatch, error):
    real_import = builtins.__import__

    def reject_dashboard(name, *args, **kwargs):
        if name.startswith("dashboard"):
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_dashboard)
    cycle = runner.run_stages(test_config, tmp_db, [("post", lambda *_: "stage ran")])
    assert cycle["stages"] == {"post": "stage ran"}
    assert not cycle["errors"]


@pytest.mark.parametrize("meta_pause", ["none", "active", "expired", "during_hold"])
def test_resume_only_lifts_dashboard_pause(control, tmp_db, fake_clock, meta_pause):
    _, client, cfg, _ = control

    def cooldown():
        return rate_limit.active_cooldown(posting.META_SOURCE, tmp_db)

    if meta_pause in {"active", "expired"}:
        rate_limit.record_error(posting.META_SOURCE, "HTTP 429", 2, tmp_db)
    _, pause = confirmed(control, "pause")
    commands.runner_boundary(cfg, tmp_db, "post")
    assert result(tmp_db, pause)["status"] == "applied"
    assert posting.meta_paused(tmp_db)[0]
    if meta_pause == "during_hold":
        rate_limit.record_error(posting.META_SOURCE, "HTTP 429", 2, tmp_db)
    deadline, reason = cooldown()
    if meta_pause != "none":
        assert deadline == "2026-11-10T08:00:00Z" and reason == "HTTP 429"
    if meta_pause == "expired":
        fake_clock.advance(hours=3)
    _, resume = confirmed(control, "resume")
    commands.runner_boundary(cfg, tmp_db, "post")
    assert result(tmp_db, resume)["status"] == "applied"
    state = client.get("/api/state", base_url=BASE).get_json()
    assert not state["dashboard_paused"]
    should_remain_paused = meta_pause in {"active", "during_hold"}
    assert posting.meta_paused(tmp_db)[0] is should_remain_paused
    assert state["meta_cooldown_until"] == (deadline if should_remain_paused else None)
    if deadline:
        with db.db(tmp_db) as conn:
            row = conn.execute("SELECT paused_until, last_error, consecutive_errors FROM rate_limits").fetchone()
        assert tuple(row) == (deadline, reason, 1)


def test_meta_only_pause_has_deadline_and_refuses_resume(control, tmp_db, fake_clock):
    _, client, _, csrf = control
    rate_limit.record_error(posting.META_SOURCE, "HTTP 429", 2, tmp_db)
    state = client.get("/api/state", base_url=BASE).get_json()
    assert state["meta_paused"] and not state["dashboard_paused"]
    assert state["meta_cooldown_reason"] == "HTTP 429"
    assert state["meta_cooldown_until"] == "2026-11-10T08:00:00Z"

    response = post(client, csrf, "/api/prepare", {"action": "resume"})
    assert response.status_code == 409
    assert "No dashboard pause" in response.get_json()["error"]
    fake_clock.advance(minutes=30)
    assert "1.5h more" in posting.meta_paused(tmp_db)[1]


def test_resume_recognizes_legacy_dashboard_pause(control, tmp_db):
    _, _, cfg, _ = control
    rate_limit.record_error(
        posting.META_SOURCE, "Paused by dashboard; resume from the control panel",
        (datetime(9999, 1, 1, tzinfo=timezone.utc) - clock.utcnow()).total_seconds() / 3600,
        tmp_db,
    )
    assert rate_limit.dashboard_meta_paused(tmp_db)
    _, resume = confirmed(control, "resume")
    commands.runner_boundary(cfg, tmp_db, "post")
    assert result(tmp_db, resume)["status"] == "applied"
    assert not posting.meta_paused(tmp_db)[0]


def test_edited_hook_uses_local_keywords(control, clip, tmp_db, monkeypatch):
    _, _, cfg, _ = control
    text = "VICE CITY AFTER DARK?"
    confirmed(control, "edit_hook", clip["id"], {"hook_text": text})
    commands.runner_boundary(cfg, tmp_db, "hooks")
    monkeypatch.setattr(hooks, "extract_sample_frames", lambda *a, **k: [b"frame"])
    monkeypatch.setattr(hooks, "verify_hook_grounding", lambda *a, **k: (True, "PASS"))
    monkeypatch.setattr(hooks, "generate_hook_and_voiceover", lambda *a, **k: pytest.fail("unexpected model generation"))
    assert hooks.run_hooks_for_item(clip["id"], cfg, tmp_db)[0]
    assert keywords.load_keywords(db.get_item(clip["id"], tmp_db)) == ["VICE"]


def test_demo_hooks_and_styles_are_varied(tmp_path):
    from scripts.dashboard_demo_db import build_demo

    path, _ = build_demo(tmp_path / "demo.db", media=False)
    with db.db(path) as conn:
        rows = conn.execute("SELECT hook_text, hook_style FROM items").fetchall()
    assert len({row["hook_text"] for row in rows}) == 8
    assert {row["hook_style"] for row in rows} == {"curiosity", "question", "hype_analysis"}
