"""SQLite database in WAL mode for persistent pipeline state."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Iterator

from . import clock
from .config import project_root

DEFAULT_DB_PATH = project_root() / "pipeline.db"

STAGES = [
    "discovered",
    "downloaded",
    "cut",
    "rendered",
    "awaiting_approval",
    "approved",
    "queued",
    "posted",
    "measured",
    "cleaned",
    "rejected",
    "failed",
]

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    channel_id TEXT,
    source_url TEXT NOT NULL,
    author_name TEXT NOT NULL,
    original_title TEXT,
    original_duration REAL,
    view_count INTEGER DEFAULT 0,
    upvote_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    upload_time TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    raw_velocity REAL NOT NULL,
    percentile_rank REAL NOT NULL,
    discovery_score REAL NOT NULL,
    download_path TEXT,
    downloaded_at TEXT,
    is_duplicate INTEGER DEFAULT 0,
    rejection_reason TEXT,
    cut_start REAL,
    cut_end REAL,
    cut_duration REAL,
    cut_path TEXT,
    hook_style TEXT,
    hook_text TEXT,
    hook_grounded INTEGER DEFAULT 1,
    authenticity TEXT DEFAULT 'unverified',
    keywords TEXT,                             -- C1: JSON array of 3-6 caption keywords
    style_preset TEXT,                          -- style preset the renders were made with
    transcript_words TEXT,                      -- JSON {cut_start, cut_end, model, words}: cut transcript cached by the hooks stage
    preview_only INTEGER DEFAULT 0,             -- 1 = style-preview data only: triggers block approval/posting
    voiceover_script TEXT,
    voiceover_audio_path TEXT,
    caption_text TEXT,
    render_a_path TEXT,
    render_b_path TEXT,
    chosen_variant TEXT,
    approval_type TEXT,
    ab_excluded INTEGER DEFAULT 0,
    stage TEXT NOT NULL,
    error_stage TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    stage_updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_stage ON items(stage);
CREATE INDEX IF NOT EXISTS idx_items_discovery_score ON items(discovery_score DESC);
CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url);

CREATE TABLE IF NOT EXISTS fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    second_offset INTEGER NOT NULL,
    phash_uint64 INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_item_scope ON fingerprints(item_id, scope);
CREATE INDEX IF NOT EXISTS idx_fingerprints_hash ON fingerprints(phash_uint64);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    variant TEXT NOT NULL,
    hook_style TEXT NOT NULL,
    source TEXT NOT NULL,
    authenticity TEXT DEFAULT 'unverified',
    slot_datetime TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_container_id TEXT,
    remote_id TEXT,
    remote_url TEXT,
    ab_excluded INTEGER DEFAULT 0,
    attempt_count INTEGER DEFAULT 0,
    publish_started_at TEXT,
    caption_text TEXT,
    no_retry INTEGER DEFAULT 0,                 -- 1 = failed for good: never retried (e.g. unknown remote state)
    style_preset TEXT,                          -- captured for learning (items.style_preset at scheduling)
    error_message TEXT,
    posted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
    UNIQUE(item_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_slot_datetime ON posts(slot_datetime);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    snapshot_hours INTEGER NOT NULL,
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    raw_json TEXT,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE,
    UNIQUE(post_id, snapshot_hours)
);

CREATE TABLE IF NOT EXISTS ab_learning (
    dimension TEXT NOT NULL,
    dimension_value TEXT NOT NULL,
    sample_count INTEGER DEFAULT 0,
    normalized_score REAL DEFAULT 0.0,
    weight REAL DEFAULT 1.0,
    rebuilt_at TEXT NOT NULL,
    PRIMARY KEY(dimension, dimension_value)
);

CREATE TABLE IF NOT EXISTS rate_limits (
    source TEXT PRIMARY KEY,
    paused_until TEXT,
    consecutive_errors INTEGER DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stage_log_item ON stage_log(item_id);
"""

#: Stages a preview_only item may never enter (anything that leads to approval/posting).
RELEASE_STAGES = ("rendered", "awaiting_approval", "approved", "queued", "posted", "measured")

# Enforced in SQLite itself so no code path (runner, CP3 bot, ad-hoc SQL) can release a
# preview_only item: it cannot enter a release stage and cannot get a posts row.
PREVIEW_ONLY_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS items_preview_only_never_released
BEFORE UPDATE OF stage, preview_only ON items
WHEN NEW.preview_only = 1 AND NEW.stage IN ({", ".join(f"'{s}'" for s in RELEASE_STAGES)})
BEGIN
    SELECT RAISE(ABORT, 'preview_only item cannot enter an approval/posting stage');
