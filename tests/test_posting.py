"""Unit tests for CP3 Meta posting: the two-phase state machine, crash recovery, retries and the
dry run. Zero network: the transport is a fake and no real request is ever built."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests

from pipeline import clock, db, posting, rate_limit
from pipeline.errors import RateLimitError

IG_USER = "17841400000000000"
FB_PAGE = "100000000000000"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload: Any = None, status_code: int = 200, text: str = ""):
        self._payload = {} if payload is None else payload
        self.status_code = status_code
        self.text = text or json.dumps(self._payload)

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    """Answers each (method, url-fragment) from a routing table and records every request."""

    def __init__(self, routes: dict[str, Any] | None = None):
        self.routes: dict[str, Any] = routes or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.crash_on: str | None = None   # the first request whose url contains this raises CrashError

    def __call__(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if self.crash_on and self.crash_on in url:
            raise CrashError(f"process died during {url}")
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, list):
                    return answer.pop(0) if len(answer) > 1 else answer[0]
                if callable(answer):
                    return answer(method, url, kwargs)
                return answer
        return FakeResponse({"error": {"message": f"no fake route for {url}", "code": 100}}, 400)

    def urls(self) -> list[str]:
        return [u for _, u, _ in self.calls]

    def sent(self, fragment: str) -> list[tuple[str, str, dict[str, Any]]]:
        return [c for c in self.calls if fragment in c[1]]


class CrashError(RuntimeError):
    """Stands in for the process being killed: it is not one of the errors posting catches."""


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "app": {"dry_run": False},
        "posting": {
            "platforms": {"instagram": True, "facebook": True},
            "meta_min_gap_seconds": 0,
            "graph_base": "https://graph.test",
            "rupload_base": "https://rupload.test",
            "api_version": "v25.0",
            "dry_run_mode": "plan_only",
            "max_retries": 2,
            "retry_backoff_s": 1.0,
            "retry_backoff_cap_s": 8.0,
            "max_post_attempts": 3,
            "rate_limit_pause_hours": 3.0,
            "container_poll_seconds": 0.0,
            "container_poll_max_minutes": 1.0,
            "recent_media_limit": 25,
        },
    }


@pytest.fixture
def secrets(monkeypatch) -> None:
    values = {
        "IG_USER_ID": IG_USER,
        "IG_ACCESS_TOKEN": "ig-token-secret",
        "FB_PAGE_ID": FB_PAGE,
        "FB_PAGE_ACCESS_TOKEN": "fb-page-token-secret",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
    }
    monkeypatch.setattr(posting.config, "secret", lambda key, default="": values.get(key, default))


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 3))
        self.now += seconds


def make_publisher(tmp_db, cfg, transport, alerts=None, clock=None):
    clock = clock or Clock()
    client = posting.MetaClient(
        transport=transport, cfg=cfg, db_path=tmp_db, clock=clock, sleep=clock.sleep
    )
    return posting.Publisher(
        client=client,
        cfg=cfg,
        db_path=tmp_db,
        alert=(lambda text, key=None: alerts.append((text, key))) if alerts is not None else (lambda t, k=None: None),
        clock=clock,
        sleep=clock.sleep,
    ), clock


def make_item(tmp_db: Path, tmp_path: Path, item_id: str = "item-1", variant: str | None = "B") -> dict[str, Any]:
    # variant "B": these tests drive the posting state machine with the variant already decided;
    # the slot-time draw itself is tested in test_scheduler.py.
    db.insert_discovered_item(
        {
            "id": item_id,
            "source": "youtube",
            "source_id": item_id,
            "source_url": f"https://youtu.be/{item_id}",
            "author_name": "GamingBolt",
            "discovery_score": 0.6,
            "raw_velocity": 5.0,
            "percentile_rank": 0.8,
        },
        db_path=tmp_db,
    )
    a = tmp_path / f"{item_id}_A.mp4"
    b = tmp_path / f"{item_id}_B.mp4"
    a.write_bytes(b"A" * 1024)
    b.write_bytes(b"B" * 4096)
    db.transition_stage(
        item_id,
        "approved",
        extra_fields={
            "hook_text": "WHAT IS HIDDEN HERE?",
            "hook_style": "question",
            "authenticity": "unverified",
            "render_a_path": str(a),
            "render_b_path": str(b),
            "chosen_variant": variant,
            "approval_type": "approved",
        },
        db_path=tmp_db,
    )
    return db.get_item(item_id, db_path=tmp_db)


def queue_item(tmp_db, cfg, item: dict[str, Any], slot: str) -> None:
    """What the scheduler does: post rows at `slot`, item moved to `queued`."""
    posting.ensure_post_rows(item, slot_datetime=slot, cfg=cfg, db_path=tmp_db)
    db.transition_stage(item["id"], "queued", db_path=tmp_db)


def happy_routes() -> dict[str, Any]:
    """Every call of both flows answering successfully."""
    return {
        f"/{IG_USER}/media_publish": FakeResponse({"id": "media-999"}),
        f"/{IG_USER}/media": FakeResponse({"id": "container-abc"}),
        "/ig-api-upload/": FakeResponse({"success": True}),
        "/container-abc": FakeResponse({"status_code": "FINISHED"}),
        "/media-999": FakeResponse({"permalink": "https://instagram.com/reel/xyz",
                                    "timestamp": "2026-11-09T10:00:00+0000"}),
        f"/{FB_PAGE}/video_reels": [FakeResponse({"video_id": "fbvid-777"}), FakeResponse({"success": True})],
        "/video-upload/": FakeResponse({"success": True}),
        "/fbvid-777": FakeResponse({"status": {"video_status": "ready"}, "permalink_url": "/reel/777"}),
    }


def ig_only(cfg: dict[str, Any]) -> dict[str, Any]:
    cfg["posting"]["platforms"] = {"instagram": True, "facebook": False}
    return cfg


def post_row(tmp_db, item_id: str, platform: str) -> dict[str, Any]:
    return next(p for p in posting.get_posts_for_item(item_id, db_path=tmp_db) if p["platform"] == platform)


# ---------------------------------------------------------------------------
# Caption template
# ---------------------------------------------------------------------------

def test_caption_template_carries_the_hook_the_creator_credit_and_the_tags():
    caption = posting.build_caption("WHAT IS HIDDEN HERE?", "GamingBolt", "youtube")
    assert caption == (
        "WHAT IS HIDDEN HERE?\n\n"
        "Credit: GamingBolt\n.\n"
        "#gta6 #grandtheftauto #gta #gaming #rockstargames"
    )


def test_twitch_items_get_the_twitch_credit_template():
    caption = posting.build_caption("WHAT IS HIDDEN HERE?", "Shroud Live", "twitch")
    assert caption == (
        "WHAT IS HIDDEN HERE?\n\n"
        "Clipped from Shroud Live on Twitch\ntwitch.tv/shroudlive\n.\n"
        "#gta6 #grandtheftauto #gta #gaming #rockstargames #twitchclips"
    )
    # a display name with no ASCII login gets the credit without a broken link
    assert "twitch.tv/" not in posting.build_caption("HOOK", "配信者", "twitch")


def test_there_is_no_reddit_template_and_templates_can_be_overridden_in_config():
    assert "reddit" not in posting.CAPTION_TEMPLATES
    assert "Found via r/" not in posting.build_caption("HOOK", "u/someone", "reddit")
    cfg = {"posting": {"caption_templates": {"twitch": "{hook_text} / {author_name} / {hashtags}"},
                       "hashtags": "#x"}}
    assert posting.build_caption("HOOK", "Streamer", "twitch", cfg=cfg) == "HOOK / Streamer / #x"


def test_caption_falls_back_to_the_template_when_the_item_has_none():
    built = posting.caption_for_item({"hook_text": "HOOK", "author_name": "Creator", "source": "youtube"})
    assert built.startswith("HOOK\n\nCredit: Creator")
    stored = posting.caption_for_item({"caption_text": "already written", "hook_text": "HOOK"})
    assert stored == "already written"


def test_a_caption_is_capped_at_the_instagram_limit():
    caption = posting.build_caption("X" * 5000, "Creator")
    assert len(caption) == posting.IG_CAPTION_LIMIT


# ---------------------------------------------------------------------------
# The two-phase state machine
# ---------------------------------------------------------------------------

def test_instagram_walks_uploading_container_ready_publishing_live_in_order(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")

    seen: list[str] = []
    real = posting.set_status

    def spy(post_id, status, db_path=None, **fields):
        seen.append(status)
        real(post_id, status, db_path=db_path, **fields)

    posting.set_status = spy
    try:
        assert publisher.publish(row["id"], item) == posting.STATUS_LIVE
    finally:
        posting.set_status = real

    assert seen == ["uploading", "container_ready", "publishing", "live"]
    final = post_row(tmp_db, item["id"], "instagram")
    assert final["remote_container_id"] == "container-abc"
    assert final["remote_id"] == "media-999"
    assert final["remote_url"] == "https://instagram.com/reel/xyz"
    assert final["posted_at"] == "2026-11-09T10:00:00Z"


def test_the_container_id_is_saved_before_the_bytes_are_ever_uploaded(tmp_db, tmp_path, cfg, secrets):
    """Phase 1 must be durable first: a crash during the upload still leaves the id behind."""
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    transport.crash_on = "/ig-api-upload/"
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")

    with pytest.raises(CrashError):
        publisher.publish(row["id"], item)

    crashed = post_row(tmp_db, item["id"], "instagram")
    assert crashed["status"] == posting.STATUS_CONTAINER_READY
    assert crashed["remote_container_id"] == "container-abc"


def test_publishing_is_written_before_the_publish_call_is_issued(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    transport.crash_on = "media_publish"
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")

    with pytest.raises(CrashError):
        publisher.publish(row["id"], item)

    crashed = post_row(tmp_db, item["id"], "instagram")
    assert crashed["status"] == posting.STATUS_PUBLISHING
    assert crashed["publish_started_at"]


def test_facebook_walks_the_video_reels_start_upload_finish_flow(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    cfg["posting"]["platforms"] = {"instagram": False, "facebook": True}
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "facebook")

    assert publisher.publish(row["id"], item) == posting.STATUS_LIVE

    start, finish = [c for c in transport.sent("/video_reels")]
    assert start[2]["data"]["upload_phase"] == "start"
    assert finish[2]["data"]["upload_phase"] == "finish"
    assert finish[2]["data"]["video_state"] == "PUBLISHED"
    assert "Credit: GamingBolt" in finish[2]["data"]["description"]
    upload = transport.sent("/video-upload/")[0]
    assert upload[2]["headers"]["file_size"] == "4096"      # variant B's bytes
    assert upload[2]["headers"]["Authorization"] == "OAuth fb-page-token-secret"
    final = post_row(tmp_db, item["id"], "facebook")
    assert final["remote_id"] == "fbvid-777"
    assert final["remote_url"] == "https://www.facebook.com/reel/777"


def test_variant_a_only_items_upload_the_a_render(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path, variant="A")
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item)

    assert transport.sent("/ig-api-upload/")[0][2]["headers"]["file_size"] == "1024"   # variant A's bytes


def test_the_item_becomes_posted_only_when_both_platform_rows_are_live(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    results = posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)

    assert results == {"instagram": "live", "facebook": "live"}
    assert db.get_item(item["id"], db_path=tmp_db)["stage"] == "posted"


def test_an_item_is_not_posted_while_one_platform_failed(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{FB_PAGE}/video_reels"] = FakeResponse({"error": {"message": "Page is restricted", "code": 200}}, 403)
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes))

    results = posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)

    assert results["instagram"] == "live"
    assert results["facebook"] == "failed"
    assert db.get_item(item["id"], db_path=tmp_db)["stage"] == "approved"   # not 'posted'


# ---------------------------------------------------------------------------
# Crash at every status, and resume without a duplicate publish
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "crash_at, expect_status, expect_container",
    [
        (f"/{IG_USER}/media", posting.STATUS_UPLOADING, None),       # died creating the container
        ("/ig-api-upload/", posting.STATUS_CONTAINER_READY, "container-abc"),  # died uploading the bytes
        ("/container-abc", posting.STATUS_CONTAINER_READY, "container-abc"),   # died polling the container
        ("media_publish", posting.STATUS_PUBLISHING, "container-abc"),         # died publishing
    ],
)
def test_a_crash_at_each_status_leaves_a_resumable_row(tmp_db, tmp_path, cfg, secrets, crash_at, expect_status, expect_container):
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    transport.crash_on = crash_at
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")

    with pytest.raises(CrashError):
        publisher.publish(row["id"], item)

    crashed = post_row(tmp_db, item["id"], "instagram")
    assert crashed["status"] == expect_status
    assert crashed["remote_container_id"] == expect_container


def test_a_crash_before_publishing_resumes_and_publishes_exactly_once(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    transport.crash_on = "/ig-api-upload/"
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row_id = post_row(tmp_db, item["id"], "instagram")["id"]
    with pytest.raises(CrashError):
        publisher.publish(row_id, item)

    transport.crash_on = None                                 # the process comes back
    assert publisher.publish(row_id, item) == posting.STATUS_LIVE

    assert len(transport.sent("media_publish")) == 1           # published once, and only once
    assert len(transport.sent(f"/{IG_USER}/media,")) == 0
    # the container was created once; the second run reused the saved id
    creates = [c for c in transport.sent(f"/{IG_USER}/media") if "media_publish" not in c[1]]
    assert len(creates) == 1


def test_resuming_in_publishing_marks_live_when_the_remote_shows_it_published(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes["/container-abc"] = FakeResponse({"status_code": "PUBLISHED"})
    routes[f"/{IG_USER}/media"] = FakeResponse({
        "data": [{"id": "media-555", "caption": "WHAT IS HIDDEN HERE?\n\nCredit: GamingBolt\n.\n#gta6",
                  "permalink": "https://instagram.com/reel/555", "timestamp": "2026-11-09T12:00:00+0000"}]
    })
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="container-abc", publish_started_at="2026-11-09T11:00:00Z")

    assert publisher.publish(row["id"], item) == posting.STATUS_LIVE

    assert transport.sent("media_publish") == []               # NOT published again
    final = post_row(tmp_db, item["id"], "instagram")
    assert final["remote_id"] == "media-555"
    assert final["remote_url"] == "https://instagram.com/reel/555"
    assert final["posted_at"] == "2026-11-09T12:00:00Z"


def test_resuming_in_publishing_with_a_dead_container_restarts_and_publishes_once(tmp_db, tmp_path, cfg, secrets):
    """ERROR / EXPIRED is positive proof nothing published, so a fresh container is safe."""
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes["/container-abc"] = FakeResponse({"status_code": "EXPIRED", "status": "container expired"})
    routes[f"/{IG_USER}/media"] = [FakeResponse({"id": "container-new"})]
    routes["/container-new"] = FakeResponse({"status_code": "FINISHED"})
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="container-abc", publish_started_at=db.now())

    assert publisher.publish(row["id"], item) == posting.STATUS_LIVE

    final = post_row(tmp_db, item["id"], "instagram")
    assert final["remote_container_id"] == "container-new"
    assert len(transport.sent("media_publish")) == 1


def test_an_unknown_remote_state_is_marked_failed_and_alerted_and_never_published(tmp_db, tmp_path, cfg, secrets):
    """A FINISHED container with no matching media cannot prove a publish did not land."""
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes["/container-abc"] = FakeResponse({"status_code": "FINISHED"})
    routes[f"/{IG_USER}/media"] = FakeResponse({"data": []})     # nothing on the account
    transport = FakeTransport(routes)
    alerts: list[tuple[str, str | None]] = []
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport, alerts)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="container-abc", publish_started_at=db.now())

    assert publisher.publish(row["id"], item) == posting.STATUS_FAILED

    assert transport.sent("media_publish") == []               # never re-published
    final = post_row(tmp_db, item["id"], "instagram")
    assert "unknown" in final["error_message"]
    assert len(alerts) == 1 and "failed" in alerts[0][0]


def test_an_unreadable_container_is_also_unknown_and_never_published(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes["/container-abc"] = FakeResponse({"error": {"message": "temporarily unavailable", "code": 2}}, 400)
    transport = FakeTransport(routes)
    alerts: list[tuple[str, str | None]] = []
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport, alerts)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="container-abc", publish_started_at=db.now())

    assert publisher.publish(row["id"], item) == posting.STATUS_FAILED
    assert transport.sent("media_publish") == []
    assert "could not be read" in post_row(tmp_db, item["id"], "instagram")["error_message"]


def test_a_facebook_reel_that_is_already_published_is_recovered_not_re_finished(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    cfg["posting"]["platforms"] = {"instagram": False, "facebook": True}
    routes = happy_routes()
    routes["/fbvid-777"] = FakeResponse({
        "status": {"video_status": "ready", "publishing_phase": {"publish_status": "published"}},
        "permalink_url": "/reel/777",
    })
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "facebook")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="fbvid-777", publish_started_at=db.now())

    assert publisher.publish(row["id"], item) == posting.STATUS_LIVE

    finishes = [c for c in transport.sent("/video_reels") if c[2].get("data", {}).get("upload_phase") == "finish"]
    assert finishes == []
    assert post_row(tmp_db, item["id"], "facebook")["remote_url"] == "https://www.facebook.com/reel/777"


def test_a_facebook_reel_in_an_in_between_state_is_unknown_and_never_re_finished(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    cfg["posting"]["platforms"] = {"instagram": False, "facebook": True}
    routes = happy_routes()
    routes["/fbvid-777"] = FakeResponse({"status": {"video_status": "processing"}})
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "facebook")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="fbvid-777", publish_started_at=db.now())

    assert publisher.publish(row["id"], item) == posting.STATUS_FAILED
    finishes = [c for c in transport.sent("/video_reels") if c[2].get("data", {}).get("upload_phase") == "finish"]
    assert finishes == []


def test_settle_resolves_every_row_left_in_publishing_on_startup(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes["/container-abc"] = FakeResponse({"status_code": "PUBLISHED"})
    routes[f"/{IG_USER}/media"] = FakeResponse({"data": [
        {"id": "media-555", "caption": "WHAT IS HIDDEN HERE?\n\nCredit: GamingBolt\n.\n#gta6",
         "permalink": "https://instagram.com/reel/555", "timestamp": "2026-11-09T12:00:00+0000"}]})
    routes["/fbvid-777"] = FakeResponse({"status": {"video_status": "processing"}})
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    for platform, remote in (("instagram", "container-abc"), ("facebook", "fbvid-777")):
        posting.set_status(post_row(tmp_db, item["id"], platform)["id"], posting.STATUS_PUBLISHING,
                           db_path=tmp_db, remote_container_id=remote, publish_started_at="2026-11-09T11:00:00Z")

    settled = dict((pid, status) for pid, status in posting.settle_publishing_rows(
        cfg=cfg, db_path=tmp_db, publisher=publisher))

    assert sorted(settled.values()) == ["failed", "live"]
    assert transport.sent("media_publish") == []


# ---------------------------------------------------------------------------
# Partial platform retry
# ---------------------------------------------------------------------------

def test_a_failed_facebook_row_retries_facebook_only(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{FB_PAGE}/video_reels"] = FakeResponse({"error": {"message": "transient", "code": 1}}, 400)
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    first = posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)
    assert first == {"instagram": "live", "facebook": "failed"}
    ig_calls_before = len(transport.sent(f"/{IG_USER}/"))

    # Facebook is fixed; the retry pass must leave the live Instagram row alone.
    transport.routes[f"/{FB_PAGE}/video_reels"] = [
        FakeResponse({"video_id": "fbvid-777"}), FakeResponse({"success": True})
    ]
    second = posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)

    assert second == {"instagram": "live", "facebook": "live"}
    assert len(transport.sent(f"/{IG_USER}/")) == ig_calls_before   # no Instagram request at all
    assert db.get_item(item["id"], db_path=tmp_db)["stage"] == "posted"


def test_a_row_is_given_up_on_after_the_attempt_cap(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{FB_PAGE}/video_reels"] = FakeResponse({"error": {"message": "broken", "code": 1}}, 400)
    cfg["posting"]["platforms"] = {"instagram": False, "facebook": True}
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes))
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row_id = post_row(tmp_db, item["id"], "facebook")["id"]

    for _ in range(cfg["posting"]["max_post_attempts"] + 1):
        publisher.publish(row_id, item)

    final = post_row(tmp_db, item["id"], "facebook")
    assert final["status"] == posting.STATUS_FAILED
    assert "gave up after 3 attempts" in final["error_message"]


# ---------------------------------------------------------------------------
# Rate limits, backoff and retries
# ---------------------------------------------------------------------------

def test_a_429_pauses_meta_for_hours_and_leaves_the_row_alone(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{IG_USER}/media"] = FakeResponse(
        {"error": {"message": "Application request limit reached", "code": 4}}, 429
    )
    alerts: list[tuple[str, str | None]] = []
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), FakeTransport(routes), alerts)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row_id = post_row(tmp_db, item["id"], "instagram")["id"]

    with pytest.raises(RateLimitError):
        publisher.publish(row_id, item)

    paused, why = posting.meta_paused(tmp_db)
    assert paused is True
    assert "429" in why
    with db.db(tmp_db) as conn:
        remaining = conn.execute(
            "SELECT paused_until FROM rate_limits WHERE source = 'meta_graph'"
        ).fetchone()["paused_until"]
    assert remaining > db.now()           # the pause reaches into the future, measured in hours
    assert post_row(tmp_db, item["id"], "instagram")["status"] != posting.STATUS_FAILED
    assert alerts and "Meta paused" in alerts[0][0]


def test_a_block_code_without_a_429_also_pauses_meta(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{IG_USER}/media"] = FakeResponse({"error": {"message": "User request limit", "code": 17}}, 400)
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), FakeTransport(routes))
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    with pytest.raises(RateLimitError):
        publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item)

    assert posting.meta_paused(tmp_db)[0] is True


def test_nothing_is_posted_while_meta_is_paused(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    rate_limit.record_error(posting.META_SOURCE, "429 earlier", pause_hours=3.0, db_path=tmp_db)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}
    assert transport.calls == []
    assert db.get_item(item["id"], db_path=tmp_db)["stage"] == "approved"


def test_a_5xx_is_retried_with_capped_exponential_backoff(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    routes = happy_routes()
    routes[f"/{IG_USER}/media"] = [
        FakeResponse({}, 503), FakeResponse({}, 503), FakeResponse({"id": "container-abc"})
    ]
    transport = FakeTransport(routes)
    publisher, clock = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    assert publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item) == posting.STATUS_LIVE
    assert clock.slept[:2] == [1.0, 2.0]          # retry_backoff_s doubling


def test_a_network_error_is_retried_and_then_fails_the_row_with_a_redacted_message(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)

    def always_broken(method, url, kwargs):
        raise requests.ConnectionError(f"could not reach {url}")

    routes = happy_routes()
    routes[f"/{IG_USER}/media"] = always_broken
    publisher, clock = make_publisher(tmp_db, ig_only(cfg), FakeTransport(routes))
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    assert publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item) == posting.STATUS_FAILED
    assert clock.slept == [1.0, 2.0]              # max_retries = 2, then it gives up
    assert "ig-token-secret" not in post_row(tmp_db, item["id"], "instagram")["error_message"]


def test_the_request_pacing_keeps_meta_calls_a_gap_apart(tmp_db, tmp_path, cfg, secrets):
    cfg["posting"]["meta_min_gap_seconds"] = 3
    clock = Clock()
    client = posting.MetaClient(
        transport=FakeTransport({"": FakeResponse({"ok": True})}), cfg=cfg, db_path=tmp_db,
        clock=clock, sleep=clock.sleep,
    )
    client.get("https://graph.test/a")      # nothing before it
    client.post("https://graph.test/b")     # right behind it: a full gap
    clock.now += 1
    client.get("https://graph.test/c")      # only the missing two seconds
    assert clock.slept == [3.0, 2.0]


def test_a_token_is_never_written_into_an_error_message(tmp_db, tmp_path, cfg, secrets):
    assert posting.redact("https://graph.test/x?access_token=ig-token-secret&y=1") == (
        "https://graph.test/x?access_token=***&y=1"
    )
    assert "ig-token-secret" not in posting.redact("OAuth ig-token-secret failed")


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def test_a_dry_run_never_calls_a_publish_endpoint_and_logs_what_it_would_send(tmp_db, tmp_path, cfg, secrets):
    cfg["app"]["dry_run"] = True
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    results = posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)

    assert results == {"instagram": "dry_run", "facebook": "dry_run"}
    assert transport.calls == []            # plan_only: not one request, of any kind
    # a dry run must not advance the item: it still has to post for real once dry_run is off
    assert db.get_item(item["id"], db_path=tmp_db)["stage"] == "approved"
    for platform in ("instagram", "facebook"):
        row = post_row(tmp_db, item["id"], platform)
        assert row["status"] == posting.STATUS_DRY_RUN
        plan_path = row["error_message"].split("plan: ")[-1]
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        assert plan["dry_run"] is True
        urls = [s["url"] for s in plan["steps"]]
        assert any("rupload.test" in u for u in urls)                  # the upload it would do
        assert json.dumps(plan)  # serialisable
        assert "ig-token-secret" not in json.dumps(plan)               # and token-free
        assert "fb-page-token-secret" not in json.dumps(plan)
    ig_plan = json.loads(Path(
        post_row(tmp_db, item["id"], "instagram")["error_message"].split("plan: ")[-1]
    ).read_text(encoding="utf-8"))
    assert any("media_publish" in s["url"] for s in ig_plan["steps"])  # planned, never sent


def test_a_dry_run_still_walks_the_whole_state_machine(tmp_db, tmp_path, cfg, secrets):
    cfg["app"]["dry_run"] = True
    item = make_item(tmp_db, tmp_path)
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), FakeTransport(happy_routes()))
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")

    seen: list[str] = []
    real = posting.set_status

    def spy(post_id, status, db_path=None, **fields):
        seen.append(status)
        real(post_id, status, db_path=db_path, **fields)

    posting.set_status = spy
    try:
        publisher.publish(row["id"], item)
    finally:
        posting.set_status = real

    assert seen == ["uploading", "container_ready", "publishing", "dry_run"]


def test_up_to_publish_mode_uploads_but_still_never_publishes(tmp_db, tmp_path, cfg, secrets):
    cfg["app"]["dry_run"] = True
    cfg["posting"]["dry_run_mode"] = "up_to_publish"
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    assert publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item) == posting.STATUS_DRY_RUN

    assert len(transport.sent("/ig-api-upload/")) == 1      # the bytes really went up
    assert transport.sent("media_publish") == []            # the publish call did not


def test_a_dry_run_row_left_in_publishing_is_settled_without_any_remote_call(tmp_db, tmp_path, cfg, secrets):
    cfg["app"]["dry_run"] = True
    item = make_item(tmp_db, tmp_path)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), transport)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    row = post_row(tmp_db, item["id"], "instagram")
    posting.set_status(row["id"], posting.STATUS_PUBLISHING, db_path=tmp_db,
                       remote_container_id="<dry-run-container:1>")

    assert publisher.settle(post_row(tmp_db, item["id"], "instagram")) == posting.STATUS_DRY_RUN
    assert transport.calls == []


# ---------------------------------------------------------------------------
# posts rows
# ---------------------------------------------------------------------------

def test_post_rows_are_created_once_per_platform_and_carry_the_learning_fields(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    posting.ensure_post_rows(item, slot_datetime="2026-11-10T13:00:00Z", cfg=cfg, db_path=tmp_db)
    posting.ensure_post_rows(item, slot_datetime="2026-11-10T18:00:00Z", cfg=cfg, db_path=tmp_db)

    rows = posting.get_posts_for_item(item["id"], db_path=tmp_db)
    assert [r["platform"] for r in rows] == ["facebook", "instagram"]
    assert all(r["slot_datetime"] == "2026-11-10T13:00:00Z" for r in rows)   # not recreated
    assert all(r["hook_style"] == "question" and r["source"] == "youtube" for r in rows)
    assert all(r["variant"] == "B" for r in rows)                            # default when unassigned
    assert all("Credit: GamingBolt" in r["caption_text"] for r in rows)


def test_only_enabled_platforms_get_a_row(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    posting.ensure_post_rows(item, cfg=ig_only(cfg), db_path=tmp_db)
    assert [r["platform"] for r in posting.get_posts_for_item(item["id"], db_path=tmp_db)] == ["instagram"]


def test_a_missing_render_file_fails_the_row_rather_than_uploading_nothing(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    Path(item["render_b_path"]).unlink()
    publisher, _ = make_publisher(tmp_db, ig_only(cfg), FakeTransport(happy_routes()))
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)

    assert publisher.publish(post_row(tmp_db, item["id"], "instagram")["id"], item) == posting.STATUS_FAILED
    assert "missing on disk" in post_row(tmp_db, item["id"], "instagram")["error_message"]


# ---------------------------------------------------------------------------
# The laptop dry-run script's guard
# ---------------------------------------------------------------------------

def _load_cp3_script():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "scripts" / "cp3_dry_run.py"
    spec = importlib.util.spec_from_file_location("cp3_dry_run", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dry_run_script_guard_refuses_every_write_and_publish_endpoint(secrets):
    script = _load_cp3_script()
    guard = script.GuardedTransport()
    guard.inner = lambda method, url, **kw: FakeResponse({"ok": True})

    # read-only GETs pass
    assert guard("GET", "https://graph.test/v25.0/debug_token", params={"input_token": "x"}).json() == {"ok": True}

    forbidden = [
        ("POST", f"https://graph.test/v25.0/{IG_USER}/media_publish", {"data": {"creation_id": "c"}}),
        ("POST", "https://rupload.facebook.com/ig-api-upload/v25.0/c", {}),
        ("POST", f"https://graph.test/v25.0/{FB_PAGE}/video_reels", {"data": {"upload_phase": "finish"}}),
        ("POST", f"https://graph.test/v25.0/{IG_USER}/media", {"data": {"upload_type": "resumable"}}),
        ("GET", f"https://graph.test/v25.0/{IG_USER}/media", {"params": {"upload_type": "resumable"}}),
    ]
    for method, url, kwargs in forbidden:
        with pytest.raises(script.PublishAttempted):
            guard(method, url, **kwargs)
    assert len(guard.gets) == 1   # only the debug_token GET was ever let through


# ---------------------------------------------------------------------------
# The laptop dry-run script's Meta checks: nine permissions, PAGE token, IG linked to the Page
# ---------------------------------------------------------------------------

NINE_SCOPES = [
    "instagram_basic", "instagram_content_publish", "instagram_manage_insights", "business_management",
    "pages_show_list", "pages_read_engagement", "pages_manage_posts", "read_insights",
    "pages_manage_engagement",
]


def _meta_secrets(monkeypatch, fb_token: str = "") -> None:
    values = {"IG_USER_ID": IG_USER, "IG_ACCESS_TOKEN": "page-token-secret", "FB_PAGE_ID": FB_PAGE,
              "FB_PAGE_ACCESS_TOKEN": fb_token}
    monkeypatch.setattr(posting.config, "secret", lambda key, default="": values.get(key, default))


def _meta_routes(scopes: dict[str, list[str]], token_type: str = "PAGE", profile: str | None = FB_PAGE,
                 linked: str | None = IG_USER, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    def debug(method, url, kw):
        info = {"type": token_type, "is_valid": True, "expires_at": 0, "data_access_expires_at": 1_800_000_000,
                "scopes": scopes[kw["params"]["input_token"]]}
        if profile:
            info["profile_id"] = profile
        return FakeResponse({"data": {**info, **(extra or {})}})

    def page(method, url, kw):
        if "instagram_business_account" in kw["params"]["fields"]:
            return FakeResponse({"id": FB_PAGE, **({"instagram_business_account": {"id": linked}} if linked else {})})
        return FakeResponse({"id": FB_PAGE, "name": "Clips Page"})

    return {
        "debug_token": debug,
        f"/{IG_USER}/content_publishing_limit": FakeResponse({"data": [{"quota_usage": 0}]}),
        f"/{IG_USER}": FakeResponse({"id": IG_USER, "username": "clips"}),
        f"/{FB_PAGE}": page,
    }


def _run_meta_checks(tmp_db, cfg, routes):
    script = _load_cp3_script()
    transport = FakeTransport(routes)
    clock = Clock()
    client = posting.MetaClient(transport=transport, cfg=cfg, db_path=tmp_db, clock=clock, sleep=clock.sleep)
    rows = {r["step"]: r for r in script.check_meta_tokens(client)}
    assert all(method == "GET" for method, _, _ in transport.calls)
    return script, rows


def test_the_dry_run_script_requires_exactly_the_nine_documented_permissions():
    script = _load_cp3_script()
    assert sorted(script.REQUIRED_SCOPES) == sorted(NINE_SCOPES)
    assert set(script.IG_SCOPES) | set(script.FB_SCOPES) == set(NINE_SCOPES)
    assert {"read_insights", "pages_manage_engagement", "pages_manage_posts"} <= set(script.FB_SCOPES)


def test_the_dry_run_meta_checks_pass_with_one_page_token_carrying_all_nine(tmp_db, cfg, monkeypatch, capsys):
    _meta_secrets(monkeypatch)
    script, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES}))

    assert list(rows) == ["debug_token", "ig_user", "ig_publishing_limit", "fb_page", "ig_linked_to_page"]
    assert all(r["ok"] for r in rows.values()), rows
    assert rows["debug_token"]["missing"] == []
    assert "type=PAGE" in rows["debug_token"]["detail"] and "expires=never" in rows["debug_token"]["detail"]
    assert "9/9 required permissions" in rows["debug_token"]["detail"]
    assert script.print_summary(list(rows.values())) == 0
    assert "page-token-secret" not in capsys.readouterr().out


def test_the_dry_run_fails_and_lists_every_missing_permission(tmp_db, cfg, monkeypatch, capsys):
    _meta_secrets(monkeypatch)
    old_list = [s for s in NINE_SCOPES if s not in ("read_insights", "pages_manage_engagement")]
    script, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": old_list}))

    token = rows["debug_token"]
    assert token["ok"] is False
    assert token["missing"] == ["read_insights", "pages_manage_engagement"]
    assert "MISSING 2 permission(s): read_insights, pages_manage_engagement" in token["detail"]
    assert script.print_summary(list(rows.values())) == 1
    out = capsys.readouterr().out
    assert "missing read_insights" in out and "missing pages_manage_engagement" in out
    assert "Needs attention: debug_token" in out


def test_the_dry_run_counts_an_answer_with_no_scopes_as_all_nine_missing(tmp_db, cfg, monkeypatch):
    # The old check read `if scopes and s not in scopes`, so an empty scope list passed.
    _meta_secrets(monkeypatch)
    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": []}))
    assert rows["debug_token"]["ok"] is False
    assert sorted(rows["debug_token"]["missing"]) == sorted(NINE_SCOPES)


def test_the_dry_run_fails_a_user_token_even_with_every_permission(tmp_db, cfg, monkeypatch):
    _meta_secrets(monkeypatch)
    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES},
                                                         token_type="USER", profile=None))
    assert rows["debug_token"]["ok"] is False
    assert "USER token, not a PAGE token" in rows["debug_token"]["detail"]
    assert rows["fb_page"]["ok"] is True          # the Page itself is fine; the token is the problem


def test_the_dry_run_fails_an_invalid_token_or_a_token_for_another_page(tmp_db, cfg, monkeypatch):
    _meta_secrets(monkeypatch)
    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES},
                                                         profile="999999999999999"))
    assert rows["debug_token"]["ok"] is False
    assert f"token for Page 999999999999999, not for FB_PAGE_ID {FB_PAGE}" in rows["debug_token"]["detail"]

    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES},
                                                         extra={"is_valid": False}))
    assert rows["debug_token"]["ok"] is False
    assert "not valid" in rows["debug_token"]["detail"]


def test_the_dry_run_fails_when_the_ig_account_is_not_linked_to_the_page(tmp_db, cfg, monkeypatch):
    _meta_secrets(monkeypatch)
    script, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES}, linked=None))
    assert rows["ig_linked_to_page"]["ok"] is False
    assert "has no Instagram account linked" in rows["ig_linked_to_page"]["detail"]
    assert script.print_summary(list(rows.values())) == 1

    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes({"page-token-secret": NINE_SCOPES},
                                                         linked="17841499999999999"))
    assert rows["ig_linked_to_page"]["ok"] is False
    assert f"linked to Instagram account 17841499999999999, but IG_USER_ID is {IG_USER}" in \
        rows["ig_linked_to_page"]["detail"]


def test_the_dry_run_splits_the_nine_permissions_over_two_tokens(tmp_db, cfg, monkeypatch):
    script = _load_cp3_script()
    _meta_secrets(monkeypatch, fb_token="fb-page-token-secret")
    scopes = {"page-token-secret": list(script.IG_SCOPES), "fb-page-token-secret": list(script.FB_SCOPES)}
    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes(scopes))
    assert rows["debug_token_ig"]["ok"] and rows["debug_token_fb"]["ok"], rows
    assert "debug_token" not in rows

    scopes["fb-page-token-secret"] = [s for s in script.FB_SCOPES if s != "read_insights"]
    _, rows = _run_meta_checks(tmp_db, cfg, _meta_routes(scopes))
    assert rows["debug_token_ig"]["ok"] is True
    assert rows["debug_token_fb"]["ok"] is False
    assert rows["debug_token_fb"]["missing"] == ["read_insights"]


# ---------------------------------------------------------------------------
# preview_only: filtered explicitly, and a trigger refusal fails one item cleanly
# ---------------------------------------------------------------------------

def _drop_preview_triggers(tmp_db) -> None:
    with db.db(tmp_db) as conn:
        for name in ("items_preview_only_never_released", "posts_block_preview_only_insert",
                     "posts_block_preview_only_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def test_posting_filters_preview_only_items_without_relying_on_the_trigger(tmp_db, tmp_path, cfg, secrets):
    item = make_item(tmp_db, tmp_path)
    _drop_preview_triggers(tmp_db)                       # the filter must hold on its own
    db.set_preview_only(item["id"], True, db_path=tmp_db)
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}
    with pytest.raises(posting.PublishError, match="preview_only"):
        posting.run_posting_for_item(item["id"], cfg=cfg, db_path=tmp_db, publisher=publisher)
    with pytest.raises(posting.PublishError, match="preview_only"):
        posting.ensure_post_rows(db.get_item(item["id"], db_path=tmp_db), cfg=cfg, db_path=tmp_db)

    assert transport.calls == []
    assert posting.get_posts_for_item(item["id"], db_path=tmp_db) == []


def test_a_trigger_refusal_fails_that_one_item_and_the_next_item_still_posts(tmp_db, tmp_path, cfg, secrets, monkeypatch):
    bad = make_item(tmp_db, tmp_path, "item-bad")
    good = make_item(tmp_db, tmp_path, "item-good")
    ig_only(cfg)
    for it, slot in ((bad, "2026-11-10T07:30:00Z"), (good, "2026-11-10T07:31:00Z")):
        queue_item(tmp_db, cfg, it, slot)
    with db.db(tmp_db) as conn:   # any posts write for item-bad is refused, as the real trigger would
        conn.execute(
            "CREATE TRIGGER refuse_bad BEFORE UPDATE ON posts WHEN NEW.item_id = 'item-bad' "
            "BEGIN SELECT RAISE(ABORT, 'preview_only item cannot be posted'); END"
        )
    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    now = clock.parse("2026-11-10T07:35:00Z")

    out = posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher, now=now, limit=None)

    failed = db.get_item(bad["id"], db_path=tmp_db)
    assert failed["stage"] == "failed" and failed["error_stage"] == "posting"
    assert "refused by the database" in failed["error_message"]
    assert out == {good["id"]: {"instagram": "live"}}
    # the refused item left the queue: a second pass has nothing to trip over
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher, now=now) == {}


def test_the_real_trigger_raises_an_error_posting_recognises(tmp_db, tmp_path):
    import sqlite3
    item = make_item(tmp_db, tmp_path)
    db.transition_stage(item["id"], "cut", db_path=tmp_db)
    db.set_preview_only(item["id"], True, db_path=tmp_db)
    with pytest.raises(sqlite3.IntegrityError) as err:
        db.transition_stage(item["id"], "approved", db_path=tmp_db)
    assert db.is_trigger_refusal(err.value)


def test_a_lost_publish_response_is_never_retried_and_settles_live_without_a_second_publish(tmp_db, tmp_path, cfg, secrets):
    """Meta published, but the response to media_publish was lost (network error). The call is
    not retried in flight, the row stays `publishing`, and the next pass settles it from the
    remote: live, with exactly one media_publish ever sent."""
    ig_only(cfg)
    item = make_item(tmp_db, tmp_path)
    posting.ensure_post_rows(item, cfg=cfg, db_path=tmp_db)
    state = {"published": False}

    def media_publish(method, url, kwargs):
        state["published"] = True                       # Meta did publish...
        raise requests.ConnectionError("connection reset")   # ...but the answer never arrived

    routes = happy_routes()
    routes[f"/{IG_USER}/media_publish"] = media_publish
    routes["/container-abc"] = lambda m, u, k: FakeResponse(
        {"status_code": "PUBLISHED" if state["published"] else "FINISHED"})
    routes[f"/{IG_USER}/media"] = lambda m, u, k: FakeResponse(
        {"id": "container-abc"} if m == "POST" else
        {"data": [{"id": "media-999", "caption": post_row(tmp_db, item["id"], "instagram")["caption_text"],
                   "timestamp": "2099-01-01T00:00:00+0000", "permalink": "https://instagram.com/reel/xyz"}]})
    transport = FakeTransport(routes)
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    row = post_row(tmp_db, item["id"], "instagram")
    assert publisher.publish(row["id"], item) == posting.STATUS_PUBLISHING
    assert len(transport.sent("media_publish")) == 1                 # not retried in flight
    assert post_row(tmp_db, item["id"], "instagram")["status"] == posting.STATUS_PUBLISHING

    assert publisher.publish(row["id"], item) == posting.STATUS_LIVE   # the next pass settles it
    done = post_row(tmp_db, item["id"], "instagram")
    assert done["remote_id"] == "media-999" and len(transport.sent("media_publish")) == 1
