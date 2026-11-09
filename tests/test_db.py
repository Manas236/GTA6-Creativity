"""Unit tests for SQLite state store in WAL mode."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import db


def test_db_wal_mode(tmp_db: Path) -> None:
    with db.db(tmp_db) as conn:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0].lower() == "wal"


def test_insert_and_get_item(tmp_db: Path) -> None:
    item_data = {
        "id": "test_item_1",
        "source": "youtube",
        "source_id": "vid123",
        "source_url": "https://youtube.com/watch?v=vid123",
        "author_name": "Rockstar Channel",
        "original_title": "GTA 6 Official Clip",
        "original_duration": 45.0,
        "view_count": 50000,
        "raw_velocity": 1250.0,
        "percentile_rank": 0.95,
        "discovery_score": 118.75,
    }
    inserted = db.insert_discovered_item(item_data, db_path=tmp_db)
    assert inserted is True

    # Duplicate insert should not raise, returns True (or False if identical)
    inserted_dup = db.insert_discovered_item(item_data, db_path=tmp_db)
    # Row still exists and is updated
    item = db.get_item("test_item_1", db_path=tmp_db)
    assert item is not None
    assert item["source_id"] == "vid123"
    assert item["stage"] == "discovered"
    assert item["view_count"] == 50000


def test_stage_transitions(tmp_db: Path) -> None:
    item_data = {
        "id": "test_item_2",
        "source": "reddit",
        "source_id": "red456",
        "source_url": "https://reddit.com/r/GTA6/comments/red456",
        "author_name": "gta_fan",
        "raw_velocity": 50.0,
        "percentile_rank": 0.8,
        "discovery_score": 40.0,
    }
    db.insert_discovered_item(item_data, db_path=tmp_db)

    # Transition to downloaded
    ok = db.transition_stage(
        "test_item_2",
        "downloaded",
        extra_fields={"download_path": "/fake/path.mp4", "downloaded_at": db.now()},
        db_path=tmp_db,
    )
    assert ok is True
    updated = db.get_item("test_item_2", db_path=tmp_db)
    assert updated["stage"] == "downloaded"
    assert updated["download_path"] == "/fake/path.mp4"

    # Mark failed
    db.mark_failed("test_item_2", "render", "GPU memory error", db_path=tmp_db)
    failed = db.get_item("test_item_2", db_path=tmp_db)
    assert failed["stage"] == "failed"
    assert failed["error_stage"] == "render"
    assert failed["retry_count"] == 1


def test_fingerprints_crud(tmp_db: Path) -> None:
    item_data = {
        "id": "test_fp_item",
        "source": "youtube",
        "source_id": "fp_vid",
        "source_url": "https://youtube.com/watch?v=fp_vid",
        "author_name": "Test",
        "raw_velocity": 10.0,
        "percentile_rank": 0.5,
        "discovery_score": 10.0,
    }
    db.insert_discovered_item(item_data, db_path=tmp_db)

    hashes = [(0, 123456789), (1, 987654321)]
    db.save_fingerprints("test_fp_item", "download", hashes, db_path=tmp_db)

    stored = db.get_stored_fingerprints_by_item("download", db_path=tmp_db)
    assert "test_fp_item" in stored
    assert len(stored["test_fp_item"]) == 2
    assert stored["test_fp_item"][0] == (0, 123456789)


def test_buffer_counts(tmp_db: Path) -> None:
    assert db.get_ready_buffer_count(tmp_db) == 0
    item_data = {
        "id": "buf_item",
        "source": "youtube",
        "source_id": "buf_vid",
        "source_url": "https://youtube.com/watch?v=buf_vid",
        "author_name": "Test",
        "raw_velocity": 10.0,
        "percentile_rank": 0.5,
        "discovery_score": 10.0,
    }
    db.insert_discovered_item(item_data, db_path=tmp_db)
    assert db.get_ready_buffer_count(tmp_db) == 0

    db.transition_stage("buf_item", "rendered", db_path=tmp_db)
    assert db.get_ready_buffer_count(tmp_db) == 1


# ----------------------------------------------------------------------------- preview_only

def _preview_item(tmp_db: Path, item_id: str = "prev_item") -> str:
    db.insert_discovered_item({"id": item_id, "source": "youtube", "source_id": item_id,
                               "source_url": "https://example.invalid/p", "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage(item_id, "cut", db_path=tmp_db)
    assert db.set_preview_only(item_id, True, db_path=tmp_db)
    return item_id


@pytest.mark.parametrize("stage", db.RELEASE_STAGES)
def test_preview_only_item_can_never_enter_an_approval_or_posting_stage(tmp_db: Path, stage: str) -> None:
    import sqlite3
    iid = _preview_item(tmp_db)
    with pytest.raises(sqlite3.DatabaseError, match="preview_only"):
        db.transition_stage(iid, stage, db_path=tmp_db)
    assert db.get_item(iid, db_path=tmp_db)["stage"] == "cut"


def test_preview_only_item_cannot_get_a_posts_row(tmp_db: Path) -> None:
    import sqlite3
    iid = _preview_item(tmp_db)
    with pytest.raises(sqlite3.DatabaseError, match="preview_only"):
        with db.db(tmp_db) as conn:
            conn.execute(
                "INSERT INTO posts (item_id, platform, variant, hook_style, source, slot_datetime, status, "
                "created_at, updated_at) VALUES (?, 'instagram', 'A', 'question', 'youtube', ?, 'pending', ?, ?)",
                (iid, db.now(), db.now(), db.now()),
            )


def test_preview_only_flag_cannot_be_set_on_an_already_released_item(tmp_db: Path) -> None:
    import sqlite3
    db.insert_discovered_item({"id": "rel", "source": "youtube", "source_id": "rel",
                               "source_url": "https://example.invalid/r", "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage("rel", "approved", db_path=tmp_db)
    with pytest.raises(sqlite3.DatabaseError, match="preview_only"):
        db.set_preview_only("rel", True, db_path=tmp_db)


def test_preview_only_still_allows_pre_release_stages_and_clearing(tmp_db: Path) -> None:
    iid = _preview_item(tmp_db)
    db.transition_stage(iid, "failed", db_path=tmp_db)          # failing/rejecting is fine
    db.transition_stage(iid, "cut", db_path=tmp_db)
    assert db.set_preview_only(iid, False, db_path=tmp_db)      # tomorrow's real hooks run
    db.transition_stage(iid, "rendered", db_path=tmp_db)
    assert db.get_item(iid, db_path=tmp_db)["stage"] == "rendered"


def test_init_db_adds_preview_only_and_its_triggers_to_an_old_db(tmp_path: Path) -> None:
    import sqlite3
    path = tmp_path / "old.db"
    db.init_db(path)
    with sqlite3.connect(path) as conn:
        for t in ("items_preview_only_never_released", "posts_block_preview_only_insert",
                  "posts_block_preview_only_update"):
            conn.execute(f"DROP TRIGGER {t}")
        conn.execute("ALTER TABLE items DROP COLUMN preview_only")
    db.init_db(path)
    iid = _preview_item(path)
    with pytest.raises(sqlite3.DatabaseError, match="preview_only"):
        db.transition_stage(iid, "queued", db_path=path)

