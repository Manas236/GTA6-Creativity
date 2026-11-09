"""Unit tests for per-source rate limiting and cooldown state."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import db, rate_limit


def test_rate_limit_lifecycle(tmp_db: Path) -> None:
    # Initially not paused
    paused, why = rate_limit.is_source_paused("youtube", db_path=tmp_db)
    assert paused is False
    assert why == ""

    # Record error with 2 hours pause
    rate_limit.record_error("youtube", "HTTP 429 Too Many Requests", pause_hours=2.0, db_path=tmp_db)
    paused, why = rate_limit.is_source_paused("youtube", db_path=tmp_db)
    assert paused is True
    assert "HTTP 429" in why

    # Record success resets cooldown
    rate_limit.record_success("youtube", db_path=tmp_db)
    paused, why = rate_limit.is_source_paused("youtube", db_path=tmp_db)
    assert paused is False


def test_rate_limit_expiration(tmp_db: Path) -> None:
    # Set paused_until to a past timestamp
    past_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
    past_ts = past_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    with db.db(tmp_db) as conn:
        conn.execute(
            """
            INSERT INTO rate_limits (source, paused_until, consecutive_errors, last_error, updated_at)
            VALUES ('youtube', ?, 1, 'Expired error', ?)
            """,
            (past_ts, db.now()),
        )

    paused, why = rate_limit.is_source_paused("youtube", db_path=tmp_db)
    assert paused is False