END;

CREATE TRIGGER IF NOT EXISTS posts_block_preview_only_insert
BEFORE INSERT ON posts
WHEN (SELECT preview_only FROM items WHERE id = NEW.item_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'preview_only item cannot be posted');
END;

CREATE TRIGGER IF NOT EXISTS posts_block_preview_only_update
BEFORE UPDATE ON posts
WHEN (SELECT preview_only FROM items WHERE id = NEW.item_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'preview_only item cannot be posted');
END;
"""


def now() -> str:
    """Return ISO-8601 UTC timestamp (from pipeline.clock, so tests can fake time)."""
    return clock.now_iso()


def parse_ts(ts: str) -> datetime:
    """Parse ISO-8601 UTC timestamp."""
    clean = ts.replace("Z", "+00:00")
    return datetime.fromisoformat(clean)


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    target_path = Path(db_path or DEFAULT_DB_PATH)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def db(db_path: Path | str | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite connection with automatic commit/rollback."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    """Initialize SQLite database with schema in WAL mode."""
    with db(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migrate channel_id and authenticity if missing in existing table
        cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
        if "channel_id" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN channel_id TEXT")
        if "authenticity" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN authenticity TEXT DEFAULT 'unverified'")
        # Style layer (C1 keyword cache, rendered preset)
        if "keywords" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN keywords TEXT")
        if "style_preset" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN style_preset TEXT")
        # Cut-window transcript cached by the hooks stage and reused by render
        if "transcript_words" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN transcript_words TEXT")
        if "preview_only" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN preview_only INTEGER DEFAULT 0")
        # Created after the migration: the trigger bodies reference items.preview_only.
        conn.executescript(PREVIEW_ONLY_TRIGGERS)
        if "ab_excluded" not in cols:
            conn.execute("ALTER TABLE items ADD COLUMN ab_excluded INTEGER DEFAULT 0")
        post_cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
        if "authenticity" not in post_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN authenticity TEXT DEFAULT 'unverified'")
        # CP3 two-phase posting bookkeeping
        for col, ddl in (
            ("ab_excluded", "ALTER TABLE posts ADD COLUMN ab_excluded INTEGER DEFAULT 0"),
            ("attempt_count", "ALTER TABLE posts ADD COLUMN attempt_count INTEGER DEFAULT 0"),
            ("publish_started_at", "ALTER TABLE posts ADD COLUMN publish_started_at TEXT"),
            ("caption_text", "ALTER TABLE posts ADD COLUMN caption_text TEXT"),
            # CP4
            ("no_retry", "ALTER TABLE posts ADD COLUMN no_retry INTEGER DEFAULT 0"),
            ("style_preset", "ALTER TABLE posts ADD COLUMN style_preset TEXT"),
        ):
            if col not in post_cols:
                conn.execute(ddl)


# ---------------------------------------------------------------------------
# Items Management
# ---------------------------------------------------------------------------

def insert_discovered_item(item_data: dict[str, Any], db_path: Path | str | None = None) -> bool:
    """Insert newly discovered item. Returns True if inserted, False if already exists."""
    ts = now()
    with db(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO items (
                id, source, source_id, channel_id, source_url, author_name, original_title,
                original_duration, view_count, upvote_count, comment_count,
                upload_time, discovered_at, raw_velocity, percentile_rank,
                discovery_score, stage, stage_updated_at, created_at, updated_at
            ) VALUES (
                :id, :source, :source_id, :channel_id, :source_url, :author_name, :original_title,
                :original_duration, :view_count, :upvote_count, :comment_count,
                :upload_time, :discovered_at, :raw_velocity, :percentile_rank,
                :discovery_score, 'discovered', :stage_updated_at, :created_at, :updated_at
            )
            ON CONFLICT(source, source_id) DO UPDATE SET
                channel_id = COALESCE(excluded.channel_id, items.channel_id),
                view_count = excluded.view_count,
                upvote_count = excluded.upvote_count,
                comment_count = excluded.comment_count,
                raw_velocity = excluded.raw_velocity,
                percentile_rank = excluded.percentile_rank,
                discovery_score = excluded.discovery_score,
                updated_at = excluded.updated_at
            WHERE stage = 'discovered'
            """,
            {
                "id": item_data["id"],
                "source": item_data["source"],
                "source_id": item_data["source_id"],
                "channel_id": item_data.get("channel_id", ""),
                "source_url": item_data["source_url"],
                "author_name": item_data.get("author_name", "Creator"),
                "original_title": item_data.get("original_title", ""),
                "original_duration": item_data.get("original_duration", 0.0),
                "view_count": item_data.get("view_count", 0),
                "upvote_count": item_data.get("upvote_count", 0),
                "comment_count": item_data.get("comment_count", 0),
                "upload_time": item_data.get("upload_time", ts),
                "discovered_at": ts,
                "raw_velocity": item_data.get("raw_velocity", 0.0),
                "percentile_rank": item_data.get("percentile_rank", 0.5),
                "discovery_score": item_data.get("discovery_score", 0.0),
                "stage_updated_at": ts,
                "created_at": ts,
                "updated_at": ts,
            },
        )
        return cursor.rowcount > 0


