"""Loopback-only HTTP UI. All control writes require a signed confirmation and CSRF token."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from pipeline import binding, config, db
import connection_checks
from . import commands, views
from .store import initialize, snapshot


def create_app(db_path=None, cfg=None, media_root=None):
    path = Path(db_path or db.DEFAULT_DB_PATH).resolve()
    initialize(path)
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        MAX_CONTENT_LENGTH=32 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )
    app.config["DASHBOARD_DB"] = path
    signer = URLSafeTimedSerializer(app.secret_key, salt="dashboard-confirmation")
    root = Path(media_root or config.media_root()).resolve()
    connection_results = {}
    connection_lock = threading.Lock()
    connection_run_lock = threading.Lock()

    def settings():
        return cfg if cfg is not None else config.get_config(reload=True)

    @app.before_request
    def protect_local_controls():
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403, "Use the loopback address printed when the dashboard starts.")
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
                abort(403, "Cross-origin control requests are refused.")
            token = request.headers.get("X-CSRF-Token", "")
            if not session.get("csrf") or not secrets.compare_digest(
                token, session["csrf"]
            ):
                abort(403, "Refresh the dashboard before confirming this action.")

    @app.after_request
    def security_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.errorhandler(commands.ControlError)
    @app.errorhandler(binding.CaptionMismatchError)
    def bad_control(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(sqlite3.OperationalError)
    def busy_database(error):
        return (
            jsonify(
                error="Database is busy or unavailable. Refresh and retry; no action was confirmed."
            ),
            503,
        )

    @app.get("/")
    @app.get("/queue")
    @app.get("/analytics")
    @app.get("/failures")
    @app.get("/connections")
    @app.get("/items/<item_id>")
    def page(item_id=None):
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def current_state():
        session.setdefault("csrf", secrets.token_hex(32))
        data = views.state(path, settings())
        data["csrf"] = session["csrf"]
        return jsonify(data)

    @app.get("/api/connections")
    def connections():
        with connection_lock:
            rows = connection_checks.describe(
                connection_checks.current_environment(), connection_results
            )
        return jsonify(connections=rows)

    @app.post("/api/connections/test")
    def test_connections():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("service") not in (
            "all", *connection_checks.CHECKS
        ):
            return jsonify(error="Choose a known connection check."), 400
        if not connection_run_lock.acquire(blocking=False):
            return jsonify(error="A connection test is already running."), 409
        try:
            env = connection_checks.current_environment()
            selected = None if body["service"] == "all" else [body["service"]]
            results = connection_checks.run_checks(env, selected, settings())
            with connection_lock:
                connection_results.update(results)
                rows = connection_checks.describe(env, connection_results)
            return jsonify(connections=rows)
        finally:
            connection_run_lock.release()

    @app.post("/api/connections/send-telegram-test")
    def send_telegram_test():
        if not connection_run_lock.acquire(blocking=False):
            return jsonify(error="A connection test is already running."), 409
        try:
            return jsonify(result=connection_checks.send_telegram_test(
                connection_checks.current_environment(), settings()
            ))
        finally:
            connection_run_lock.release()

    @app.get("/api/items/<item_id>")
    def detail(item_id):
        data = views.item_detail(path, item_id)
        if data is None:
            abort(404)
        return jsonify(data)

    @app.post("/api/prepare")
    def prepare():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("payload", {}), dict):
            abort(400)
        if body.get("item_id") is not None and not isinstance(body["item_id"], str):
            abort(400)
        with snapshot(path):
            plan = commands.prepare(
                str(body.get("action", "")),
                body.get("item_id"),
                body.get("payload", {}),
                path,
                settings(),
            )
        plan["client"] = session["csrf"]
        plan["nonce"] = secrets.token_hex(16)
        return jsonify(
            ticket=signer.dumps(plan),
            description=plan["description"],
            title=plan["title"],
            action=plan["action"],
            payload=plan["payload"],
        )

    @app.post("/api/confirm")
    def confirm():
        body = request.get_json(silent=True) or {}
        ticket = body.get("ticket") if isinstance(body, dict) else None
        if not isinstance(ticket, str):
            abort(400)
        try:
            plan = signer.loads(ticket, max_age=900)
        except (BadSignature, SignatureExpired):
            abort(409, "Confirmation expired or invalid. Prepare the action again.")
        if not secrets.compare_digest(plan["client"], session["csrf"]):
            abort(403)
        command_id = hashlib.sha256(ticket.encode()).hexdigest()[:32]
        result = commands.enqueue(plan, command_id, path)
        return jsonify(command=result), 202

    @app.get("/media/<item_id>/<variant>")
    def media(item_id, variant):
        column = {"a": "render_a_path", "b": "render_b_path", "cut": "cut_path"}.get(
            variant
        )
        if not column:
            abort(404)
        with snapshot(path):
            item = db.get_item(item_id, path)
        value = (item or {}).get(column)
        if not value:
            abort(404)
        video = Path(value).resolve()
        if (
            not video.is_relative_to(root)
            or video.suffix.lower() not in (".mp4", ".mov", ".webm", ".mkv")
            or not video.is_file()
        ):
            abort(404)
        return send_file(video, conditional=True, mimetype="video/mp4")

    return app
