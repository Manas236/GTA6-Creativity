"""Connection checks are explicit, parallel, bounded and never expose credentials."""
import json
import threading
import time
from urllib.parse import quote

import pytest
import requests

import connection_checks as checks
from dashboard.app import create_app


def response(data=None, status=200):
    value = requests.Response()
    value.status_code = status
    value._content = json.dumps(data or {}).encode()
    value._content_consumed = True
    return value


def transport_for(data=None, status=200, calls=None):
    def transport(method, url, **kwargs):
        if calls is not None:
            calls.append((method, url, kwargs))
        assert 0 < kwargs["timeout"] <= 10
        assert kwargs["allow_redirects"] is False
        return response(data, status)
    return transport


def test_empty_never_calls_network():
    def forbidden(*args, **kwargs):
        pytest.fail("An unset service made a request")
    rows = checks.run_checks({}, transport=forbidden)
    assert len(rows) == 8
    assert all(row["status"] == "not_set" for row in rows.values())
    assert all(row["checked_at"] for row in rows.values())


def test_provider_errors_and_encoded_credentials_are_masked():
    secret = "abcd-private+/credential"
    message = f"Invalid API key {secret}; encoded {quote(secret, safe='')}"
    row = checks.check_gemini({"GEMINI_API_KEY": secret}, transport=transport_for(
        {"error": {"message": message}}, 403))
    assert row["status"] == "failed"
    assert "HTTP 403: Invalid API key abcd…" in row["reason"]
    assert secret not in json.dumps(row)
    assert quote(secret, safe="") not in row["reason"]


def test_exception_url_masking():
    token = "1234:private-bot-token"
    def fail(method, url, **kwargs):
        raise requests.ConnectionError("Connection failed for " + url)
    row = checks.check_telegram_bot({"TELEGRAM_BOT_TOKEN": token}, transport=fail)
    assert token not in row["reason"]
    assert "1234…" in row["reason"]


@pytest.mark.parametrize("expiry,valid,scopes,status", [
    (0, True, ["instagram_content_publish", "pages_manage_posts"], "ok"),
    (3600, True, ["instagram_content_publish", "pages_manage_posts"], "warning"),
    (-3600, True, [], "failed"),
    (0, False, [], "failed"),
    (20 * 86400, True, ["instagram_content_publish"], "warning"),
])
def test_meta_expiry_and_scopes(expiry, valid, scopes, status):
    calls = []
    row = checks.check_instagram_token({"IG_ACCESS_TOKEN": "meta-private"},
        {"posting": {"api_version": "v25.0"}}, transport_for({"data": {
            "is_valid": valid, "expires_at": int(time.time()) + expiry if expiry else 0,
            "scopes": scopes,
        }}, calls=calls))
    assert row["status"] == status
    assert "Valid:" in row["reason"] and "expiry:" in row["reason"]
    assert "instagram_content_publish:" in row["reason"]
    assert "pages_manage_posts:" in row["reason"]
    assert calls[0][1] == "https://graph.facebook.com/v25.0/debug_token"


def test_twitch_keeps_token_only_in_memory_and_masks_it_on_error():
    calls = []
    app_token = "temp-app-token-private"
    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return response({"access_token": app_token})
        assert kwargs["headers"]["Authorization"] == "Bearer " + app_token
        assert kwargs["params"]["name"] == "Grand Theft Auto VI"
        return response({"message": "Denied " + app_token}, 401)
    row = checks.check_twitch({"TWITCH_CLIENT_ID": "client-id", "TWITCH_CLIENT_SECRET": "client-secret"}, transport=transport)
    assert calls[0][0] == "POST"
    assert calls[0][2]["data"]["grant_type"] == "client_credentials"
    assert calls[1][0] == "GET"
    assert app_token not in json.dumps(row)
    assert row["status"] == "failed"


def test_read_only_telegram_and_separate_send():
    env = {"TELEGRAM_BOT_TOKEN": "bot-private-token", "TELEGRAM_CHAT_ID": "chat-private-id"}
    calls = []
    transport = transport_for({"ok": True, "result": {"username": "test_bot"}}, calls=calls)
    rows = checks.run_checks(env, transport=transport)
    assert rows["telegram_bot"]["reason"] == "Bot @test_bot"
    assert all(method == "GET" for method, _, _ in calls)
    assert {url.rsplit("/", 1)[-1] for _, url, _ in calls} == {"getMe", "getChat"}
    assert checks.send_telegram_test(env, transport=transport)["status"] == "ok"
    assert calls[-1][0] == "POST" and calls[-1][1].endswith("/sendMessage")


@pytest.mark.parametrize("suffix", ["/fail", "/start", "/1", "?status=fail", "/%66ail", "#fragment"])
def test_healthcheck_rejects_non_success_urls(suffix):
    url = "https://hc-ping.com/00000000-1111-2222-3333-444444444444" + suffix
    calls = []
    assert checks.check_healthcheck({"HEALTHCHECK_URL": url}, transport=transport_for(calls=calls))["status"] == "failed"
    assert calls == []


def test_healthcheck_success_and_uuid_masking():
    url = "https://hc-ping.com/00000000-1111-2222-3333-444444444444"
    calls = []
    assert checks.check_healthcheck({"HEALTHCHECK_URL": url}, transport=transport_for(calls=calls))["status"] == "ok"
    assert calls[0][0:2] == ("GET", url)
    assert "00000000-1111-2222-3333-444444444444" not in checks.mask_text(url, [url])


def test_parallel_checks_and_wall_timeout(monkeypatch):
    barrier = threading.Barrier(2)
    release = threading.Event()
    monkeypatch.setattr(checks, "TIMEOUT", 0.1)
    def slow(*args):
        barrier.wait(timeout=1)
        release.wait(timeout=2)
        return checks.result("ok", "late result")
    monkeypatch.setitem(checks.CHECKS, "slow1", {"keys": (), "label": "Slow", "check": slow})
    monkeypatch.setitem(checks.CHECKS, "slow2", {"keys": (), "label": "Slow", "check": slow})
    start = time.monotonic()
    try:
        rows = checks.run_checks({}, ["slow1", "slow2"])
        assert time.monotonic() - start < 0.8
        assert all(row["status"] == "failed" for row in rows.values())
    finally:
        release.set()


def test_routes_are_explicit_csrf_protected_and_cache_results(tmp_db, monkeypatch):
    monkeypatch.setattr(checks, "current_environment", lambda: {})
    app = create_app(tmp_db)
    client = app.test_client()
    base = "http://127.0.0.1"
    csrf = client.get("/api/state", base_url=base).get_json()["csrf"]
    calls = []
    original = checks.run_checks
    def run(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(checks, "run_checks", run)
    assert client.get("/connections", base_url=base).status_code == 200
    data = client.get("/api/connections", base_url=base).get_json()
    assert all(row["checked_at"] is None for row in data["connections"])
    assert not calls
    endpoint = "/api/connections/test"
    assert client.post(endpoint, base_url=base, json={"service": "all"}).status_code == 403
    headers = {"X-CSRF-Token": csrf}
    assert client.post(endpoint, base_url=base, headers=headers, json={"service": []}).status_code == 400
    assert client.post(endpoint, base_url=base, headers={**headers, "Origin": "https://evil.example"}, json={"service": "all"}).status_code == 403
    data = client.post(endpoint, base_url=base, headers=headers, json={"service": "all"}).get_json()
    assert len(calls) == 1
    assert all(row["status"] == "not_set" and row["checked_at"] for row in data["connections"])
    assert client.get("/api/connections", base_url=base).get_json() == data
    assert len(calls) == 1