def get_item(item_id: str, db_path: Path | str | None = None) -> dict[str, Any] | None:
    with db(db_path) as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


#: SQL that keeps only items that may be approved / scheduled / posted. Used explicitly by the
#: approval and posting queries - they do not rely on the preview_only triggers firing.
SYNTHETIC_PREFIX = "synthetic-"   # ids of the items scripts/cp3_dry_run.py builds for its test card
RELEASABLE_SQL = f"COALESCE(preview_only, 0) = 0 AND id NOT LIKE '{SYNTHETIC_PREFIX}%'"


def is_synthetic(item: dict[str, Any] | None) -> bool:
    return bool(item) and str(item.get("id") or "").startswith(SYNTHETIC_PREFIX)


def is_releasable(item: dict[str, Any] | None) -> bool:
    """False for an item that must never reach approval or posting: preview_only items and the
    synthetic test items the dry-run scripts create."""
    return bool(item) and not int(item.get("preview_only") or 0) and not is_synthetic(item)


def is_trigger_refusal(exc: BaseException) -> bool:
    """True when `exc` is one of the preview_only triggers refusing a write (RAISE(ABORT))."""
    return isinstance(exc, sqlite3.IntegrityError) and "preview_only" in str(exc)


def get_items_by_stage(
    stage: str,
    limit: int | None = None,
    order_by_score: bool = True,
    db_path: Path | str | None = None,
    releasable_only: bool = False,
) -> list[dict[str, Any]]:
    order_clause = "ORDER BY discovery_score DESC" if order_by_score else "ORDER BY created_at ASC"
    limit_clause = f"LIMIT {limit}" if limit else ""
    where = f"AND {RELEASABLE_SQL}" if releasable_only else ""
    with db(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM items WHERE stage = ? {where} {order_clause} {limit_clause}", (stage,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_ready_buffer_count(db_path: Path | str | None = None) -> int:
    """Return count of items in ready or pipeline queue stages."""
    with db(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM items 
            WHERE stage IN ('rendered', 'awaiting_approval', 'approved', 'queued')
              AND id NOT LIKE 'synthetic-%'
            """
        ).fetchone()
        return int(row[0]) if row else 0


def transition_stage(
    item_id: str,
    to_stage: str,
    extra_fields: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Transition an item to a new stage."""
    ts = now()
    fields = {"stage": to_stage, "stage_updated_at": ts, "updated_at": ts}
    if extra_fields:
        fields.update(extra_fields)

    set_clauses = [f"{k} = :{k}" for k in fields]
    fields["id"] = item_id

    with db(db_path) as conn:
        cursor = conn.execute(
            f"UPDATE items SET {', '.join(set_clauses)} WHERE id = :id",
            fields,
        )
        return cursor.rowcount > 0


def set_preview_only(item_id: str, preview_only: bool, db_path: Path | str | None = None) -> bool:
    """Flag (or unflag) an item as preview-only. While set, the DB triggers refuse to move it
    into any RELEASE_STAGES stage or to create/update a posts row for it."""
    ts = now()
    with db(db_path) as conn:
        cur = conn.execute(
            "UPDATE items SET preview_only = ?, updated_at = ? WHERE id = ?",
            (1 if preview_only else 0, ts, item_id),
        )
        return cur.rowcount > 0


def mark_failed(
    item_id: str, error_stage: str, error_message: str, db_path: Path | str | None = None
) -> None:
    ts = now()
    with db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET 
                stage = 'failed',
                error_stage = ?,
                error_message = ?,
                retry_count = retry_count + 1,
                stage_updated_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (error_stage, error_message[:1000], ts, ts, item_id),
        )


def mark_rejected(
    item_id: str, reason: str, is_duplicate: bool = False, db_path: Path | str | None = None
) -> None:
    ts = now()
    with db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET 
                stage = 'rejected',
                rejection_reason = ?,
                is_duplicate = ?,
                stage_updated_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (reason, 1 if is_duplicate else 0, ts, ts, item_id),
        )


