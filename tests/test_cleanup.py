"""CP4 cleanup: retention per config, and never deleting what a pending/publishing post needs."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import cleanup, config, db, posting


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "app": {"dry_run": False},
        "retention": {"purge_downloads_immediately": True, "purge_renders_after_days": 7,
                      "purge_rejected_failed_after_days": 3},
        "posting": {"platforms": {"instagram": True, "facebook": True}, "max_post_attempts": 3},
    }


def make_item(tmp_db: Path, item_id: str, stage: str, when: str, fake_clock, files=None) -> dict[str, Any]:
    """An item with every media file on disk under the (tmp) media folder, moved to `stage` at `when`."""
    paths = {
        "download_path": config.media_dir("downloads") / f"{item_id}.mp4",
        "cut_path": config.media_dir("cuts") / f"{item_id}_cut.mp4",
        "voiceover_audio_path": config.media_dir("audio") / f"{item_id}_vo.wav",
        "render_a_path": config.media_dir("renders") / f"{item_id}_A.mp4",
        "render_b_path": config.media_dir("renders") / f"{item_id}_B.mp4",
    }
    if files is not None:
        paths = {k: v for k, v in paths.items() if k in files}
    for p in paths.values():
        p.write_bytes(b"x" * 1000)
    (config.media_dir("subtitles") / f"{item_id}_A.ass").write_text("subs")
    db.insert_discovered_item({"id": item_id, "source": "youtube", "source_id": item_id,
                               "source_url": f"https://x.test/{item_id}"}, db_path=tmp_db)
    fake_clock.set(when)
    db.transition_stage(item_id, stage, extra_fields={k: str(v) for k, v in paths.items()}, db_path=tmp_db)
    return db.get_item(item_id, db_path=tmp_db)


def add_post(tmp_db: Path, item_id: str, platform: str, status: str, posted_at: str | None = None, **extra) -> None:
    cols = {"item_id": item_id, "platform": platform, "variant": "A", "hook_style": "q", "source": "youtube",
            "slot_datetime": posted_at or "2026-11-19T07:30:00Z", "status": status, "posted_at": posted_at,
            "created_at": "2026-11-19T00:00:00Z", "updated_at": "2026-11-19T00:00:00Z", **extra}
    with db.db(tmp_db) as conn:
        conn.execute(f"INSERT INTO posts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     list(cols.values()))


def existing(item: dict[str, Any]) -> set[str]:
    return {c for c in cleanup.ALL_COLUMNS if item.get(c) and Path(item[c]).exists()}


def run(tmp_db, cfg, fake_clock, when: str) -> dict[str, Any]:
    fake_clock.set(when)
    return cleanup.run_cleanup_stage(cfg=cfg, db_path=tmp_db)


def test_the_source_is_deleted_once_rendered_and_nothing_else(tmp_db, cfg, fake_clock):
    before = make_item(tmp_db, "item-cut", "cut", "2026-11-19T00:00:00Z", fake_clock)
    rendered = make_item(tmp_db, "item-rendered", "rendered", "2026-11-19T00:00:00Z", fake_clock)

    out = run(tmp_db, cfg, fake_clock, "2026-11-19T00:01:00Z")

    assert out["deleted"] == [rendered["download_path"]]
    assert existing(before) == set(cleanup.ALL_COLUMNS)                       # not rendered yet: untouched
    assert existing(rendered) == set(cleanup.RENDER_COLUMNS)
    assert db.get_item("item-rendered", db_path=tmp_db)["download_path"] is None

    cfg["retention"]["purge_downloads_immediately"] = False                   # the rule is config
    kept = make_item(tmp_db, "item-keep", "rendered", "2026-11-19T00:02:00Z", fake_clock)
    run(tmp_db, cfg, fake_clock, "2026-11-19T00:03:00Z")
    assert "download_path" in existing(kept)


def test_renders_go_seven_days_after_the_post_and_the_item_becomes_cleaned(tmp_db, cfg, fake_clock):
    item = make_item(tmp_db, "item-1", "measured", "2026-11-22T08:00:00Z", fake_clock,
                     files=cleanup.RENDER_COLUMNS)
    add_post(tmp_db, "item-1", "instagram", "live", "2026-11-19T07:30:00Z")
    add_post(tmp_db, "item-1", "facebook", "live", "2026-11-19T07:31:00Z")
    subtitle = config.media_root() / "subtitles" / "item-1_A.ass"

    assert run(tmp_db, cfg, fake_clock, "2026-11-26T07:30:59Z")["deleted"] == []   # 7 days after the LAST live row
    assert existing(item) == set(cleanup.RENDER_COLUMNS)

    out = run(tmp_db, cfg, fake_clock, "2026-11-26T07:31:00Z")
    assert out["deleted_files"] == 5 and out["bytes_freed"] == 4004 and out["cleaned"] == ["item-1"]
    assert existing(item) == set() and not subtitle.exists()
    after = db.get_item("item-1", db_path=tmp_db)
    assert after["stage"] == "cleaned" and all(after[c] is None for c in cleanup.ALL_COLUMNS)
    assert run(tmp_db, cfg, fake_clock, "2026-11-27T00:00:00Z")["deleted"] == []   # idempotent


def test_a_posted_item_not_yet_measured_loses_its_files_but_waits_for_analytics(tmp_db, cfg, fake_clock):
    cfg["retention"]["purge_renders_after_days"] = 1
    item = make_item(tmp_db, "item-1", "posted", "2026-11-19T07:30:00Z", fake_clock, files=cleanup.RENDER_COLUMNS)
    add_post(tmp_db, "item-1", "instagram", "live", "2026-11-19T07:30:00Z")
    add_post(tmp_db, "item-1", "facebook", "live", "2026-11-19T07:30:00Z")
    out = run(tmp_db, cfg, fake_clock, "2026-11-20T08:00:00Z")
    assert existing(item) == set() and out["cleaned"] == []
    assert db.get_item("item-1", db_path=tmp_db)["stage"] == "posted"         # analytics still owes it a 72h snapshot
    db.transition_stage("item-1", "measured", db_path=tmp_db)
    assert run(tmp_db, cfg, fake_clock, "2026-11-22T09:00:00Z")["cleaned"] == ["item-1"]


def test_rejected_and_failed_media_goes_after_three_days(tmp_db, cfg, fake_clock):
    rejected = make_item(tmp_db, "item-rej", "rejected", "2026-11-19T00:00:00Z", fake_clock)
    failed = make_item(tmp_db, "item-fail", "failed", "2026-11-20T00:00:00Z", fake_clock)

    assert run(tmp_db, cfg, fake_clock, "2026-11-21T23:59:59Z")["deleted"] == []
    run(tmp_db, cfg, fake_clock, "2026-11-22T00:00:00Z")
    assert existing(rejected) == set() and existing(failed) == set(cleanup.ALL_COLUMNS)
    run(tmp_db, cfg, fake_clock, "2026-11-23T00:00:00Z")
    assert existing(failed) == set()
    assert db.get_item("item-rej", db_path=tmp_db)["stage"] == "rejected"     # the record stays, the bytes go
    assert run(tmp_db, cfg, fake_clock, "2026-12-01T00:00:00Z")["deleted"] == []


@pytest.mark.parametrize("status", ["pending", "uploading", "container_ready", "publishing"])
def test_nothing_a_pending_or_publishing_post_still_needs_is_ever_deleted(tmp_db, cfg, fake_clock, status):
    # Instagram went live 30 days ago; the Facebook row is still open. Both rules must hold off.
    posted = make_item(tmp_db, "item-posted", "posted", "2026-10-20T00:00:00Z", fake_clock,
                       files=cleanup.RENDER_COLUMNS)
    add_post(tmp_db, "item-posted", "instagram", "live", "2026-10-20T07:30:00Z")
    add_post(tmp_db, "item-posted", "facebook", status)
    failed = make_item(tmp_db, "item-failed", "failed", "2026-10-20T00:00:00Z", fake_clock,
                       files=cleanup.RENDER_COLUMNS)
    add_post(tmp_db, "item-failed", "instagram", status)

    out = run(tmp_db, cfg, fake_clock, "2026-11-19T00:00:00Z")

    assert out["deleted"] == [] and out["protected"] == ["item-failed", "item-posted"]
    assert existing(posted) == set(cleanup.RENDER_COLUMNS) and existing(failed) == set(cleanup.RENDER_COLUMNS)

    with db.db(tmp_db) as conn:                                               # the rows finish
        conn.execute("UPDATE posts SET status = 'live', posted_at = '2026-11-19T00:00:00Z' WHERE status = ?", (status,))
    out = run(tmp_db, cfg, fake_clock, "2026-11-26T00:00:01Z")                # 7 days after the last live row
    assert existing(posted) == set()


def test_a_failed_row_with_retries_left_protects_its_media_and_a_given_up_row_does_not(tmp_db, cfg, fake_clock):
    item = make_item(tmp_db, "item-1", "posted", "2026-10-20T00:00:00Z", fake_clock, files=cleanup.RENDER_COLUMNS)
    add_post(tmp_db, "item-1", "instagram", "live", "2026-10-20T07:30:00Z")
    add_post(tmp_db, "item-1", "facebook", "failed", attempt_count=1)
    assert run(tmp_db, cfg, fake_clock, "2026-11-19T00:00:00Z")["protected"] == ["item-1"]
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET no_retry = 1 WHERE platform = 'facebook'")
    assert run(tmp_db, cfg, fake_clock, "2026-11-19T00:00:00Z")["protected"] == []
    assert existing(item) == set()


def test_items_still_in_the_queue_are_never_touched(tmp_db, cfg, fake_clock):
    items = [make_item(tmp_db, f"item-{s}", s, "2026-10-01T00:00:00Z", fake_clock, files=cleanup.RENDER_COLUMNS)
             for s in ("rendered", "awaiting_approval", "approved", "queued")]
    assert run(tmp_db, cfg, fake_clock, "2026-12-01T00:00:00Z")["deleted"] == []
    assert all(existing(i) == set(cleanup.RENDER_COLUMNS) for i in items)


def test_only_files_inside_the_media_folder_are_deleted(tmp_db, tmp_path, cfg, fake_clock):
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"do not delete")
    db.insert_discovered_item({"id": "item-1", "source": "youtube", "source_id": "1", "source_url": "u"}, db_path=tmp_db)
    fake_clock.set("2026-11-01T00:00:00Z")
    db.transition_stage("item-1", "rejected", extra_fields={
        "render_a_path": str(outside),
        "render_b_path": str(config.media_dir("renders") / ".." / ".." / "precious.mp4"),
    }, db_path=tmp_db)
    out = run(tmp_db, cfg, fake_clock, "2026-12-01T00:00:00Z")
    assert out["deleted"] == [] and outside.read_bytes() == b"do not delete"
    assert db.get_item("item-1", db_path=tmp_db)["render_a_path"] == str(outside)   # pointer kept, visibly


def test_a_file_another_item_still_points_at_survives(tmp_db, cfg, fake_clock):
    shared = config.media_dir("dry_run") / "synthetic_A.mp4"
    shared.write_bytes(b"shared")
    for item_id, stage in (("synthetic-cp3-1", "rejected"), ("synthetic-cp3-2", "awaiting_approval")):
        db.insert_discovered_item({"id": item_id, "source": "youtube", "source_id": item_id, "source_url": "u"},
                                  db_path=tmp_db)
        fake_clock.set("2026-11-01T00:00:00Z")
        db.transition_stage(item_id, stage, extra_fields={"render_a_path": str(shared)}, db_path=tmp_db)
    run(tmp_db, cfg, fake_clock, "2026-12-01T00:00:00Z")
    assert shared.exists()
    assert db.get_item("synthetic-cp3-1", db_path=tmp_db)["render_a_path"] is None
    assert db.get_item("synthetic-cp3-2", db_path=tmp_db)["render_a_path"] == str(shared)


def test_preview_only_items_are_left_alone(tmp_db, cfg, fake_clock):
    item = make_item(tmp_db, "item-prev", "failed", "2026-10-01T00:00:00Z", fake_clock)
    db.set_preview_only("item-prev", True, db_path=tmp_db)
    assert run(tmp_db, cfg, fake_clock, "2026-12-01T00:00:00Z")["deleted"] == []
    assert existing(item) == set(cleanup.ALL_COLUMNS)


def test_retention_defaults_match_the_shipped_config(test_config):
    assert cleanup.retention(test_config) == {"source_after_render": True, "renders_days": 7.0,
                                              "rejected_failed_days": 3.0}
