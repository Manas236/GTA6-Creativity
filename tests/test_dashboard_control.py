"""Control API regression tests using a disposable database, config and media tree."""
import pytest
from dashboard import settings
from dashboard.app import create_app
from pipeline import config, db, posting, scheduler

BASE = "http://localhost"


def test_saving_twitch_toggle_preserves_scheduled_enable(tmp_path):
    cfg = {"sources": {"twitch": {"enabled": False, "enable_from": "2026-11-19"}}}
    path = tmp_path / "config.local.yaml"
    path.write_text('sources:\n  twitch:\n    enable_from: "2026-11-19"\n', encoding="utf-8")
    original = path.read_bytes()
    settings.save({**settings.values(cfg), "twitch": True}, cfg, tmp_path)
    assert "2026-11-19" in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.bak"))[0].read_bytes() == original
    overlay = settings.group_overlay("sources", {"twitch": {"enabled": False}}, cfg)
    settings.save_overlay(overlay, tmp_path)
    assert "2026-11-19" in path.read_text(encoding="utf-8")
    overlay = settings.group_overlay("sources", {"twitch": {"enable_from": None}}, cfg)
    settings.save_overlay(overlay, tmp_path)
    assert "enable_from: null" in path.read_text(encoding="utf-8")


def test_state_settings_schedule_and_dry_ticket(tmp_db, isolated_media, fake_clock):
    client = create_app(tmp_db, cfg={"app": {"dry_run": True}}, media_root=isolated_media).test_client()
    state = client.get("/api/state", base_url=BASE).get_json()
    headers = {"X-CSRF-Token": state["csrf"]}
    assert state["status_line"]["dry_run"] is True
    assert state["analytics_warnings"] == []
    assert state["disk"] == {"media_bytes": 0}
    schedule = client.get("/api/schedule?from=2026-11-19&days=1", base_url=BASE).get_json()
    assert len(schedule["slots"]) == 2 and all(s["item_id"] is None for s in schedule["slots"])
    assert client.get("/api/settings", base_url=BASE).get_json()["look"]["versions"] == ["P1", "P2", "P3"]
    assert client.post("/api/prepare", base_url=BASE, headers=headers,
                       json={"action": "go_live"}).status_code == 409
    ticket = client.post("/api/prepare", base_url=BASE, headers=headers,
                         json={"action": "go_dry"}).get_json()["ticket"]
    first = client.post("/api/confirm", base_url=BASE, headers=headers, json={"ticket": ticket})
    replay = client.post("/api/confirm", base_url=BASE, headers=headers, json={"ticket": ticket})
    assert first.get_json() == replay.get_json()
    assert first.get_json()["command"]["status"] == "applied"
    assert len(list(tmp_db.parent.glob("*.bak"))) == 1


def test_three_arm_floor_and_post_resolves_preset(tmp_db, isolated_media, monkeypatch):
    db.insert_discovered_item({"id": "clip", "source": "youtube", "source_id": "clip",
                               "source_url": "https://example.test"}, tmp_db)
    db.transition_stage("clip", "approved", {"approved_versions": '["p1","p2","p3"]'}, tmp_db)
    video = config.media_dir("renders") / "clip_P2.mp4"
    video.write_bytes(b"fixture")
    with db.db(tmp_db) as conn:
        conn.execute("INSERT INTO renders VALUES (?, ?, ?, ?, ?, ?)",
                     ("clip", "p2", "P2", str(video), str(video.with_suffix(".meta.json")), db.now()))
    monkeypatch.setattr(scheduler, "in_week_one", lambda *_: False)
    monkeypatch.setattr(scheduler, "variant_stats",
                        lambda *_: {key: {"n": 3, "score": score}
                                    for key, score in (("A", 10), ("p1", 0.1), ("p2", 0.8), ("p3", 0.2))})
    probabilities, _ = scheduler.variant_probabilities({}, tmp_db, ["p1", "p2", "p3"])
    assert probabilities == pytest.approx({"p1": 0.2, "p2": 0.6, "p3": 0.2})
    item = db.get_item("clip", tmp_db)
    assert posting.video_for_post(item, {"variant": "p2"}, tmp_db) == video


def test_go_live_requires_current_meta_check_and_only_writes_local_overlay(
    tmp_db, isolated_media, fake_clock, monkeypatch
):
    import connection_checks

    cfg = {"app": {"dry_run": True}, "posting": {"platforms": {"instagram": True}}}
    env = {"IG_ACCESS_TOKEN": "testing-token", "IG_USER_ID": "testing-user"}
    monkeypatch.setattr(connection_checks, "current_environment", lambda: env.copy())
    monkeypatch.setattr(connection_checks, "run_checks", lambda *_args, **_kwargs: {
        service: {"status": "ok", "checked_at": fake_clock.now.isoformat(), "reason": "fixture"}
        for service in ("instagram_token", "instagram_user")
    })
    client = create_app(tmp_db, cfg=cfg, media_root=isolated_media).test_client()
    csrf = client.get("/api/state", base_url=BASE).get_json()["csrf"]

    def post(url, body):
        return client.post(url, base_url=BASE, headers={"X-CSRF-Token": csrf}, json=body)

    action = {"action": "go_live", "payload": {"dry_run_mode": None}}
    assert post("/api/prepare", action).status_code == 409
    assert post("/api/connections/test", {"service": "all"}).status_code == 200
    ticket = post("/api/prepare", action).get_json()["ticket"]
    env["IG_ACCESS_TOKEN"] = "rotated-testing-token"
    assert post("/api/confirm", {"ticket": ticket}).get_json()["command"]["status"] == "refused"
    assert post("/api/connections/test", {"service": "all"}).status_code == 200
    ticket = post("/api/prepare", action).get_json()["ticket"]
    assert post("/api/confirm", {"ticket": ticket}).get_json()["command"]["status"] == "applied"
    assert client.get("/api/settings", base_url=BASE).get_json()["golive"]["dry_run"] is False
    assert cfg["app"]["dry_run"] is True