def log_stage(
    item_id: str, stage: str, status: str, message: str = "", duration_s: float = 0.0, db_path: Path | str | None = None
) -> None:
    ts = now()
    with db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO stage_log (item_id, stage, status, message, duration_s, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, stage, status, message[:500], duration_s, ts),
        )


# ---------------------------------------------------------------------------
# Fingerprint Operations
# ---------------------------------------------------------------------------

def to_signed_int64(val: int) -> int:
    """Convert unsigned 64-bit integer to signed 64-bit integer for SQLite."""
    return val if val < (1 << 63) else val - (1 << 64)


def to_unsigned_int64(val: int) -> int:
    """Convert signed 64-bit integer back to unsigned 64-bit integer."""
    return val if val >= 0 else val + (1 << 64)


def save_fingerprints(
    item_id: str, scope: str, hashes: list[tuple[int, int]], db_path: Path | str | None = None
) -> None:
    """Save (second_offset, phash_uint64) tuples for an item."""
    ts = now()
    records = [(item_id, scope, sec, to_signed_int64(h), ts) for sec, h in hashes]
    with db(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO fingerprints (item_id, scope, second_offset, phash_uint64, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            records,
        )


def delete_fingerprints(item_id: str, scope: str, db_path: Path | str | None = None) -> int:
    """Remove an item's fingerprints for one scope (a re-cut replaces its cut hashes)."""
    with db(db_path) as conn:
        cur = conn.execute("DELETE FROM fingerprints WHERE item_id = ? AND scope = ?", (item_id, scope))
        return cur.rowcount


def get_stored_fingerprints_by_item(
    scope: str, exclude_item_id: str | None = None, db_path: Path | str | None = None
) -> dict[str, list[tuple[int, int]]]:
    """Retrieve all fingerprints for a given scope grouped by item_id: {item_id: [(sec, hash), ...]}."""
    with db(db_path) as conn:
        query = "SELECT item_id, second_offset, phash_uint64 FROM fingerprints WHERE scope = ?"
        params: list[Any] = [scope]
        if exclude_item_id:
            query += " AND item_id != ?"
            params.append(exclude_item_id)
        query += " ORDER BY item_id, second_offset ASC"

        rows = conn.execute(query, params).fetchall()
        result: dict[str, list[tuple[int, int]]] = {}
        for r in rows:
            result.setdefault(r["item_id"], []).append(
                (r["second_offset"], to_unsigned_int64(r["phash_uint64"]))
            )
        return result


# ---------------------------------------------------------------------------
# Rate Limiting & KV Store
# ---------------------------------------------------------------------------

def kv_get(key: str, default: str | None = None, db_path: Path | str | None = None) -> str | None:
    with db(db_path) as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def kv_set(key: str, value: str, db_path: Path | str | None = None) -> None:
    ts = now()
    with db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, ts),
        )


def get_recent_source_velocities(
    source: str, limit: int = 100, db_path: Path | str | None = None
) -> list[float]:
    """Return raw velocities for recent items discovered from a source."""
    with db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT raw_velocity FROM items
            WHERE source = ? AND raw_velocity > 0
            ORDER BY created_at DESC LIMIT ?
            """,
            (source, limit),
        ).fetchall()
        return [float(r["raw_velocity"]) for r in rows]


# ---------------------------------------------------------------------------
# Launch clock: "week 1" and the approval switch both count from the first live post
# ---------------------------------------------------------------------------

K_FIRST_LIVE = "launch:first_live_at"


def first_live_at(db_path: Path | str | None = None) -> str | None:
    """ISO time of the channel's first live post, or None before launch.

    Read from `posts` the first time and then pinned in `kv`, so later cleanup or a failed row can
    never move the start of week 1.
    """
    pinned = kv_get(K_FIRST_LIVE, db_path=db_path)
    if pinned:
        return pinned
    with db(db_path) as conn:
        row = conn.execute(
            "SELECT MIN(posted_at) AS first FROM posts WHERE status = 'live' AND posted_at IS NOT NULL"
        ).fetchone()
    first = row["first"] if row else None
    if first:
        kv_set(K_FIRST_LIVE, first, db_path=db_path)
    return first


def days_since_first_live(db_path: Path | str | None = None) -> float | None:
    """Days elapsed since the first live post (None before launch)."""
    first = first_live_at(db_path)
    if not first:
        return None
    return (clock.utcnow() - clock.parse(first)).total_seconds() / 86400.0
