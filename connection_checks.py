"""Explicit, server-side connection probes. No publishing or automatic checks.

Public check_* functions accept an environment mapping, optional config and an
injectable requests-compatible transport. Results contain only masked text.
"""
from __future__ import annotations

import functools
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus, unquote, urlsplit

import requests

from pipeline import config
from pipeline.posting import API_VERSION

TIMEOUT = 10.0
CHECKS = {}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def mask_text(text, values):
    """Mask raw and escaped credentials, including credentials echoed by APIs."""
    text = str(text)
    replacements = {}
    for value in values:
        if not value:
            continue
        value = str(value)
        masked = value[:4] + "…" if len(value) > 4 else "…"
        variants = (value, quote(value, safe=""), quote_plus(value),
                    json.dumps(value, ensure_ascii=True)[1:-1])
        for variant in variants:
            replacements[variant] = masked
        # The Healthchecks UUID itself is a credential, even without the URL.
        if value.startswith(("https://", "http://")):
            for part in urlsplit(value).path.split("/"):
                if len(part) >= 16:
                    replacements[part] = part[:4] + "…"
    if replacements:
        pattern = "|".join(re.escape(v) for v in sorted(replacements, key=len, reverse=True))
        text = re.sub(pattern, lambda m: replacements[m.group()], text)
    return text[:1200]


def secret_values(env):
    return [v for k, v in env.items() if v and (
        k in {key for spec in CHECKS.values() for key in spec["keys"]}
        or re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|HEALTHCHECK_URL", k, re.I)
    )]


def result(status, reason, checked_at=None):
    return dict(status=status, reason=reason, checked_at=checked_at or timestamp())


class CheckFailure(Exception):
    pass


