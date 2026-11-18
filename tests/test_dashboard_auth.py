"""Dashboard password login, brute-force limits, login alerts, tunnel host allowlist and the
cloudflared supervisor. Every DB and .env is temporary; no network, no real cloudflared."""

import copy
import io
import re
import time

import pytest

from dashboard import __main__ as cli
from dashboard import auth, tunnel
from dashboard.app import create_app
from pipeline import config, db, hooks, posting, telegram_bot

BASE = "http://127.0.0.1"
TUNNEL_HOST = "brave-lime-otter-sample.trycloudflare.com"
TUNNEL = f"https://{TUNNEL_HOST}"
PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dashboard attempted an external API call")

    monkeypatch.setattr(posting.MetaClient, "request", forbidden)
    monkeypatch.setattr(telegram_bot.ApprovalBot, "send_message", forbidden)
    monkeypatch.setattr(hooks, "_call_gemini", forbidden)
    # Alerts run inline in tests so their calls can be counted.
    monkeypatch.setattr(auth, "background", lambda function, *a, **k: function(*a, **k))


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "dashboard.env"
    path.write_text("TELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    return path


@pytest.fixture
def cfg():
    return copy.deepcopy(config.get_config(reload=True))


def make_app(tmp_db, cfg, tmp_path, env_file, password=PASSWORD, remote_host=TUNNEL_HOST):
    if password:
        auth.write_hash(env_file, auth.make_hash(password))
    remote = tunnel.RemoteState()
    if remote_host:
        remote.set("on", f"https://{remote_host}")
    alerts = []
    app = create_app(tmp_db, cfg, tmp_path, env_path=env_file, remote=remote, alert=alerts.append)
    app.config["TESTING"] = True
    return app, alerts


def login(client, password=PASSWORD, base=BASE, ip=None, remember=False, next_path="/"):
    headers = {"Origin": base}
    if ip:
        headers["CF-Connecting-IP"] = ip
    page = client.get("/login", base_url=base, headers=headers)
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.get_data(as_text=True)).group(1)
    form = {"csrf": token, "password": password, "next": next_path}
    if remember:
        form["remember"] = "on"
    return client.post("/login", base_url=base, data=form, headers=headers)


PAGES = ["/", "/queue", "/analytics", "/failures", "/make", "/review", "/library",
         "/settings", "/connections", "/items/x"]
GET_APIS = ["/api/state", "/api/library", "/api/settings", "/api/schedule", "/api/discovered",
            "/api/logs", "/api/connections", "/api/items/x"]
POST_APIS = ["/api/settings", "/api/settings/x", "/api/runner", "/api/connections/test",
             "/api/connections/send-telegram-test", "/api/prepare", "/api/confirm", "/logout"]


def every_rule_is_covered(app):
    listed = set(PAGES + GET_APIS + POST_APIS + ["/media/x/x", "/login", "/static/x"])
    for rule in app.url_map.iter_rules():
        sample = re.sub(r"<[^>]+>", "x", rule.rule)
        assert sample in listed or sample.startswith("/static"), f"route not covered by tests: {rule}"


# --------------------------------------------------------------------- no password: unchanged
def test_without_hash_dashboard_is_open_and_remote_off(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file, password=None, remote_host=None)
    client = app.test_client()
    state = client.get("/api/state", base_url=BASE)
    assert state.status_code == 200
    assert state.get_json()["auth"] is False
    assert state.get_json()["remote"]["status"] == "off"
    assert client.get("/", base_url=BASE).status_code == 200
    assert client.get("/login", base_url=BASE).status_code == 302


