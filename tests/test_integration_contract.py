"""Exercise the merged dashboard contract against a disposable demo database."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import yaml

import connection_checks

from dashboard import workers
from dashboard.app import create_app
from pipeline import clock, config, creation, db, plan, posting, runner, scheduler
from scripts.dashboard_demo_db import build_demo


@pytest.fixture
def demo(tmp_path):
    path, media = build_demo(tmp_path / "demo.db", media=False)
    cfg = config.get_config()
    assert cfg["app"]["dry_run"] is True
    return path, media, cfg


def connect(demo):
    path, media, cfg = demo
    client = create_app(path, cfg=cfg, media_root=media).test_client()
    csrf = client.get("/api/state").get_json()["csrf"]

    def post(url, body):
        return client.post(url, json=body, headers={"X-CSRF-Token": csrf})

    def confirm(action, item_id=None, payload=None):
        request = {"action": action, "payload": payload or {}}
        if item_id is not None:
            request["item_id"] = item_id
        response = post("/api/prepare", request)
        assert response.status_code == 200, response.get_json()
        response = post("/api/confirm", {"ticket": response.get_json()["ticket"]})
        assert response.status_code == 202, response.get_json()
        return response.get_json()

    return client, post, confirm


def test_demo_read_contract(demo):
    client, _, _ = connect(demo)
    state = client.get("/api/state").get_json()
    assert {"status_line", "todo", "analytics_warnings", "disk"} <= state.keys()
    assert state["status_line"]["dry_run"] is True
    assert state["disk"]["media_bytes"] > 0
    metric = next(m for m in state["metrics"] if m["views"] > 0)
    assert metric["posted_at"] and metric["platform"] in ("instagram", "facebook")
    review = next(i for i in state["items"] if i["stage"] == "awaiting_approval")
    detail = client.get(f"/api/items/{review['id']}").get_json()
    assert {"script", "voice", "speed"} <= detail["voiceover"].keys()
    assert {v["variant"] for v in detail["versions"]} == {"p1", "p2"}
    version = detail["versions"][0]
    assert version["meta"]["caption_words"][0]["i"] == 0
    assert client.get(version["url"]).status_code == 200
    posted = client.get(f"/api/items/{metric['item_id']}").get_json()["posts"]
    assert all({"metrics", "missed_windows", "remote_url"} <= p.keys() for p in posted)
    assert any(p["metrics"] for p in posted)
    settings = client.get("/api/settings").get_json()
    assert {"posting", "look", "sources", "approval", "voiceover", "retention", "golive"} <= settings.keys()
    slots = client.get("/api/schedule").get_json()["slots"]
    assert any(s["item_id"] is None and s["title"] is None for s in slots)
    assert all({"at", "profile", "item_id", "title"} <= s.keys() for s in slots)
    discovered = client.get("/api/discovered").get_json()
    assert {"item_id", "title", "source", "score", "age_h", "url"} <= discovered[0].keys()
    assert client.get("/api/logs").get_json() == {"lines": []}
    assert {"ids", "page", "pages", "total"} <= client.get("/api/library").get_json().keys()
    assert client.get("/api/connections").status_code == 200


def test_demo_approval_edit_and_undo(demo, monkeypatch):
    path, media, cfg = demo
    client, _, confirm = connect(demo)
    jobs = []
    monkeypatch.setattr(workers, "spawn", lambda *args: jobs.append(args))
    with db.db(path) as conn:
        item_id = conn.execute("SELECT item_id FROM renders ORDER BY item_id LIMIT 1").fetchone()[0]

    single = confirm("approve_versions", item_id, {"variants": ["p2"]})
    assert single["command"]["status"] == "applied"
    item = db.get_item(item_id, path)
    assert (item["stage"], item["chosen_variant"], item["ab_excluded"]) == ("approved", "p2", 1)
    undone = confirm(**single["undo"])
    assert undone["command"]["status"] == "applied"
    assert db.get_item(item_id, path)["stage"] == "awaiting_approval"

    multi = confirm("approve_versions", item_id, {"variants": ["p2", "p1"]})
    assert multi["command"]["status"] == "applied"
    item = db.get_item(item_id, path)
    assert (item["chosen_variant"], item["ab_excluded"]) == (None, 0)
    assert json.loads(item["approved_versions"]) == ["p1", "p2"]
    assert confirm(**multi["undo"])["command"]["status"] == "applied"

    edited = confirm("edit_caption_words", item_id, {"words": [{"i": 3, "text": "STRAUSS"}]})
    monkeypatch.setattr("scripts.dashboard_demo_db.stub_produce", lambda *_: None)
    creation.run_jobs(cfg, path, only=edited["command"]["id"])
    item = client.get(f"/api/items/{item_id}").get_json()["item"]
    assert item["stage"] == "cut"
    assert json.loads(item["caption_words_override"]) == {"3": "STRAUSS"}
    assert jobs == [(path, media, "make", "--job", edited["command"]["id"])]


def test_demo_move_obeys_gap(demo):
    path, _, _ = demo
    client, post, confirm = connect(demo)
    profiles = {name: ["12:00", "13:00", "16:00"] for name in ("local", "server")}
    assert post("/api/settings/posting", {"slots": profiles, "daily_post_target": 3,
                                         "min_gap_hours": 3}).status_code == 200
    state = client.get("/api/state").get_json()
    first, second = [i["id"] for i in state["items"] if i["stage"] == "approved"]
    future = clock.utcnow() + timedelta(days=14)
    _, zone, _ = scheduler.active_profile(demo[2])
    day = future.astimezone(zone).date().isoformat()
    slots = client.get(f"/api/schedule?from={day}&days=1").get_json()["slots"]
    assert len(slots) == 3 and all(s["item_id"] is None for s in slots)
    assert confirm("move_to_slot", first, {"at": slots[0]["at"]})["command"]["status"] == "applied"
    refusal = confirm("move_to_slot", second, {"at": slots[1]["at"]})["command"]
    assert refusal["status"] == "refused" and "posting gap" in refusal["result"]
    assert db.get_item(second, path)["stage"] == "approved"
    assert confirm("move_to_slot", second, {"at": slots[2]["at"]})["command"]["status"] == "applied"
    assert posting.item_slot(posting.get_posts_for_item(second, path)) == slots[2]["at"]


def test_snapshot_and_go_live_guard(demo, monkeypatch):
    path, _, cfg = demo
    # Exercise the actual worker and Meta freshness check on the disposable fixture.
    db.kv_set("dashboard:demo", "", path)
    cfg = config.merge_config(cfg, {"posting": {"meta_min_gap_seconds": 0}})
    client, post, confirm = connect((path, demo[1], cfg))
    queued = []
    requests = []

    def graph_request(self, method, url, **kwargs):
        assert method == "GET" and "insights" in url
        requests.append(url)
        return SimpleNamespace(status_code=200, json=lambda: {
            "data": [{"name": "views", "values": [{"value": 54321}]}],
        })

    monkeypatch.setattr(workers, "spawn", lambda *args: queued.append(args))
    monkeypatch.setattr(posting.RequestsTransport, "__call__", graph_request)
    monkeypatch.setattr(posting, "ig_token", lambda: "fixture")
    monkeypatch.setattr(posting, "fb_token", lambda: "fixture")
    item = next(p["item_id"] for p in client.get("/api/state").get_json()["posts"]
                if p["status"] == "live")
    command = confirm("snapshot_now", item)["command"]
    assert command["status"] == "pending"
    assert queued[0][2:] == ("operation", "--job", command["id"])
    runner.run_dashboard_operations(cfg, path, job_id=command["id"])
    assert requests
    state = client.get("/api/state").get_json()
    result = next(c for c in state["commands"] if c["id"] == command["id"])
    assert result["status"] == "applied", result["result"]
    assert any(m["views"] == 54321 for m in state["metrics"])

    response = post("/api/prepare", {"action": "go_live"})
    assert response.status_code == 409
    assert "successful Meta connection test" in response.get_json()["error"]

    checked_at = clock.utcnow()
    services = ("instagram_token", "instagram_user", "facebook_page")
    monkeypatch.setattr(connection_checks, "current_environment", lambda: {})
    monkeypatch.setattr(connection_checks, "run_checks", lambda *_args, **_kwargs: {
        service: {"status": "ok", "checked_at": clock.iso(checked_at), "reason": "fixture"}
        for service in services
    })
    assert post("/api/connections/test", {"service": "all"}).status_code == 200
    prepared = post("/api/prepare", {"action": "go_live"})
    assert prepared.status_code == 200
    monkeypatch.setattr(clock, "utcnow", lambda: checked_at + timedelta(minutes=16))
    response = post("/api/confirm", {"ticket": prepared.get_json()["ticket"]})
    assert response.status_code == 202
    refused = response.get_json()["command"]
    assert refused["status"] == "refused"
    assert "successful Meta connection test" in refused["result"]
    assert client.get("/api/settings").get_json()["golive"]["dry_run"] is True


def test_music_toggle_controls_p3_plan(demo, tmp_path):
    client, post, _ = connect(demo)
    assert client.get("/api/settings").get_json()["look"]["music"]["enabled"] is True
    track = tmp_path / "fixture.wav"
    track.touch()
    base = config.merge_config(demo[2], {"style": {"music_bed": {"file": str(track)}}})
    analysis = plan.analyze_clip("unused.mp4", cfg=base, preset="P0", duration=3)
    for enabled in (False, True):
        assert post("/api/settings/look", {"music": {"enabled": enabled}}).status_code == 200
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
        assert overlay["style"]["music_bed"]["enabled"] is enabled
        assert client.get("/api/settings").get_json()["look"]["music"]["enabled"] is enabled
        cfg = config.merge_config(base, overlay)
        p3 = plan.build_style_plan(analysis, "VICE CITY", preset="P3", cfg=cfg)
        assert p3.music_file == (str(track) if enabled else None)


@pytest.mark.parametrize("template", ["", " \t\n"])
def test_posting_rejects_blank_caption_template(demo, template):
    client, post, _ = connect(demo)
    assert client.get("/api/settings").get_json()["posting"]["caption_templates"]["youtube"]
    response = post("/api/settings/posting", {"caption_templates": {"youtube": template}})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Caption templates cannot be blank."
    assert not (demo[0].parent / "config.local.yaml").exists()
