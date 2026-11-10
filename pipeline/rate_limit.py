"""Rate limiting, error backoff, and per-source cooldown tracking."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import clock, db

log = logging.getLogger(__name__)


def is_source_paused(source: str, db_path: Any = None) -> tuple[bool, str]:
    """Check if a source is currently under cooldown."""
    with db.db(db_path) as conn:
        row = conn.execute("SELECT paused_until, last_error FROM rate_limits WHERE source = ?", (source,)).fetchone()
        if not row or not row["paused_until"]:
            return False, ""
        
        paused_until = db.parse_ts(row["paused_until"])
        now_dt = clock.utcnow()
        if now_dt < paused_until:
            rem_s = (paused_until - now_dt).total_seconds()
            return True, f"Paused for {rem_s / 3600:.1f}h more due to: {row['last_error']}"
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

