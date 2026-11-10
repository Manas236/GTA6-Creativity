"""Twitch discovery through the official Helix API (CP4, PLAN.md sections 1 and 4).

  POST https://id.twitch.tv/oauth2/token   client_credentials -> app access token
  GET  /helix/games?name=Grand Theft Auto VI                  -> the game id (cached in kv)
  GET  /helix/clips?game_id=..&started_at=..&ended_at=..&first=N   -> top clips by views

Clips become ordinary discovery candidates: the same velocity -> percentile -> decay scoring as
YouTube (in `discover.run_discovery`), downloaded by yt-dlp from the clip URL and deduplicated by
the same perceptual-hash path. Nothing is scraped.

ON/OFF: `sources.twitch.enabled: true` forces it on; otherwise it switches itself on from
`sources.twitch.enable_from` (the GTA VI launch date, compared in UTC).

FAILURE POLICY: missing TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET is not an error - Twitch is skipped
and ONE alert is sent, ever. A 429 pauses the `twitch` source through `rate_limit`. Any other
failure is logged and returns no candidates; YouTube discovery is unaffected.

Network sits behind a transport callable, so the tests are fully mocked.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Callable

import requests

from . import clock, config, db, rate_limit

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_BASE = "https://api.twitch.tv/helix"
SOURCE = "twitch"
K_GAME_ID = "twitch:game_id"                    # kv: "<game name>|<id>"
ALERT_NO_CREDENTIALS = "twitch_missing_credentials"

# App access tokens live in memory only (never in the database): {client_id: (token, expires_epoch)}
_TOKENS: dict[str, tuple[str, float]] = {}


class TwitchError(RuntimeError):
    pass


def _default_transport(method: str, url: str, **kwargs: Any) -> Any:
    return requests.request(method, url, **kwargs)


def twitch_cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return (((cfg or {}).get("sources") or {}).get("twitch") or {})


def is_enabled(cfg: dict[str, Any] | None = None, now: datetime | None = None) -> tuple[bool, str]:
    """(on?, why). `enabled: true` forces it; otherwise on from `enable_from` (a UTC date)."""
    tw = twitch_cfg(cfg)
    if tw.get("enabled") is True:
        return True, "sources.twitch.enabled is true"
    start = tw.get("enable_from")
    if not start:
        return False, "sources.twitch.enabled is false and no enable_from date is set"
    try:
        start_date = start if isinstance(start, date) else date.fromisoformat(str(start))
    except ValueError:
        return False, f"sources.twitch.enable_from {start!r} is not a YYYY-MM-DD date"
    today = (now or clock.utcnow()).date()
    if today >= start_date:
        return True, f"auto-enabled from {start_date.isoformat()}"
    return False, f"Twitch starts on {start_date.isoformat()} (today is {today.isoformat()} UTC)"


class TwitchClient:
    """Helix with an app access token: fetched on demand, refreshed once on a 401."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        transport: Callable[..., Any] | None = None,
        db_path: Any = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.transport = transport or _default_transport
        self.db_path = db_path

    # ------------------------------------------------------------- token
    def token(self, force: bool = False) -> str:
        cached = _TOKENS.get(self.client_id)
        if cached and not force and cached[1] - 60 > clock.epoch():
            return cached[0]
        resp = self.transport(
            "POST", TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret,
                  "grant_type": "client_credentials"},
            timeout=20,
        )
        if int(getattr(resp, "status_code", 200)) != 200:
            raise TwitchError(f"token request failed: HTTP {resp.status_code} (check TWITCH_CLIENT_ID / SECRET)")
        data = resp.json()
        token = data["access_token"]
        _TOKENS[self.client_id] = (token, clock.epoch() + float(data.get("expires_in", 3600)))
        return token

    # ------------------------------------------------------------- requests
    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in (0, 1):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token(force=attempt == 1)}"}
            resp = self.transport("GET", f"{HELIX_BASE}/{path}", params=params, headers=headers, timeout=20)
            status = int(getattr(resp, "status_code", 200))
            if status == 401 and attempt == 0:
                continue                      # the token expired or was revoked: one fresh token, one retry
            if status == 429:
                rate_limit.record_error(SOURCE, "HTTP 429 from Helix", pause_hours=1.0, db_path=self.db_path)
                raise TwitchError("Helix rate limit (429); twitch paused for 1 h")
            if status != 200:
                raise TwitchError(f"Helix {path}: HTTP {status} {str(getattr(resp, 'text', ''))[:200]}")
            return resp.json()
        raise TwitchError(f"Helix {path}: still 401 after a fresh token")

    def game_id(self, name: str) -> str | None:
        cached = db.kv_get(K_GAME_ID, db_path=self.db_path) or ""
        if cached.startswith(f"{name}|"):
            return cached.split("|", 1)[1]
        data = self.get("games", {"name": name}).get("data") or []
        if not data:
            return None
        gid = str(data[0]["id"])
        db.kv_set(K_GAME_ID, f"{name}|{gid}", db_path=self.db_path)
        return gid

    def clips(self, game_id: str, started_at: datetime, ended_at: datetime, first: int) -> list[dict[str, Any]]:
        params = {
            "game_id": game_id,
            "started_at": clock.iso(started_at),
            "ended_at": clock.iso(ended_at),
            "first": max(1, min(int(first), 100)),
        }
        return self.get("clips", params).get("data") or []


