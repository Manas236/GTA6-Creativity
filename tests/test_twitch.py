"""CP4 Twitch discovery through Helix. Fully mocked: the transport is a fake, no network."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import db, discover, rate_limit, twitch


class Resp:
    def __init__(self, payload: Any = None, status_code: int = 200):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> Any:
        return self._payload


class FakeHelix:
    """Token endpoint + /games + /clips, recording every request."""

    def __init__(self, clips: list[dict] | None = None, games: list[dict] | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.clips = clips if clips is not None else []
        self.games = games if games is not None else [{"id": "515025", "name": "Grand Theft Auto VI"}]
        self.tokens_issued = 0
        self.status: dict[str, list[int]] = {}      # path fragment -> queued status codes

    def __call__(self, method: str, url: str, **kwargs: Any) -> Resp:
        self.calls.append((method, url, kwargs))
        for fragment, queue in self.status.items():
            if fragment in url and queue:
                return Resp({"message": "error"}, queue.pop(0))
        if url == twitch.TOKEN_URL:
            self.tokens_issued += 1
            return Resp({"access_token": f"app-token-{self.tokens_issued}", "expires_in": 5000})
        if url.endswith("/games"):
            return Resp({"data": self.games})
        if url.endswith("/clips"):
            return Resp({"data": self.clips})
        return Resp({"message": "not found"}, 404)

    def of(self, fragment: str) -> list[tuple[str, str, dict]]:
        return [c for c in self.calls if fragment in c[1]]


def clip(clip_id: str, views: int, created_at: str, name: str = "Streamer", language: str = "en") -> dict:
    return {
        "id": clip_id, "url": f"https://clips.twitch.tv/{clip_id}", "broadcaster_id": "b1",
        "broadcaster_name": name, "creator_name": "some_viewer", "game_id": "515025", "language": language,
        "title": f"clip {clip_id}", "view_count": views, "created_at": created_at, "duration": 28.5,
    }


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "sources": {
            "youtube": {"enabled": False},
            "twitch": {"enabled": False, "enable_from": "2026-11-19", "game_name": "Grand Theft Auto VI",
                       "weight": 1.10, "limit": 20, "lookback_hours": 48, "languages": ["en"]},
        },
        "discovery": {"min_age_hours": 0.5, "half_life_hours": 36.0, "velocity_power": 1.2},
        "telegram": {"enabled": False},
    }


@pytest.fixture
def creds(monkeypatch):
    values = {"TWITCH_CLIENT_ID": "client-abc", "TWITCH_CLIENT_SECRET": "secret-xyz"}
    monkeypatch.setattr(twitch.config, "secret", lambda key, default="": values.get(key, default))
    twitch._TOKENS.clear()
    yield values
    twitch._TOKENS.clear()


@pytest.fixture
def no_creds(monkeypatch):
    monkeypatch.setattr(twitch.config, "secret", lambda key, default="": "")


# ---------------------------------------------------------------------------
# Auto-enable
# ---------------------------------------------------------------------------

def test_twitch_switches_itself_on_from_the_launch_date(cfg, fake_clock):
    fake_clock.set("2026-11-18T23:59:59Z")
    on, why = twitch.is_enabled(cfg)
    assert on is False and "2026-11-19" in why
    fake_clock.set("2026-11-19T00:00:00Z")
    assert twitch.is_enabled(cfg)[0] is True
    fake_clock.set("2027-03-01T00:00:00Z")
    assert twitch.is_enabled(cfg)[0] is True


def test_enabled_true_forces_it_on_and_no_date_keeps_it_off(cfg, fake_clock):
    fake_clock.set("2026-11-10T00:00:00Z")
    cfg["sources"]["twitch"]["enabled"] = True
    assert twitch.is_enabled(cfg)[0] is True
    cfg["sources"]["twitch"].update(enabled=False, enable_from=None)
    fake_clock.set("2027-01-01T00:00:00Z")
    assert twitch.is_enabled(cfg)[0] is False
    cfg["sources"]["twitch"]["enable_from"] = "19 Nov"
    assert twitch.is_enabled(cfg) == (False, "sources.twitch.enable_from '19 Nov' is not a YYYY-MM-DD date")


def test_the_shipped_config_auto_enables_twitch_on_19_nov_2026(test_config, fake_clock):
    tw = test_config["sources"]["twitch"]
    assert tw["enabled"] is False and str(tw["enable_from"]) == "2026-11-19"
    fake_clock.set("2026-11-18T12:00:00Z")
    assert twitch.is_enabled(test_config)[0] is False
    fake_clock.set("2026-11-19T12:00:00Z")
    assert twitch.is_enabled(test_config)[0] is True


def test_before_the_date_nothing_is_requested(tmp_db, cfg, creds, fake_clock):
    helix = FakeHelix([clip("c1", 100, "2026-11-10T00:00:00Z")])
    fake_clock.set("2026-11-18T12:00:00Z")
    assert twitch.discover_twitch(cfg, tmp_db, transport=helix) == []
    assert helix.calls == []


# ---------------------------------------------------------------------------
# Helix
# ---------------------------------------------------------------------------

def test_clips_for_the_gta_vi_game_id_become_candidates(tmp_db, cfg, creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([
        clip("FastClip", 9000, "2026-11-20T06:00:00Z", name="Shroud"),      # 6 h old
        clip("OldClip", 9000, "2026-11-19T00:00:00Z"),                       # 36 h old
        clip("Spanish", 50000, "2026-11-20T06:00:00Z", language="es"),       # filtered: not in languages
    ])

    out = twitch.discover_twitch(cfg, tmp_db, transport=helix)

    assert [c["source_id"] for c in out] == ["FastClip", "OldClip"]
    fast = out[0]
    assert fast["source"] == "twitch" and fast["source_url"] == "https://clips.twitch.tv/FastClip"
    assert fast["author_name"] == "Shroud"                                   # the broadcaster, not the clipper
    assert fast["raw_velocity"] == 1500.0 and fast["age_hours"] == 6.0       # 9000 views / 6 h
    assert fast["effective_weight"] == 1.10 and fast["original_duration"] == 28.5
    assert fast["upload_time"] == "2026-11-20T06:00:00Z" and fast["id"].startswith("item_")

    # token -> game lookup -> clips, with the app token and client id on the Helix calls
    assert [c[1].rsplit("/", 1)[-1] for c in helix.calls] == ["token", "games", "clips"]
    assert helix.calls[0][2]["data"]["grant_type"] == "client_credentials"
    _, _, kwargs = helix.of("/clips")[0]
    assert kwargs["headers"] == {"Client-Id": "client-abc", "Authorization": "Bearer app-token-1"}
    assert kwargs["params"] == {"game_id": "515025", "started_at": "2026-11-18T12:00:00Z",
                                "ended_at": "2026-11-20T12:00:00Z", "first": 20}
    assert helix.of("/games")[0][2]["params"] == {"name": "Grand Theft Auto VI"}


def test_the_game_id_and_the_token_are_cached(tmp_db, cfg, creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([clip("c1", 10, "2026-11-20T06:00:00Z")])
    twitch.discover_twitch(cfg, tmp_db, transport=helix)
    twitch.discover_twitch(cfg, tmp_db, transport=helix)
    assert helix.tokens_issued == 1 and len(helix.of("/games")) == 1 and len(helix.of("/clips")) == 2
    assert db.kv_get(twitch.K_GAME_ID, db_path=tmp_db) == "Grand Theft Auto VI|515025"
    with db.db(tmp_db) as conn:                                              # the token is never stored
        assert not conn.execute("SELECT 1 FROM kv WHERE value LIKE '%app-token%'").fetchone()

    cfg["sources"]["twitch"]["game_id"] = "999"                              # a pinned id skips the lookup
    twitch.discover_twitch(cfg, tmp_db, transport=helix)
    assert helix.of("/clips")[-1][2]["params"]["game_id"] == "999" and len(helix.of("/games")) == 1


def test_an_expired_token_is_refreshed_once(tmp_db, cfg, creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([clip("c1", 10, "2026-11-20T06:00:00Z")])
    helix.status["/games"] = [401]
    assert len(twitch.discover_twitch(cfg, tmp_db, transport=helix)) == 1
    assert helix.tokens_issued == 2
    fake_clock.advance(hours=2)                                              # past expires_in: a new token
    twitch.discover_twitch(cfg, tmp_db, transport=helix)
    assert helix.tokens_issued == 3


# ---------------------------------------------------------------------------
# Failure policy
# ---------------------------------------------------------------------------

def test_missing_credentials_skip_twitch_with_exactly_one_alert_and_no_crash(tmp_db, cfg, no_creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([clip("c1", 10, "2026-11-20T06:00:00Z")])
    alerts: list[tuple[str, str]] = []

    for _ in range(3):
        assert twitch.discover_twitch(cfg, tmp_db, transport=helix, alert=lambda t, k: alerts.append((t, k))) == []
    assert helix.calls == []
    assert {k for _, k in alerts} == {twitch.ALERT_NO_CREDENTIALS} and "TWITCH_CLIENT_ID" in alerts[0][0]

    # Through the real alert path the same key is delivered once, ever.
    from pipeline import telegram_bot
    sent: list[str] = []

    class Bot(telegram_bot.ApprovalBot):
        def send_message(self, text, **extra):
            sent.append(text)
            return {"message_id": 1}

    bot = Bot(transport=lambda *a, **k: True, chat_id="1", cfg=cfg, db_path=tmp_db)
    for _ in range(3):
        fake_clock.advance(days=1)
        telegram_bot.send_alert("twitch creds missing", key=twitch.ALERT_NO_CREDENTIALS, cfg=cfg,
                                db_path=tmp_db, bot=bot, repeat_gap_s=None)
    assert len(sent) == 1


def test_missing_twitch_credentials_never_stop_discovery(tmp_db, cfg, no_creds, fake_clock, monkeypatch):
    fake_clock.set("2026-11-20T12:00:00Z")
    yt = {"id": "item_yt", "source": "youtube", "source_id": "yt1", "source_url": "https://youtu.be/yt1",
          "author_name": "Creator", "raw_velocity": 10.0, "age_hours": 5.0, "effective_weight": 1.0}
    monkeypatch.setattr(discover, "discover_youtube", lambda cfg, db_path: [dict(yt)])
    out = discover.run_discovery(cfg, tmp_db)                                # must not raise
    assert [i["source_id"] for i in out] == ["yt1"]


def test_a_429_pauses_twitch_and_other_errors_just_return_nothing(tmp_db, cfg, creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([clip("c1", 10, "2026-11-20T06:00:00Z")])
    helix.status["/clips"] = [500]
    assert twitch.discover_twitch(cfg, tmp_db, transport=helix) == []
    assert rate_limit.is_source_paused("twitch", tmp_db)[0] is False

    helix.status["/clips"] = [429]
    assert twitch.discover_twitch(cfg, tmp_db, transport=helix) == []
    assert rate_limit.is_source_paused("twitch", tmp_db)[0] is True
    before = len(helix.calls)
    assert twitch.discover_twitch(cfg, tmp_db, transport=helix) == []        # paused: no request
    assert len(helix.calls) == before

    fake_clock.advance(hours=1, minutes=1)                                   # the pause lifts
    assert len(twitch.discover_twitch(cfg, tmp_db, transport=helix)) == 1


def test_a_bad_secret_or_an_unknown_game_returns_nothing_and_does_not_raise(tmp_db, cfg, creds, fake_clock):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([clip("c1", 10, "2026-11-20T06:00:00Z")])
    helix.status["/token"] = [400]
    assert twitch.discover_twitch(cfg, tmp_db, transport=helix) == []

    alerts: list = []
    nogame = FakeHelix([], games=[])
    assert twitch.discover_twitch(cfg, tmp_db, transport=nogame, alert=lambda t, k: alerts.append(k)) == []
    assert alerts == ["twitch_no_game:Grand Theft Auto VI"] and nogame.of("/clips") == []


# ---------------------------------------------------------------------------
# Same scoring / insert path as YouTube
# ---------------------------------------------------------------------------

def test_twitch_clips_are_scored_and_inserted_by_the_same_discovery_path(tmp_db, cfg, creds, fake_clock, monkeypatch):
    fake_clock.set("2026-11-20T12:00:00Z")
    helix = FakeHelix([
        clip("Hot", 12000, "2026-11-20T06:00:00Z"),      # 2000 views/h
        clip("Mild", 600, "2026-11-20T06:00:00Z"),       # 100 views/h
    ])
    yt = {"id": "item_yt", "source": "youtube", "source_id": "yt1", "source_url": "https://youtu.be/yt1",
          "author_name": "Creator", "raw_velocity": 1_000_000.0, "age_hours": 6.0, "effective_weight": 1.0}
    monkeypatch.setattr(discover, "discover_youtube", lambda cfg, db_path: [dict(yt)])
    monkeypatch.setattr(twitch, "discover_twitch",
                        lambda c, d, _orig=twitch.discover_twitch: _orig(c, d, transport=helix))

    scored = {i["source_id"]: i for i in discover.run_discovery(cfg, tmp_db)}

    # Ranked within Twitch's own pool: the huge YouTube velocity does not push Twitch clips down.
    assert scored["Hot"]["percentile_rank"] == 1.0 and scored["Mild"]["percentile_rank"] == 0.5
    decay = 0.5 ** (6.0 / 36.0)
    assert scored["Hot"]["discovery_score"] == pytest.approx((1.0 ** 1.2) * decay * 1.10)
    assert scored["Mild"]["discovery_score"] == pytest.approx((0.5 ** 1.2) * decay * 1.10)

    rows = {r["source_id"]: r for r in db.get_items_by_stage("discovered", db_path=tmp_db)}
    assert set(rows) == {"Hot", "Mild", "yt1"}
    hot = rows["Hot"]
    assert hot["source"] == "twitch" and hot["source_url"] == "https://clips.twitch.tv/Hot"
    assert hot["stage"] == "discovered" and hot["view_count"] == 12000

    # Re-discovering the same clip updates it instead of inserting a duplicate (UNIQUE source+id).
    discover.run_discovery(cfg, tmp_db)
    assert len(db.get_items_by_stage("discovered", db_path=tmp_db)) == 3


def test_a_twitch_item_downloads_through_ytdlp_with_the_capped_selector(tmp_db, cfg, synthetic_video, monkeypatch):
    import shutil
    from unittest.mock import MagicMock

    from pipeline import config as pconfig, download

    db.insert_discovered_item({"id": "tw_item", "source": "twitch", "source_id": "Hot",
                               "source_url": "https://clips.twitch.tv/Hot", "author_name": "Shroud"}, db_path=tmp_db)
    seen: dict[str, Any] = {}

    def fake_ydl(opts):
        seen["opts"] = opts
        ydl = MagicMock()

        def dl(urls):
            seen["urls"] = urls
            shutil.copyfile(synthetic_video, pconfig.media_dir("downloads") / "tmp_tw_item.mp4")

        ydl.download.side_effect = dl
        return MagicMock(__enter__=lambda s: ydl, __exit__=lambda *a: None)

    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", fake_ydl)
    ok, _ = download.download_single_item(db.get_item("tw_item", db_path=tmp_db), cfg=cfg, db_path=tmp_db)

    assert ok is True and seen["urls"] == ["https://clips.twitch.tv/Hot"]
    assert seen["opts"]["format"] == download.format_selector(cfg)
    item = db.get_item("tw_item", db_path=tmp_db)
    assert item["stage"] == "downloaded"                 # and it went through dedupe_download on the way
