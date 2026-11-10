"""Unit tests for orchestrator runner lock and heartbeat management."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("command", ["hooks", "voiceover", "render"])
def test_stage_commands_migrate_an_old_db_before_running(tmp_path: Path, monkeypatch, command: str) -> None:
    """An existing pipeline.db from before items.transcript_words must be migrated, not crash."""
    path = tmp_path / "old.db"
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE items DROP COLUMN transcript_words")   # simulate the old schema
    monkeypatch.setattr("sys.argv", ["runner", "--db", str(path), command])
    runner.main()
    with sqlite3.connect(path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    assert "transcript_words" in cols


@pytest.mark.parametrize("command", ["hooks", "voiceover", "render"])
def test_stage_commands_work_on_a_brand_new_db(tmp_path: Path, monkeypatch, command: str) -> None:
    path = tmp_path / "fresh.db"                                          # does not exist yet
    monkeypatch.setattr("sys.argv", ["runner", "--db", str(path), command])
    runner.main()                                                         # no 'no such table: kv'
    assert path.exists()



# ---------------------------------------------------------------------------
# CP4: isolated stages and the long-running loop
# ---------------------------------------------------------------------------

def _stages(calls: list[str], failing: set[str] = frozenset()):
    def make(name: str):
        def stage(cfg, db_path):
            calls.append(name)
            if name in failing:
                raise RuntimeError(f"{name} blew up")
            return {"ok": name}
        return stage
    return [(n, make(n)) for n in ("discover", "download", "post", "cleanup")]


def test_one_failing_stage_never_stops_the_others(tmp_db: Path, monkeypatch) -> None:
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(runner.telegram_bot, "send_alert",
                        lambda text, key=None, **kw: alerts.append((text, key)) or True)
    calls: list[str] = []

    result = runner.run_stages({}, tmp_db, stages=_stages(calls, failing={"download"}))

    assert calls == ["discover", "download", "post", "cleanup"]          # every stage still ran, in order
    assert result["status"] == "partial"
    assert list(result["errors"]) == ["download"] and "blew up" in result["errors"]["download"]
    assert set(result["stages"]) == {"discover", "post", "cleanup"}
    assert alerts == [("Pipeline stage 'download' failed: RuntimeError: download blew up", "stage_failed:download")]


def test_the_cycle_runs_every_stage_in_pipeline_order() -> None:
    names = [n for n, _ in runner.STAGES]
    order = ["settle", "discover", "download", "cut", "render", "approve", "schedule", "post"]
    assert [n for n in names if n in order] == order
    assert names.index("schedule") < names.index("post")                 # slots are assigned before posting


def test_the_loop_holds_the_lock_for_its_whole_life_and_releases_it(tmp_db: Path, monkeypatch) -> None:
    seen_lock: list[bool] = []
    calls: list[str] = []

    def stage(cfg, db_path):
        calls.append("tick")
        raw = json.loads(db.kv_get(runner.LOCK_KEY, db_path=db_path))
        seen_lock.append(raw["pid"] == os.getpid())
        # a second runner started now is refused
        assert runner.run_single_cycle(cfg={}, db_path=db_path) == {"status": "locked"}

    monkeypatch.setattr(runner, "STAGES", [("only", stage)])
    slept: list[float] = []
    n = runner.run_loop(cfg={}, db_path=tmp_db, interval_s=7, max_cycles=3, sleep=slept.append)

    assert n == 3 and calls == ["tick"] * 3 and seen_lock == [True] * 3
    assert slept == [7, 7]                                               # between cycles, not after the last
    assert db.kv_get(runner.LOCK_KEY, db_path=tmp_db) == ""              # released on exit


def test_the_loop_survives_failing_cycles_and_does_not_start_when_locked(tmp_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(runner.telegram_bot, "send_alert", lambda *a, **k: True)
    count = {"n": 0}

    def flaky(cfg, db_path):
        count["n"] += 1
        raise RuntimeError("always")

    monkeypatch.setattr(runner, "STAGES", [("flaky", flaky)])
    assert runner.run_loop(cfg={}, db_path=tmp_db, interval_s=0, max_cycles=4, sleep=lambda s: None) == 4
    assert count["n"] == 4

    payload = {"pid": os.getpid(), "host": socket.gethostname(), "heartbeat": time.time(), "started_at": time.time()}
    db.kv_set(runner.LOCK_KEY, json.dumps(payload), db_path=tmp_db)
    assert runner.run_loop(cfg={}, db_path=tmp_db, max_cycles=1, sleep=lambda s: None) == 0
    assert count["n"] == 4
