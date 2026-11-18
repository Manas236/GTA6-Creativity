"""Loopback-only HTTP UI. All control writes require a signed confirmation and CSRF token."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from datetime import date, timedelta
import secrets
import sqlite3
import threading
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from pipeline import binding, clock, config, creation, db, media_paths, posting, runner, scheduler
import connection_checks
from . import commands, settings as preferences, views, workers
from .store import initialize, snapshot, transaction
from .thumbnails import Thumbnails


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
    thumbnails = Thumbnails(path, root)
    demo = bool(db.kv_get("dashboard:demo", db_path=path))
    connection_results = {}
    connection_inputs = {}
    connection_lock = threading.Lock()
    connection_run_lock = threading.Lock()
    submit_lock = threading.Lock()

    target = path.parent if demo or (cfg is not None and db_path is not None) else config.project_root()

    def settings():
        loaded = cfg if cfg is not None else config.get_config(reload=True)
        if not demo and target == config.project_root():
            return loaded
        import yaml
        local = target / "config.local.yaml"
        overlay = yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {}
        effective = config.merge_config(loaded, overlay or {})
        return config.merge_config(effective, {"app": {"dry_run": True}}) if demo else effective

    def validate_live(effective):
        if demo:
            raise commands.ControlError("Demo data cannot go live.")
        platforms = posting.enabled_platforms(effective)
        if not platforms:
            raise commands.ControlError("Enable at least one posting platform before going live.")
        required = {"instagram": ("instagram_token", "instagram_user"), "facebook": ("facebook_page",)}
        env = connection_checks.current_environment()
        with connection_lock:
            for platform in platforms:
                for service in required[platform]:
                    result = connection_results.get(service, {})
                    inputs = tuple(env.get(key) for key in connection_checks.CHECKS[service]["keys"])
                    if (connection_inputs.get(service) != inputs
                            or result.get("status") != "ok" or not result.get("checked_at")
                            or clock.utcnow() - clock.parse(result["checked_at"]) > timedelta(minutes=15)):
                        raise commands.ControlError("Run a successful Meta connection test first (valid for 15 minutes).")

    def confirm_operation(plan, command_id, effective):
        action = plan["action"]
        command = commands.enqueue(plan, command_id, path)
        if command["status"] != "pending":
            return command
        with transaction(path) as conn:
            try:
                if action == "go_live":
                    validate_live(effective)
                if action in ("go_live", "go_dry"):
                    dry = action == "go_dry"
                    message = preferences.save_overlay({"app": {"dry_run": dry},
                        "posting": {"dry_run_mode": "plan_only" if dry else None}}, target)
                    status = "applied"
                else:
                    lock = runner.active_lock(path, runner._lock_settings(effective)[0])
                    if lock and lock.get("kind") != "loop":
                        raise commands.ControlError("The runner is busy. Retry when the current job ends.")
                    if not lock:
                        workers.spawn(path, root, "operation", "--job", command_id)
                    return command
            except (commands.ControlError, OSError) as error:
                status, message = "refused", str(error)
            conn.execute("UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
                         (status, message, db.now(), command_id))
            return dict(conn.execute("SELECT * FROM dashboard_commands WHERE id=?", (command_id,)).fetchone())

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
    @app.get("/make")
    @app.get("/review")
    @app.get("/library")
    @app.get("/settings")
    @app.get("/connections")
    @app.get("/items/<item_id>")
    def page(item_id=None):
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def current_state():
        session.setdefault("csrf", secrets.token_hex(32))
        data = views.state(path, settings(), root)
        data["csrf"] = session["csrf"]
        return jsonify(data)

    @app.get("/api/library")
    def library():
        with snapshot(path):
            return jsonify(views.library_page(path, settings(), request.args))

    @app.get("/api/settings")
    def get_preferences():
        return jsonify({**preferences.values(settings()), **preferences.grouped(settings())})

    @app.post("/api/settings/<group>")
    def save_group(group):
        overlay = preferences.group_overlay(group, request.get_json(silent=True), settings())
        return jsonify(message=preferences.save_overlay(overlay, target))

    @app.get("/api/schedule")
    def schedule():
        effective = settings()
        try:
            first = (date.fromisoformat(request.args["from"]) if "from" in request.args else
                     clock.utcnow().astimezone(scheduler.active_profile(effective)[1]).date())
            days = int(request.args.get("days", 7))
            if not 1 <= days <= 60:
                raise ValueError()
        except ValueError:
            raise commands.ControlError("Choose a YYYY-MM-DD date and 1–60 days.") from None
        return jsonify(views.schedule(path, effective, first, days))

    @app.get("/api/discovered")
    def discovered():
        return jsonify(views.discovered(path))

    @app.get("/api/logs")
    def logs():
        try:
            count = int(request.args.get("lines", 200))
            if not 1 <= count <= 1000:
                raise ValueError()
        except ValueError:
            raise commands.ControlError("Choose 1–1,000 log lines.") from None
        files = [file for file in (root / "logs").glob("dashboard-*.log")
                 if file.is_file() and not file.is_symlink()]
        if not files:
            return jsonify(lines=[])
        latest = max(files, key=lambda file: file.stat().st_mtime)
        values = connection_checks.secret_values({**os.environ, **connection_checks.current_environment()})
        with latest.open(encoding="utf-8", errors="replace") as stream:
            lines = [redact_log(line.rstrip(), values) for line in deque(stream, maxlen=count)]
        return jsonify(lines=lines)

    @app.post("/api/settings")
    def save_preferences():
        return jsonify(message=preferences.save(request.get_json(silent=True), settings(), target))

    @app.post("/api/runner")
    def runner_control():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("operation") not in ("start", "stop", "force"):
            abort(400)
        return jsonify(message=workers.control(body["operation"], path, settings(), root, body.get("stage")))

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
            env = {} if demo else connection_checks.current_environment()
            selected = None if body["service"] == "all" else [body["service"]]
            results = connection_checks.run_checks(env, selected, settings(), transport=workers.connection_transport)
            with connection_lock:
                connection_results.update(results)
                connection_inputs.update({
                    service: tuple(env.get(key) for key in connection_checks.CHECKS[service]["keys"])
                    for service in results
                })
                rows = connection_checks.describe(env, connection_results)
            return jsonify(connections=rows)
        finally:
            connection_run_lock.release()

    @app.post("/api/connections/send-telegram-test")
    def send_telegram_test():
        if not connection_run_lock.acquire(blocking=False):
            return jsonify(error="A connection test is already running."), 409
        try:
            env = connection_checks.current_environment() if not demo else {}
            if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
                return jsonify(result=connection_checks.result("not_set", "Telegram credentials are missing."))
            return jsonify(result=workers.connection_request(
                {"operation": "telegram", "env": {key: env[key] for key in
                 ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}, "cfg": {}}))
        finally:
            connection_run_lock.release()

    @app.get("/api/items/<item_id>")
    def detail(item_id):
        data = views.item_detail(path, item_id, settings())
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
        effective = settings()
        if body.get("action") == "go_live":
            validate_live(effective)
        with snapshot(path):
            plan = commands.prepare(
                str(body.get("action", "")),
                body.get("item_id"),
                body.get("payload", {}),
                path,
                effective,
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
        effective = settings()
        with submit_lock:
            with db.db(path) as conn:
                seen = conn.execute("SELECT id FROM dashboard_commands WHERE id=?", (command_id,)).fetchone()
            if plan["action"] in commands.GLOBAL_ACTIONS | {"snapshot_now"}:
                if seen:
                    with db.db(path) as conn:
                        command = dict(conn.execute("SELECT * FROM dashboard_commands WHERE id=?",
                                                    (command_id,)).fetchone())
                else:
                    command = confirm_operation(plan, command_id, effective)
                return jsonify(command=command), 202
            result = commands.submit(plan, command_id, path, effective, media_root=root)
        action = plan["action"]
        if not seen and result["status"] in ("pending", "applied"):
            try:
                active = runner.active_lock(path, runner._lock_settings(effective)[0])
                if action in creation.ACTIONS and (not active or active.get("kind") in ("dashboard", "make")):
                    workers.spawn(path, root, "make", "--job", command_id)
                if action == "post_now" and not active:
                    workers.control("start", path, effective, root)
            except (OSError, commands.ControlError) as error:
                if not runner.active_lock(path, runner._lock_settings(effective)[0]):
                    warning = f"Worker could not start: {error}"
                    if action in creation.ACTIONS:
                        with db.db(path) as conn:
                            conn.execute(
                                "UPDATE dashboard_commands SET status='failed', result=?, completed_at=? "
                                "WHERE id=? AND status='pending'", (warning[:500], db.now(), command_id))
                            result = dict(conn.execute("SELECT * FROM dashboard_commands WHERE id=?",
                                                       (command_id,)).fetchone())
                    return jsonify(command=result, warning=warning), 202
        undo = commands.undo_for(plan, command_id) if result["status"] == "applied" else None
        return jsonify(command=result, **({"undo": undo} if undo else {})), 202

    @app.get("/media/<item_id>/<variant>")
    def media(item_id, variant):
        if variant == "thumb":
            thumbnail = thumbnails.get(item_id)
            if thumbnail is not None:
                return send_file(thumbnail, mimetype="image/jpeg")
            return app.send_static_file("thumbnail.svg")
        column = {"a": "render_a_path", "b": "render_b_path", "cut": "cut_path"}.get(variant)
        with snapshot(path):
            if variant in ("p1", "p2", "p3"):
                file = next((row["path"] for row in db.renders_for_item(item_id, path)
                             if row["variant"] == variant), None)
            elif column:
                file = (db.get_item(item_id, path) or {}).get(column)
            else:
                abort(404)
        video = media_paths.video_path(file, root)
        if video is None:
            abort(404)
        return send_file(video, conditional=True, mimetype="video/mp4")

    return app


def redact_log(line, values):
    for value in sorted({value for value in values if value}, key=len, reverse=True):
        from urllib.parse import quote, quote_plus
        for form in {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]}:
            line = line.replace(form, "[redacted]")
    line = re.sub(r"(?i)(Bearer\s+)[^\s\"',;]+", r"\1[redacted]", line)
    line = re.sub(r"(?i)((?:[\w-]*(?:token|secret|password|api[_-]?key)|authorization)"
                  r"[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,;}]+", r"\1[redacted]", line)
    line = re.sub(r"\b(?:\d{6,}:)?[A-Za-z0-9_-]{32,}\b", "[redacted]", line)
    return line[:1200]
