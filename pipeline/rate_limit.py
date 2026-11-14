"""Rate limiting, error backoff, and per-source cooldown tracking."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import clock, db

log = logging.getLogger(__name__)


META_SOURCE = "meta_graph"
_DASHBOARD_META_PAUSE = "dashboard:meta_pause"
_DASHBOARD_PAUSE_REASON = "Paused by dashboard; resume from the control panel"
# Older dashboards used a year-9999 API cooldown to signal an indefinite pause.
_LEGACY_DASHBOARD_PAUSE = "paused_until >= '9998' AND last_error = ?"


def dashboard_meta_paused(db_path: Any = None) -> bool:
    if db.kv_get(_DASHBOARD_META_PAUSE, db_path=db_path) == "1":
        return True
    with db.db(db_path) as conn:
        return conn.execute(
            f"SELECT 1 FROM rate_limits WHERE source = ? AND {_LEGACY_DASHBOARD_PAUSE}",
            (META_SOURCE, _DASHBOARD_PAUSE_REASON),
        ).fetchone() is not None


def set_dashboard_meta_pause(paused: bool, db_path: Any = None) -> None:
    with db.db(db_path) as conn:
        with db.connection_scope(conn, db_path):
            db.kv_set(_DASHBOARD_META_PAUSE, "1" if paused else "0", db_path)
            conn.execute(
                f"UPDATE rate_limits SET paused_until = NULL, consecutive_errors = 0, "
                f"updated_at = ? WHERE source = ? AND {_LEGACY_DASHBOARD_PAUSE}",
                (db.now(), META_SOURCE, _DASHBOARD_PAUSE_REASON),
            )


def active_cooldown(source: str, db_path: Any = None) -> tuple[str | None, str]:
    """Return an active provider deadline and reason, excluding the legacy dashboard sentinel."""
    with db.db(db_path) as conn:
        row = conn.execute(
            "SELECT paused_until, last_error FROM rate_limits WHERE source = ?", (source,)
        ).fetchone()
    if not row or not row["paused_until"]:
        return None, ""
    deadline = row["paused_until"]
    if source == META_SOURCE and deadline >= "9998" and row["last_error"] == _DASHBOARD_PAUSE_REASON:
        return None, ""
    if db.parse_ts(deadline) <= clock.utcnow():
        return None, ""
    return deadline, row["last_error"] or ""


def is_source_paused(source: str, db_path: Any = None) -> tuple[bool, str]:
    """Check the dashboard hold and provider cooldown for Meta, or just the source cooldown."""
    if source == META_SOURCE and dashboard_meta_paused(db_path):
        return True, _DASHBOARD_PAUSE_REASON
    deadline, reason = active_cooldown(source, db_path)
    if deadline:
        rem_s = (db.parse_ts(deadline) - clock.utcnow()).total_seconds()
        return True, f"Paused for {rem_s / 3600:.1f}h more due to: {reason}"
    return False, ""


def record_error(
    source: str, error_msg: str, pause_hours: float = 1.0, db_path: Any = None
) -> None:
    """Record an error and pause the source."""
    now_dt = clock.utcnow()
    paused_until_dt = now_dt + timedelta(hours=pause_hours)
    paused_until_ts = paused_until_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    ts = db.now()

    with db.db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO rate_limits (source, paused_until, consecutive_errors, last_error, updated_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                paused_until = excluded.paused_until,
                consecutive_errors = consecutive_errors + 1,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (source, paused_until_ts, error_msg[:500], ts),
        )
    log.warning("Source %s paused until %s: %s", source, paused_until_ts, error_msg)


def record_success(source: str, db_path: Any = None) -> None:
    """Reset error count and clear pause on successful operation."""
    ts = db.now()
    with db.db(db_path) as conn:
        conn.execute(
            """
            UPDATE rate_limits SET 
                paused_until = NULL, 
                consecutive_errors = 0, 
                updated_at = ? 
            WHERE source = ?
            """,
            (ts, source),
        )

