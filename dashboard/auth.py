"""Single-user password login for the dashboard (local and through the remote tunnel).

The password hash lives in .env as DASHBOARD_PASSWORD_HASH (written by
`python -m dashboard set-password`). Without it the dashboard behaves exactly as before:
loopback-only and unauthenticated. With it, every route except the login page and the two
static files that draw it needs a signed-in session.

Sessions are Flask signed cookies plus a server-side version in the kv table:
set-password, clear-password and Logout replace that version, which signs out every device.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from flask.sessions import SecureCookieSessionInterface
from werkzeug.security import check_password_hash, generate_password_hash

from pipeline import db

HASH_KEY = "DASHBOARD_PASSWORD_HASH"
VERSION_KEY = "dashboard:session_version"
MIN_LENGTH = 12
IDLE_LIMIT = timedelta(hours=12)
REMEMBER_LIMIT = timedelta(days=30)
GENERIC_ERROR = "Sign-in failed. Check the password, or wait and try again later."

IP_WINDOW_S = 15 * 60
IP_MAX_FAILURES = 5
IP_BLOCK_S = 15 * 60
GLOBAL_WINDOW_S = 60 * 60
GLOBAL_MAX_FAILURES = 20
GLOBAL_BLOCK_S = 60 * 60

# Static files the login page itself needs; everything else under /static is behind login.
PUBLIC_STATIC = frozenset({"/static/style.css", "/static/favicon.svg"})
IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------- .env storage
def read_hash(env_path: Path) -> str:
    """The stored hash, read fresh from the file (set-password takes effect without a restart)."""
    try:
        text = Path(env_path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(HASH_KEY + "="):
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def write_hash(env_path: Path, value: str | None, now: datetime | None = None) -> Path | None:
    """Replace (or add, or with None remove) the hash line. Backs up an existing .env first.

    Returns the backup path, or None when there was no .env yet. Never prints the value.
    """
    env_path = Path(env_path)
    backup = None
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if env_path.exists():
        stamp = now or datetime.now()
        while True:
            backup = env_path.with_name(f"{env_path.name}.{stamp:%Y-%m-%d_%H%M%S}.bak")
            try:
                with backup.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(existing)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)
        _private(backup)
    lines = [line for line in existing.splitlines() if not line.strip().startswith(HASH_KEY + "=")]
    if value is not None:
        lines.append(f"{HASH_KEY}={value}")
    content = "\n".join(lines) + ("\n" if lines else "")
    fd, temp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
        _private(Path(temp))
        os.replace(temp, env_path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return backup


def _private(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)


def make_hash(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def rotate_sessions(db_path) -> None:
    """Every existing session (all devices) stops being valid."""
    db.kv_set(VERSION_KEY, secrets.token_hex(16), db_path=db_path)


def session_version(db_path) -> str:
    return db.kv_get(VERSION_KEY, "initial", db_path=db_path) or "initial"


def secret_key_for(password_hash: str) -> str:
    """Signing key that survives restarts (so "keep me signed in" means something) and changes
    with the password. Derived from the salted hash, which is as secret as .env itself."""
    if not password_hash:
        return secrets.token_hex(32)
    return hmac.new(password_hash.encode(), b"felony-flamingo-dashboard-session", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------- request helpers
def safe_next(value: str | None) -> str:
    """Only same-site relative paths; anything else becomes '/'."""
    if not value or not isinstance(value, str) or len(value) > 2000:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    if value.startswith("/login") or value.startswith("/logout"):
        return "/"
    return value


def login_url(path: str) -> str:
    return "/login?next=" + quote(safe_next(path), safe="")


class Limiter:
    """In-memory failure counters. A dashboard restart clears them; an attacker cannot restart it."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.by_ip: dict[str, deque] = {}
        self.ip_blocked_until: dict[str, float] = {}
        self.all_failures: deque = deque()
        self.global_blocked_until = 0.0

    def blocked(self, ip: str, remote: bool) -> bool:
        now = self.clock()
        with self.lock:
            if self.ip_blocked_until.get(ip, 0.0) > now:
                return True
            return remote and self.global_blocked_until > now

    def failure(self, ip: str) -> bool:
        """Record a failure. Returns True when this failure started the global lockout."""
        now = self.clock()
        with self.lock:
            recent = self.by_ip.setdefault(ip, deque())
            recent.append(now)
            while recent and now - recent[0] > IP_WINDOW_S:
                recent.popleft()
            if len(recent) >= IP_MAX_FAILURES:
                self.ip_blocked_until[ip] = now + IP_BLOCK_S
                recent.clear()
            self.all_failures.append(now)
            while self.all_failures and now - self.all_failures[0] > GLOBAL_WINDOW_S:
                self.all_failures.popleft()
            if len(self.all_failures) >= GLOBAL_MAX_FAILURES and self.global_blocked_until <= now:
                self.global_blocked_until = now + GLOBAL_BLOCK_S
                self.all_failures.clear()
                return True
            return False

    def success(self, ip: str) -> None:
        with self.lock:
            self.by_ip.pop(ip, None)


def check_password(stored_hash: str, password: str) -> bool:
    """werkzeug compares the derived key with hmac.compare_digest (constant time)."""
    if not stored_hash or not isinstance(password, str):
        return False
    try:
        return check_password_hash(stored_hash, password)
    except (ValueError, TypeError):
        return False


def ist_now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def background(function, *args, **kwargs) -> None:
    """Alerts must never delay or break a request. Tests replace this with a direct call."""
    threading.Thread(target=function, args=args, kwargs=kwargs, daemon=True).start()


class SessionInterface(SecureCookieSessionInterface):
    """Secure cookies when the request reached cloudflared over HTTPS (the tunnel host, or a
    forwarded https scheme). Local http://127.0.0.1 keeps a non-Secure cookie so it still works."""

    def get_cookie_secure(self, app):
        from flask import g, request
        try:
            return bool(g.get("secure_cookie") or request.is_secure
                        or request.headers.get("X-Forwarded-Proto", "").lower() == "https")
        except RuntimeError:
            return False