def clip_to_candidate(clip: dict[str, Any], now: datetime, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """A Helix clip as a discovery candidate, or None if it lacks what the pipeline needs."""
    tw = twitch_cfg(cfg)
    clip_id, url = clip.get("id"), clip.get("url")
    if not clip_id or not url:
        return None
    languages = [str(x).lower() for x in (tw.get("languages") or [])]
    if languages and str(clip.get("language") or "").lower() not in languages:
        return None
    views = int(clip.get("view_count") or 0)
    if views < int(tw.get("min_views", 0)):
        return None
    try:
        created = clock.parse(clip["created_at"])
    except (KeyError, ValueError):
        return None
    min_age = float(((cfg.get("discovery") or {}).get("min_age_hours", 0.5)))
    age_h = max((now - created).total_seconds() / 3600.0, min_age)
    return {
        "id": f"item_{uuid.uuid4().hex[:12]}",
        "source": SOURCE,
        "source_id": str(clip_id),
        "channel_id": str(clip.get("broadcaster_id") or ""),
        "source_url": str(url),
        # Credit goes to the broadcaster (whose stream it is), not the viewer who pressed "clip".
        "author_name": clip.get("broadcaster_name") or "Twitch streamer",
        "original_title": clip.get("title") or "GTA VI Twitch clip",
        "original_duration": float(clip.get("duration") or 0.0),
        "view_count": views,
        "upvote_count": 0,
        "comment_count": 0,
        "upload_time": clock.iso(created),
        "raw_velocity": views / age_h,
        "age_hours": age_h,
        "is_official": False,
        "effective_weight": float(tw.get("weight", 1.10)),
    }


def discover_twitch(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    transport: Callable[..., Any] | None = None,
    now: datetime | None = None,
    alert: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Top GTA VI clips of the last `lookback_hours`, as unscored discovery candidates.

    Never raises: every failure is logged (and alerted where the user must act) and returns [].
    """
    cfg = cfg or config.get_config()
    now = now or clock.utcnow()
    tw = twitch_cfg(cfg)

    def notify(text: str, key: str) -> None:
        if alert:
            alert(text, key)
        else:
            from . import telegram_bot
            telegram_bot.send_alert(text, key=key, cfg=cfg, db_path=db_path, repeat_gap_s=None)

    on, why = is_enabled(cfg, now)
    if not on:
        log.debug("Twitch discovery off: %s", why)
        return []

    client_id, secret = config.secret("TWITCH_CLIENT_ID"), config.secret("TWITCH_CLIENT_SECRET")
    if not (client_id and secret):
        log.warning("Twitch discovery skipped: TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set")
        notify(
            "Twitch discovery is switched on but TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are missing "
            "from .env, so Twitch is being skipped (YouTube is unaffected). Create an app at "
            "dev.twitch.tv/console and add both keys.",
            ALERT_NO_CREDENTIALS,
        )
        return []

    paused, paused_why = rate_limit.is_source_paused(SOURCE, db_path)
    if paused:
        log.warning("Twitch discovery skipped: %s", paused_why)
        return []

    try:
        client = TwitchClient(client_id, secret, transport=transport, db_path=db_path)
        game_name = str(tw.get("game_name", "Grand Theft Auto VI"))
        game_id = str(tw["game_id"]) if tw.get("game_id") else client.game_id(game_name)
        if not game_id:
            log.warning("Twitch has no game called %r yet; nothing discovered", game_name)
            notify(f"Twitch has no game category called '{game_name}'. Set sources.twitch.game_name "
                   "(or game_id) in config.yaml to the exact category.", f"twitch_no_game:{game_name}")
            return []
        lookback = timedelta(hours=float(tw.get("lookback_hours", 48)))
        clips = client.clips(game_id, now - lookback, now, int(tw.get("limit", 20)))
    except (TwitchError, requests.RequestException, KeyError, ValueError) as e:
        log.error("Twitch discovery failed: %s", e)
        return []

    rate_limit.record_success(SOURCE, db_path)
    candidates = [c for c in (clip_to_candidate(clip, now, cfg) for clip in clips) if c]
    log.info("Twitch: %d clips, %d usable candidates (game id %s)", len(clips), len(candidates), game_id)
    return candidates
