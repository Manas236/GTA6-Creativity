"""Live crash recovery verification script.

Demonstrates:
1. Spawns real runner download process.
2. Kills mid-download by PID.
3. Proves DB row count unchanged, no duplicate rows, stage correct, temp file cleaned up.
4. Reruns immediately, proving stale lock is broken without blocking for 600s.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config, db


def run_live_crash_test() -> dict:
    db_file = Path("crash_proof.db")
    for f in [db_file, Path("crash_proof.db-wal"), Path("crash_proof.db-shm")]:
        f.unlink(missing_ok=True)

    db.init_db(db_file)

    # Use a real YouTube video ID from Launch Trailer
    item = {
        "id": "crash_test_item",
        "source": "youtube",
        "source_id": "rz1B4B2yBZY",
        "source_url": "https://www.youtube.com/watch?v=rz1B4B2yBZY",
        "author_name": "Rockstar Games",
        "raw_velocity": 1000.0,
        "percentile_rank": 0.9,
        "discovery_score": 90.0,
    }
    db.insert_discovered_item(item, db_path=db_file)

    print("--- 1. Launching real download process via CLI ---")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pipeline.runner",
            "--db",
            str(db_file),
            "download",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    print(f"Process spawned, PID: {proc.pid}")

    # Give it 3 seconds to acquire lock, start yt-dlp download, and write temp file
    time.sleep(3.0)

    print(f"--- 2. Killing process {proc.pid} mid-download by PID ---")
    proc.kill()
    proc.wait()
    print("Process successfully killed.")

    # Check state immediately after crash
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    row_count = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    dup_count = cur.execute("SELECT COUNT(*) FROM items GROUP BY source, source_id HAVING COUNT(*) > 1").fetchall()
    stage = cur.execute("SELECT stage FROM items WHERE id='crash_test_item'").fetchone()[0]
    con.close()

    temp_files_before = list(config.media_dir("downloads").glob("tmp_crash_test_item*"))

    print("State immediately after crash:")
    print(f"  Item row count: {row_count}")
    print(f"  Duplicate rows: {len(dup_count)}")
    print(f"  Item stage: {stage}")
    print(f"  Temp files present: {[f.name for f in temp_files_before]}")

    print("--- 3. Rerunning immediately ---")
    t_start = time.time()
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pipeline.runner",
            "--db",
            str(db_file),
            "download",
        ],
        capture_output=True,
        text=True,
    )
    duration = time.time() - t_start
    print(f"Rerun finished in {duration:.2f}s (lock was not blocked!)")
    print(f"Rerun stdout:\n{res.stdout}")
    if res.stderr:
        print(f"Rerun stderr:\n{res.stderr}")

    # Check state after rerun
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    row_count_after = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    dup_count_after = cur.execute("SELECT COUNT(*) FROM items GROUP BY source, source_id HAVING COUNT(*) > 1").fetchall()
    stage_after = cur.execute("SELECT stage FROM items WHERE id='crash_test_item'").fetchone()[0]
    con.close()

    temp_files_after = list(config.media_dir("downloads").glob("tmp_crash_test_item*"))
    final_file = config.media_dir("downloads") / "crash_test_item.mp4"

    print("Final state after resume:")
    print(f"  Item row count: {row_count_after} (unchanged: {row_count == row_count_after})")
    print(f"  Duplicate rows: {len(dup_count_after)}")
    print(f"  Item stage: {stage_after}")
    print(f"  Temp files remaining: {[f.name for f in temp_files_after]}")
    print(f"  Final file exists: {final_file.exists()}")

    # Cleanup test files
    for f in [db_file, Path("crash_proof.db-wal"), Path("crash_proof.db-shm")]:
        f.unlink(missing_ok=True)
    if final_file.exists():
        final_file.unlink(missing_ok=True)

    return {
        "row_count_unchanged": row_count == row_count_after,
        "no_duplicate_rows": len(dup_count_after) == 0,
        "stage_correct": stage_after == "downloaded",
        "temp_cleaned": len(temp_files_after) == 0,
        "lock_unblocked": duration < 60.0,
    }


import pytest


@pytest.mark.live
def test_live_crash_recovery() -> None:
    results = run_live_crash_test()
    assert all(results.values()), f"Crash test failed: {results}"


if __name__ == "__main__":
    results = run_live_crash_test()
    print("Test summary:", results)
    assert all(results.values()), f"Crash test failed: {results}"
