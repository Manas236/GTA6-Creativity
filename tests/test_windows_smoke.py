"""The Windows smoke harness: download polling, the real-DB proof and the seeded approval clip."""

import sqlite3
import sys

import pytest

from dashboard import commands, store
from pipeline import config, db
from scripts import windows_smoke


def row(status, message=""):
    return {"status": status, "message": message, "heatmap": None}


def test_download_row_waits_through_start_and_fails_on_errors():
    assert windows_smoke.download_row_ready(None) is None
    assert windows_smoke.download_row_ready(row("start", "Starting yt-dlp download")) is None
    finished = row("success", "Downloaded clip.mp4 (90.0s)")
    assert windows_smoke.download_row_ready(finished) is finished
    with pytest.raises(RuntimeError, match="HTTP 403"):
        windows_smoke.download_row_ready(row("error", "HTTP 403"))


def test_download_polling_keeps_going_after_start(monkeypatch):
    monkeypatch.setattr(windows_smoke.time, "sleep", lambda _seconds: None)
    rows = iter([None, row("start"), row("start"), row("success", "done")])
    result = windows_smoke.wait_for(lambda: windows_smoke.download_row_ready(next(rows)), 5, "timed out")
    assert result["message"] == "done"


def make_db(path):
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (x)")
        connection.execute("INSERT INTO t VALUES (1)")
    return path


def test_database_check_passes_only_when_db_and_wal_are_unchanged(tmp_path):
    source = make_db(tmp_path / "pipeline.db")
    before = windows_smoke.database_fingerprint(source)
    assert set(before) == {"pipeline.db"}

    (tmp_path / "pipeline.db-shm").write_bytes(b"index")  # SQLite's index, ignored on purpose
    status, reason = windows_smoke.database_check(source, before, windows_smoke.database_fingerprint(source))
    assert status == "PASS" and reason == f"unchanged (sha256 {before['pipeline.db'][:12]})"

    (tmp_path / "pipeline.db-wal").write_bytes(b"")
    status, reason = windows_smoke.database_check(source, before, windows_smoke.database_fingerprint(source))
    assert status == "FAIL" and "pipeline.db-wal" in reason

    with_wal = windows_smoke.database_fingerprint(source)
    (tmp_path / "pipeline.db-wal").write_bytes(b"frame")
    status, reason = windows_smoke.database_check(source, with_wal, windows_smoke.database_fingerprint(source))
    assert status == "FAIL" and "pipeline.db-wal" in reason

    (tmp_path / "pipeline.db-wal").unlink()
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO t VALUES (2)")
    status, reason = windows_smoke.database_check(source, before, windows_smoke.database_fingerprint(source))
    assert status == "FAIL" and reason.startswith("changed: pipeline.db (sha256 ")


def test_database_check_fails_when_the_source_is_missing(tmp_path):
    missing = tmp_path / "pipeline.db"
    assert windows_smoke.database_check(missing, {}, {})[0] == "FAIL"


@pytest.mark.parametrize("mutate", [False, True])
def test_main_prints_real_db_row_and_exits_nonzero_on_change(tmp_path, monkeypatch, capsys, mutate):
    source = make_db(tmp_path / "pipeline.db")
    monkeypatch.setattr(windows_smoke, "PROJECT", tmp_path)
    monkeypatch.setattr(windows_smoke, "stage_sandbox", lambda sandbox, original: sandbox / "smoke-copy.db")

    def execute(smoke, youtube, twitch):
        for name in windows_smoke.CHECKS:
            smoke.results[name] = ("PASS", "stubbed")
        if mutate:
            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO t VALUES (3)")

    monkeypatch.setattr(windows_smoke, "execute", execute)
    monkeypatch.setattr(sys, "argv", ["windows_smoke.py", str(source)])
    code = windows_smoke.main()
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Real DB")]
    assert len(lines) == 1
    if mutate:
        assert code == 1 and "FAIL" in lines[0]
    else:
        assert code == 0 and "PASS    unchanged (sha256 " in lines[0]


def test_seeded_clip_lives_only_in_the_sandbox_copy_and_passes_real_controls(tmp_path):
    cfg = config.get_config()
    elsewhere = tmp_path / "pipeline.db"
    db.init_db(elsewhere)
    with pytest.raises(RuntimeError, match="outside the sandbox"):
        windows_smoke.seed_approved_item(tmp_path / "sandbox", elsewhere)

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    copy = sandbox / "smoke-copy.db"
    db.init_db(copy)
    store.initialize(copy)
    item_id = windows_smoke.seed_approved_item(sandbox, copy)
    assert db.get_item(item_id, copy)["stage"] == "approved"
    assert db.get_item(item_id, elsewhere) is None
    commands.prepare("unapprove", item_id, {}, copy, cfg)
    db.transition_stage(item_id, "awaiting_approval", db_path=copy)
    commands.prepare("approve", item_id, {}, copy, cfg)
