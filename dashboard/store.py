"""Short transactions and consistent, disposable read snapshots."""

from contextlib import contextmanager
import sqlite3
from pathlib import Path

from pipeline import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_commands (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    item_id TEXT,
    payload TEXT NOT NULL,
    expected TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


def initialize(path):
    db.init_db(path)
    with db.db(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(path):
    conn = db.get_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with db.connection_scope(conn, path):
            yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def snapshot(path):
    """Pipeline read helpers may pin launch clocks: those writes stay in RAM.

    SQLite backup sees a consistent WAL snapshot without blocking the runner for
    the duration of page rendering. No GET initializes or writes the real DB.
    """
    source = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10
    )
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        source.backup(memory)
        source.close()
        with db.connection_scope(memory, path):
            yield memory
    finally:
        source.close()
        memory.close()