class Context:
    def __init__(self, env, cfg, transport):
        self.env = env
        self.values = secret_values(env)
        self.transport = transport or requests.request
        self.deadline = time.monotonic() + TIMEOUT
        version = (cfg or {}).get("posting", {}).get("api_version", API_VERSION)
        # Credentials only go to the real provider, never a configured proxy.
        self.graph = "https://graph.facebook.com/" + str(version)

    def call(self, method, url, *, json_required=True, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        response = self.transport(method, url, timeout=remaining,
                                  allow_redirects=False, stream=True, **kwargs)
        try:
            if response.status_code == 200 and not json_required:
                return {}
            body = bytearray()
            for chunk in response.iter_content(chunk_size=4096):
                if time.monotonic() >= self.deadline:
                    raise TimeoutError()
                body.extend(chunk)
                if len(body) > 65536:
                    raise CheckFailure("API response exceeded 64 KiB")
            message = body.decode("utf-8", errors="replace")
            try:
                data = json.loads(message)
            except ValueError:
                data = None
            if response.status_code != 200:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                reason = error.get("message") if isinstance(error, dict) else error
                if isinstance(data, dict):
                    reason = reason or data.get("description") or data.get("message")
                raise CheckFailure(f"HTTP {response.status_code}: {reason or message[:800] or 'empty response'}")
            if not isinstance(data, dict):
                raise CheckFailure("API returned an invalid JSON response")
            return data
        finally:
            response.close()

    def telegram(self, method, *, send=False, **params):
        token = quote(self.env["TELEGRAM_BOT_TOKEN"], safe=":")
        data = self.call("POST" if send else "GET",
                         f"https://api.telegram.org/bot{token}/{method}",
                         **({"data": params} if send else {"params": params}))
        if not data.get("ok"):
            raise CheckFailure(data.get("description", "Telegram returned ok=false"))
        return data.get("result", {})


def service(service_id, label, keys):
    def decorate(fn):
        @functools.wraps(fn)
        def check(env, cfg=None, transport=None):
            missing = [key for key in keys if not str(env.get(key, "")).strip()]
            if missing:
                return result("not_set", "not set: " + ", ".join(missing))
            ctx = Context(env, cfg, transport)
            try:
                status, reason = fn(ctx)
                output = result(status, reason)
            except (TimeoutError, requests.Timeout):
                output = result("failed", "Timed out after 10 seconds")
            except Exception as exc:
                output = result("failed", str(exc) or type(exc).__name__)
            output["reason"] = mask_text(output["reason"], ctx.values)
            return output
        CHECKS[service_id] = dict(label=label, keys=keys, check=check)
        return check
    return decorate


@service("gemini", "GEMINI_API_KEY", ("GEMINI_API_KEY",))
def check_gemini(ctx):
    ctx.call("GET", "https://generativelanguage.googleapis.com/v1beta/models",
             params={"key": ctx.env["GEMINI_API_KEY"]}, json_required=False)
    return "ok", "Models endpoint returned HTTP 200"


@service("telegram_bot", "TELEGRAM_BOT_TOKEN", ("TELEGRAM_BOT_TOKEN",))
def check_telegram_bot(ctx):
    bot = ctx.telegram("getMe")
    return "ok", "Bot @" + str(bot.get("username", "unknown"))


@service("telegram_chat", "TELEGRAM_CHAT_ID", ("TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN"))
def check_telegram_chat(ctx):
    ctx.telegram("getChat", chat_id=ctx.env["TELEGRAM_CHAT_ID"])
    return "ok", "Bot can see the chat"


@service("instagram_token", "IG_ACCESS_TOKEN", ("IG_ACCESS_TOKEN",))
def check_instagram_token(ctx):
    token = ctx.env["IG_ACCESS_TOKEN"]
    # Meta can refuse self-inspection for some token types. Surface that actual
    # error; do not invent validity or require saving another app credential.
    data = ctx.call("GET", ctx.graph + "/debug_token",
                    params={"input_token": token},
                    headers={"Authorization": "Bearer " + token}).get("data", {})
    if "is_valid" not in data:
        raise CheckFailure("Token debugger did not return validity data")
    valid = bool(data["is_valid"])
    expiry = int(data.get("expires_at") or 0)
    scopes = set(data.get("scopes") or [])
    required = ("instagram_content_publish", "pages_manage_posts")
    missing = [scope for scope in required if scope not in scopes]
    expired = bool(expiry and expiry <= time.time())
    soon = bool(expiry and expiry - time.time() <= 7 * 86400)
    expiry_text = datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else "not reported (0/no scheduled expiry)"
    status = "failed" if not valid or expired else "warning" if soon or missing else "ok"
    reason = f"Valid: {'yes' if valid else 'no'}; expiry: {expiry_text}; "
    reason += "; ".join(f"{scope}: {'yes' if scope in scopes else 'no'}" for scope in required)
    if expired:
        reason += "; expired"
    elif soon:
        reason += "; expires within 7 days"
    if isinstance(data.get("error"), dict):
        reason += "; " + str(data["error"].get("message", ""))
    return status, reason


@service("instagram_user", "IG_USER_ID", ("IG_USER_ID", "IG_ACCESS_TOKEN"))
def check_instagram_user(ctx):
    data = ctx.call("GET", ctx.graph + "/" + quote(ctx.env["IG_USER_ID"], safe=""),
                    params={"fields": "username"},
                    headers={"Authorization": "Bearer " + ctx.env["IG_ACCESS_TOKEN"]})
    if not data.get("username"):
        raise CheckFailure("API did not return a username")
    return "ok", "Instagram @" + str(data["username"])


@service("facebook_page", "FB_PAGE_ID", ("FB_PAGE_ID", "IG_ACCESS_TOKEN"))
def check_facebook_page(ctx):
    data = ctx.call("GET", ctx.graph + "/" + quote(ctx.env["FB_PAGE_ID"], safe=""),
                    params={"fields": "name"},
                    headers={"Authorization": "Bearer " + ctx.env["IG_ACCESS_TOKEN"]})
    if not data.get("name"):
        raise CheckFailure("API did not return a page name")
    return "ok", "Page: " + str(data["name"])


@service("twitch", "TWITCH_CLIENT_ID + TWITCH_CLIENT_SECRET", ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"))
def check_twitch(ctx):
    data = ctx.call("POST", "https://id.twitch.tv/oauth2/token", data={
        "client_id": ctx.env["TWITCH_CLIENT_ID"],
        "client_secret": ctx.env["TWITCH_CLIENT_SECRET"],
        "grant_type": "client_credentials",
    })
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise CheckFailure("Twitch did not return an app access token")
    ctx.values.append(token)  # Ephemeral: mask even an echoed token on the next request.
    ctx.call("GET", "https://api.twitch.tv/helix/games",
             params={"name": "Grand Theft Auto VI"}, json_required=False,
             headers={"Client-Id": ctx.env["TWITCH_CLIENT_ID"],
                      "Authorization": "Bearer " + token})
    return "ok", "App authentication and games endpoint returned HTTP 200"


@service("healthcheck", "HEALTHCHECK_URL", ("HEALTHCHECK_URL",))
def check_healthcheck(ctx):
    url = ctx.env["HEALTHCHECK_URL"]
    parsed = urlsplit(url)
    path = unquote(parsed.path).rstrip("/")
    # A normal Healthchecks ping ends at the UUID (also supports self-hosting).
    # Reject status suffixes, queries, credentials, fragments and redirects so
    # this cannot accidentally call /fail, /start or another non-success route.
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"/(?:ping/)?[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", path)):
        raise CheckFailure("Expected an HTTPS success-ping URL ending at the check UUID; no suffix or query")
    _ = parsed.port  # Validate the port before making any request.
    ctx.call("GET", url, json_required=False)
    return "ok", "Real success ping sent (HTTP 200)"