# --------------------------------------------------------------------- with password
def test_every_route_needs_login_then_works(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    every_rule_is_covered(app)
    client = app.test_client()
    for page in PAGES:
        response = client.get(page, base_url=BASE)
        assert response.status_code == 302, page
        assert response.headers["Location"].startswith("/login?next=")
    for api in GET_APIS:
        response = client.get(api, base_url=BASE)
        assert response.status_code == 401, api
        assert response.get_json()["login"] == "/login"
    for api in POST_APIS:
        assert client.post(api, base_url=BASE, json={}, headers={"Origin": BASE}).status_code == 401, api
    assert client.get("/media/x/a", base_url=BASE).status_code == 401
    assert client.get("/media/x/thumb", base_url=BASE).status_code == 401
    assert client.get("/static/app.js", base_url=BASE).status_code == 302
    assert client.get("/static/style.css", base_url=BASE).status_code == 200
    assert client.get("/static/favicon.svg", base_url=BASE).status_code == 200

    response = login(client)
    assert response.status_code == 302 and response.headers["Location"] == "/"
    for page in PAGES:
        assert client.get(page, base_url=BASE).status_code == 200, page
    state = client.get("/api/state", base_url=BASE).get_json()
    assert state["auth"] is True and state["remote"]["url"] == TUNNEL
    for api in ("/api/library", "/api/settings", "/api/schedule", "/api/discovered", "/api/logs",
                "/api/connections"):
        assert client.get(api, base_url=BASE).status_code == 200, api
    assert client.get("/api/items/x", base_url=BASE).status_code == 404
    assert client.get("/media/x/thumb", base_url=BASE).status_code == 200
    # Control POSTs still need the CSRF header after login.
    assert client.post("/api/runner", base_url=BASE, json={}, headers={"Origin": BASE}).status_code == 403


def test_login_keeps_page_next_and_refuses_open_redirects(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    assert client.get("/queue?view=list", base_url=BASE).headers["Location"] == "/login?next=%2Fqueue%3Fview%3Dlist"
    assert login(client, next_path="/queue?view=list").headers["Location"] == "/queue?view=list"
    for evil in ("//evil.com", "https://evil.com", "/\\evil.com", "\\\\evil.com", "javascript:alert(1)",
                 "evil.com", "/\nLocation: //evil.com", "/login?next=//evil.com"):
        assert login(app.test_client(), next_path=evil).headers["Location"] == "/", evil
    assert auth.safe_next("//evil.com") == "/"
    page = app.test_client().get("/login?next=//evil.com", base_url=BASE).get_data(as_text=True)
    assert 'name="next" value="/"' in page


def test_wrong_password_generic_error_and_csrf(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    wrong = login(client, password="not the password")
    assert wrong.status_code == 401
    assert auth.GENERIC_ERROR in wrong.get_data(as_text=True)
    missing_token = client.post("/login", base_url=BASE, data={"password": PASSWORD}, headers={"Origin": BASE})
    assert missing_token.status_code == 401
    assert auth.GENERIC_ERROR in missing_token.get_data(as_text=True)
    assert client.get("/api/state", base_url=BASE).status_code == 401


def test_sixth_attempt_from_same_tunnel_ip_is_refused_even_if_correct(tmp_db, cfg, tmp_path, env_file):
    app, alerts = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    for _ in range(5):
        assert login(client, "wrong password!!", base=TUNNEL, ip="203.0.113.7").status_code == 401
    refused = login(client, PASSWORD, base=TUNNEL, ip="203.0.113.7")
    assert refused.status_code == 401 and auth.GENERIC_ERROR in refused.get_data(as_text=True)
    assert client.get("/api/state", base_url=TUNNEL).status_code == 401
    # Another visitor IP and the local machine are not blocked by that IP's failures.
    assert login(app.test_client(), PASSWORD, base=TUNNEL, ip="198.51.100.9").status_code == 302
    assert login(app.test_client(), PASSWORD).status_code == 302
    assert not any("failed sign-ins" in text for text in alerts)


def test_global_lockout_refuses_remote_but_not_local_and_alerts(tmp_db, cfg, tmp_path, env_file):
    app, alerts = make_app(tmp_db, cfg, tmp_path, env_file)
    for n in range(20):
        login(app.test_client(), "wrong password!!", base=TUNNEL, ip=f"203.0.113.{n}")
    assert sum("20 failed sign-ins" in text for text in alerts) == 1
    assert login(app.test_client(), PASSWORD, base=TUNNEL, ip="198.51.100.200").status_code == 401
    assert login(app.test_client(), PASSWORD).status_code == 302


def test_tunnel_login_alerts_once_and_local_login_does_not(tmp_db, cfg, tmp_path, env_file):
    app, alerts = make_app(tmp_db, cfg, tmp_path, env_file)
    login(app.test_client(), PASSWORD)
    assert alerts == []
    client = app.test_client()
    page = client.get("/login", base_url=TUNNEL, headers={"CF-Connecting-IP": "203.0.113.50"})
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', page.get_data(as_text=True)).group(1)
    result = client.post("/login", base_url=TUNNEL, data={"csrf": token, "password": PASSWORD, "next": "/"},
                         headers={"Origin": TUNNEL, "CF-Connecting-IP": "203.0.113.50",
                                  "User-Agent": "TestPhone/1.0"})
    assert result.status_code == 302
    assert len(alerts) == 1
    assert "203.0.113.50" in alerts[0] and "TestPhone/1.0" in alerts[0] and "IST" in alerts[0]
    assert PASSWORD not in alerts[0]
    cookie = result.headers["Set-Cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert client.get("/api/state", base_url=TUNNEL).status_code == 200


def test_local_cookie_is_not_secure_and_remember_me_persists(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    plain = login(app.test_client()).headers["Set-Cookie"]
    assert "Secure" not in plain and "Expires" not in plain and "SameSite=Lax" in plain
    kept = login(app.test_client(), remember=True).headers["Set-Cookie"]
    assert "Expires" in kept


def test_idle_sessions_expire(tmp_db, cfg, tmp_path, env_file, monkeypatch):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    login(client)
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 11 * 3600)
    assert client.get("/api/state", base_url=BASE).status_code == 200  # activity refreshes it
    monkeypatch.setattr(time, "time", lambda: real + 22 * 3600)
    assert client.get("/api/state", base_url=BASE).status_code == 200
    monkeypatch.setattr(time, "time", lambda: real + 35 * 3600)
    assert client.get("/api/state", base_url=BASE).status_code == 401

    remembered = app.test_client()
    monkeypatch.setattr(time, "time", lambda: real)
    login(remembered, remember=True)
    monkeypatch.setattr(time, "time", lambda: real + 20 * 86400)
    assert remembered.get("/api/state", base_url=BASE).status_code == 200


def test_logout_invalidates_old_session_cookies(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client, other = app.test_client(), app.test_client()
    login(client)
    login(other)
    stolen = client.get_cookie("session", domain="127.0.0.1").value
    csrf = client.get("/api/state", base_url=BASE).get_json()["csrf"]
    out = client.post("/logout", base_url=BASE, json={}, headers={"Origin": BASE, "X-CSRF-Token": csrf})
    assert out.status_code == 200 and out.get_json()["redirect"] == "/login"
    assert client.get("/api/state", base_url=BASE).status_code == 401
    assert other.get("/api/state", base_url=BASE).status_code == 401
    replay = app.test_client()
    replay.set_cookie("session", stolen, domain="127.0.0.1")
    assert replay.get("/api/state", base_url=BASE).status_code == 401


def test_set_and_clear_password_commands_sign_everyone_out(tmp_db, cfg, tmp_path, env_file, monkeypatch):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    login(client)
    assert client.get("/api/state", base_url=BASE).status_code == 200
    output = []
    answers = iter(["short", "short"])
    assert cli.password_command(["set-password", "--db", str(tmp_db)], prompt=lambda _: next(answers),
                                env_path=env_file, out=output.append) == 1
    answers = iter(["another long password", "different long password"])
    assert cli.password_command(["set-password", "--db", str(tmp_db)], prompt=lambda _: next(answers),
                                env_path=env_file, out=output.append) == 1
    assert client.get("/api/state", base_url=BASE).status_code == 200  # refusals changed nothing
    answers = iter(["another long password"] * 2)
    assert cli.password_command(["set-password", "--db", str(tmp_db)], prompt=lambda _: next(answers),
                                env_path=env_file, out=output.append) == 0
    text = env_file.read_text(encoding="utf-8")
    assert text.count(auth.HASH_KEY + "=") == 1 and "TELEGRAM_CHAT_ID=1" in text
    assert auth.read_hash(env_file).startswith("scrypt:")
    assert list(tmp_path.glob("dashboard.env.*.bak"))
    joined = "\n".join(output)
    assert "another long password" not in joined and auth.read_hash(env_file) not in joined
    assert client.get("/api/state", base_url=BASE).status_code == 401
    assert login(app.test_client(), PASSWORD).status_code == 401
    assert login(app.test_client(), "another long password").status_code == 302

    fresh = app.test_client()
    login(fresh, "another long password")
    assert cli.password_command(["clear-password", "--db", str(tmp_db)], env_path=env_file,
                                out=output.append) == 0
    assert auth.read_hash(env_file) == ""
    assert app.test_client().get("/api/state", base_url=BASE).status_code == 200  # open again


# --------------------------------------------------------------------- Host / Origin
def test_only_this_runs_tunnel_host_is_allowed(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    assert client.get("/login", base_url=TUNNEL).status_code == 200
    for other in ("https://other-words-here.trycloudflare.com", "https://trycloudflare.com",
                  f"https://x.{TUNNEL_HOST}", f"https://{TUNNEL_HOST}.evil.com", "http://evil.example"):
        assert client.get("/login", base_url=other).status_code == 403, other
    login(client, base=TUNNEL, ip="203.0.113.5")
    csrf = client.get("/api/state", base_url=TUNNEL).get_json()["csrf"]
    headers = {"X-CSRF-Token": csrf}
    bad_origin = client.post("/api/runner", base_url=TUNNEL, json={"operation": "x"},
                             headers={**headers, "Origin": "https://other-words-here.trycloudflare.com"})
    assert bad_origin.status_code == 403
    assert client.post("/api/runner", base_url=TUNNEL, json={"operation": "x"}, headers=headers).status_code == 403
    good = client.post("/api/runner", base_url=TUNNEL, json={"operation": "x"}, headers={**headers, "Origin": TUNNEL})
    assert good.status_code == 400  # passed Host, Origin, login and CSRF; the body is invalid


def test_tunnel_refused_when_password_is_cleared_while_tunnel_runs(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    assert client.get("/login", base_url=TUNNEL).status_code == 200
    auth.write_hash(env_file, None)  # clear-password with the dashboard (and tunnel) still running
    for path in ("/", "/login", "/api/state", "/media/x/a"):
        assert client.get(path, base_url=TUNNEL).status_code == 403, path
    assert client.get("/api/state", base_url=BASE).status_code == 200  # local stays usable


def test_tunnel_host_refused_when_remote_is_off(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file, remote_host=None)
    assert app.test_client().get("/login", base_url=TUNNEL).status_code == 403


def test_security_headers(tmp_db, cfg, tmp_path, env_file):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file)
    client = app.test_client()
    for response in (client.get("/login", base_url=BASE), client.get("/api/state", base_url=BASE)):
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


# --------------------------------------------------------------------- tunnel process
class FakeProcess:
    def __init__(self, lines, exit_after=False):
        self.stdout = io.StringIO("".join(lines))
        self.returncode = None
        self.exit_after = exit_after
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


CLOUDFLARED_OUTPUT = [
    "2026-11-18T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...\n",
    "2026-11-18T10:00:01Z INF +--------------------------------------------------------------------+\n",
    f"2026-11-18T10:00:01Z INF |  https://{TUNNEL_HOST}                     |\n",
    "2026-11-18T10:00:01Z INF +--------------------------------------------------------------------+\n",
]


def test_tunnel_url_is_parsed_allowed_and_sent_once():
    state, sent, commands = tunnel.RemoteState(), [], []

    def popen(command, **kwargs):
        commands.append(command)
        return FakeProcess(CLOUDFLARED_OUTPUT)

    t = tunnel.Tunnel("cloudflared", 8765, state, sent.append, popen=popen, echo=lambda text: None)
    assert t.start_once() == TUNNEL
    assert commands == [["cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate"]]
    assert state.allowed_host(TUNNEL_HOST) and not state.allowed_host("other.trycloudflare.com")
    assert sent == [f"Dashboard link: {TUNNEL}"]
    t.stop()
    assert state.describe()["status"] == "off" and not state.allowed_host(TUNNEL_HOST)


def test_tunnel_without_url_times_out_and_is_terminated():
    state, sent, procs = tunnel.RemoteState(), [], []

    class Silent(FakeProcess):
        def __init__(self):
            super().__init__([])
            self.stdout = iter(())

    def popen(command, **kwargs):
        procs.append(Silent())
        return procs[-1]

    t = tunnel.Tunnel("cloudflared", 8765, state, sent.append, popen=popen, timeout_s=0.2, echo=lambda text: None)
    assert t.start_once() is None and sent == [] and state.host is None


def test_tunnel_restarts_with_backoff_and_sends_new_link():
    state, sent, waits = tunnel.RemoteState(), [], []
    hosts = iter(["first-link-aa", "second-link-bb"] + ["never"] * 10)

    def popen(command, **kwargs):
        host = next(hosts)
        return FakeProcess([f"INF |  https://{host}.trycloudflare.com |\n"] if host != "never" else [])

    t = tunnel.Tunnel("cloudflared", 8765, state, sent.append, popen=popen, timeout_s=0.2,
                      wait=lambda seconds: waits.append(seconds) or False, echo=lambda text: None)
    t.supervise()
    assert sent == ["Dashboard link: https://first-link-aa.trycloudflare.com",
                    "Dashboard link: https://second-link-bb.trycloudflare.com"]
    assert waits == [5, 10, 20, 40, 80] and state.describe()["status"] == "failed"


def test_remote_never_starts_without_password(tmp_db, cfg, tmp_path, env_file, monkeypatch):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file, password=None, remote_host=None)
    app.config["DASHBOARD_NOTIFY"] = lambda text: pytest.fail("no link may be sent")
    monkeypatch.setattr(tunnel.Tunnel, "start", lambda self: pytest.fail("tunnel started without a password"))
    monkeypatch.setattr(tunnel, "find_cloudflared", lambda configured=None: "cloudflared")
    lines = []
    enabled = {"dashboard": {"remote": {"enabled": True, "cloudflared_path": None}}}
    assert cli.start_remote(enabled, 8765, app, lines.append) is None
    assert "waiting for a password" in lines[0]
    assert app.config["DASHBOARD_REMOTE"].describe()["status"] == "waiting_for_password"
    assert app.test_client().get("/api/state", base_url=BASE).get_json()["remote"]["status"] == "waiting_for_password"


def test_remote_starts_with_password_and_stays_off_when_disabled(tmp_db, cfg, tmp_path, env_file, monkeypatch):
    app, _ = make_app(tmp_db, cfg, tmp_path, env_file, remote_host=None)
    app.config["DASHBOARD_NOTIFY"] = lambda text: None
    started = []
    monkeypatch.setattr(tunnel.Tunnel, "start", lambda self: started.append(self.command()))
    monkeypatch.setattr(tunnel, "find_cloudflared", lambda configured=None: "/usr/bin/cloudflared")
    assert cli.start_remote({"dashboard": {"remote": {"enabled": False}}}, 8765, app, lambda text: None) is None
    assert started == []
    assert cli.start_remote({"dashboard": {"remote": {"enabled": True}}}, 8765, app, lambda text: None)
    assert started == [["/usr/bin/cloudflared", "tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate"]]


def test_default_config_keeps_remote_off():
    remote = config.get_config(reload=True)["dashboard"]["remote"]
    import yaml
    shipped = yaml.safe_load((config.ROOT_DIR / "config.yaml").read_text(encoding="utf-8"))
    assert shipped["dashboard"]["remote"] == {"enabled": False, "cloudflared_path": None}
    assert set(remote) == {"enabled", "cloudflared_path"}
