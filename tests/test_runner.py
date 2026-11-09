"""Unit tests for orchestrator runner lock and heartbeat management."""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from pipeline import db, runner


def test_lock_acquire_and_release(tmp_db: Path) -> None:
    ok = runner.acquire_lock(db_path=tmp_db, stale_timeout_s=120.0, heartbeat_interval_s=1.0)
    assert ok is True

    # Lock is currently held; immediate second acquire from same process should reflect active lock
    # (or second process simulated)
    runner.release_lock(db_path=tmp_db)
    raw = db.kv_get(runner.LOCK_KEY, db_path=tmp_db)
    assert raw == ""


def test_dead_pid_breaks_lock(tmp_db: Path) -> None:
    # Simulate a lock left by a dead PID on this host
    dead_pid = 999999
    payload = {
        "pid": dead_pid,
        "host": socket.gethostname(),
        "heartbeat": time.time(),  # fresh heartbeat, but dead PID!
        "started_at": time.time() - 10,
    }
    db.kv_set(runner.LOCK_KEY, json.dumps(payload), db_path=tmp_db)

    # acquire_lock must detect dead PID and break the lock immediately
    ok = runner.acquire_lock(db_path=tmp_db, stale_timeout_s=120.0)
    assert ok is True
    runner.release_lock(db_path=tmp_db)


def test_live_heartbeat_blocks_concurrent_acquire(tmp_db: Path) -> None:
    # Simulate an active live process
    current_pid = os.getpid()
    payload = {
        "pid": current_pid,
        "host": socket.gethostname(),
        "heartbeat": time.time(),
        "started_at": time.time(),
    }
    db.kv_set(runner.LOCK_KEY, json.dumps(payload), db_path=tmp_db)

    # When PID is alive and heartbeat is fresh, acquire must fail
    ok = runner.acquire_lock(db_path=tmp_db, stale_timeout_s=120.0)
    assert ok is False


def test_stale_heartbeat_breaks_lock(tmp_db: Path) -> None:
    # Heartbeat older than 120s
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "heartbeat": time.time() - 200.0,
        "started_at": time.time() - 300.0,
    }
    db.kv_set(runner.LOCK_KEY, json.dumps(payload), db_path=tmp_db)

    # Heartbeat is older than 120s: must break lock
    ok = runner.acquire_lock(db_path=tmp_db, stale_timeout_s=120.0)
    assert ok is True
    runner.release_lock(db_path=tmp_db)