def send_telegram_test(env, cfg=None, transport=None):
    """The only send operation. Never registered in CHECKS or included in all."""
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not env.get(k, "").strip()]
    if missing:
        return result("not_set", "not set: " + ", ".join(missing))
    ctx = Context(env, cfg, transport)
    try:
        ctx.telegram("sendMessage", send=True, chat_id=env["TELEGRAM_CHAT_ID"],
                     text="GTA 6 dashboard connection test.")
        return result("ok", "Test message sent")
    except (TimeoutError, requests.Timeout):
        return result("failed", "Timed out after 10 seconds; delivery is unknown; no retry was sent")
    except Exception as exc:
        return result("failed", mask_text(str(exc) or type(exc).__name__, ctx.values))


def describe(env, cached=None):
    """Local metadata only: loading/polling a page never performs a probe."""
    cached = cached or {}
    return [dict(id=key, label=spec["label"], **cached.get(key, dict(
        status="unchecked" if all(env.get(k, "").strip() for k in spec["keys"]) else "not_set",
        reason="Not checked" if all(env.get(k, "").strip() for k in spec["keys"]) else "not set",
        checked_at=None,
    ))) for key, spec in CHECKS.items()]


def run_checks(env, selected=None, cfg=None, transport=None):
    """Run selected probes in parallel; return at the 10-second wall deadline."""
    selected = list(CHECKS) if selected is None else list(selected)
    if not selected or any(key not in CHECKS for key in selected):
        raise ValueError("Unknown connection check")
    pool = ThreadPoolExecutor(max_workers=len(selected), thread_name_prefix="connection-check")
    started = time.monotonic()
    futures = {pool.submit(CHECKS[key]["check"], env, cfg, transport): key for key in selected}
    try:
        done, pending = wait(futures, timeout=max(0, TIMEOUT - (time.monotonic() - started)))
        output = {futures[f]: f.result() for f in done}
        for future in pending:
            future.cancel()
            output[futures[future]] = result("failed", "Timed out after 10 seconds")
        return output
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def current_environment():
    # Read fresh values after local .env edits, without changing the runner cache.
    return config.load_env(config.project_root() / ".env")
