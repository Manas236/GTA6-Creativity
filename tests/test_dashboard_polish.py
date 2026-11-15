import os
import time

import pytest

from dashboard.app import create_app
from dashboard.presentation import slot_time
from pipeline import cleanup, clock, db, media_paths
from scripts.windows_smoke import copy_database


@pytest.mark.parametrize(("when", "expected"), [
    ("2026-11-10T18:39:00Z", "Today 12:09 AM"),
    ("2026-11-12T07:30:00Z", "Tomorrow 1:00 PM"),
    ("2026-11-16T07:30:00Z", "Mon 16 Nov, 1:00 PM"),
])
def test_confirmation_slot_time_uses_profile_date(test_config, when, expected):
    test_config["posting"]["profile"] = "local"
    assert slot_time(clock.parse(when), test_config, now=clock.parse("2026-11-10T18:31:00Z")) == expected


@pytest.mark.parametrize(("platforms", "text", "warning"), [
    ({"instagram": True, "facebook": False}, "Will post to: Instagram", False),
    ({"instagram": False, "facebook": True}, "Will post to: Facebook", False),
    ({"instagram": True, "facebook": True}, "Will post to: Instagram + Facebook", False),
    ({}, "No posting platforms are enabled.", True),
])
def test_enabled_platform_line(tmp_db, test_config, platforms, text, warning):
    test_config["posting"]["platforms"] = platforms
    data = create_app(tmp_db, test_config).test_client().get("/api/state").get_json()
    assert data["posting_destination"] == {"text": text, "warning": warning}


def test_thumbnail_rejects_paths_outside_media(tmp_path, tmp_db, test_config, isolated_media):
    isolated_media.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")
    db.insert_discovered_item({"id": "outside", "source": "manual", "source_id": "outside", "source_url": "https://example.test/outside"}, tmp_db)
    db.transition_stage("outside", "cut", {"cut_path": str(outside)}, tmp_db)
    client = create_app(tmp_db, test_config).test_client()
    assert client.get("/media/outside/cut").status_code == 404
    assert client.get("/media/outside/thumb").mimetype == "image/svg+xml"
    assert not (isolated_media / "thumbs").exists()


def test_thumbnail_cache_rejects_symlink_redirect(tmp_path, isolated_media):
    isolated_media.mkdir()
    try:
        (isolated_media / "thumbs").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("This account cannot create symlinks")
    assert media_paths.thumbnail_path("item-id", isolated_media) is None


def test_thumbnail_falls_back_to_cut_and_is_purged(tmp_db, test_config, isolated_media, synthetic_video):
    isolated_media.mkdir()
    cut = synthetic_video.rename(isolated_media / "cut.mp4")
    broken_render = isolated_media / "broken.mp4"
    broken_render.write_bytes(b"unreadable")
    db.insert_discovered_item({"id": "test", "source": "manual", "source_id": "test", "source_url": "https://example.test/clip"}, tmp_db)
    db.transition_stage("test", "rendered", {"cut_path": str(cut), "render_a_path": str(broken_render)}, tmp_db)
    client = create_app(tmp_db, test_config).test_client()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with client.get("/media/test/thumb") as response:
            if response.mimetype == "image/jpeg":
                assert response.data.startswith(b"\xff\xd8")
                break
        time.sleep(0.05)
    else:
        pytest.fail("The background thumbnail worker did not produce a frame from the cut")
    cached = media_paths.thumbnail_path("test", isolated_media)
    assert cached.is_file()
    cleanup.Cleaner(test_config, tmp_db).purge(db.get_item("test", tmp_db), cleanup.ALL_COLUMNS)
    assert not cached.exists()
    assert client.get("/media/test/thumb").mimetype == "image/svg+xml"


def test_windows_smoke_refuses_original_db_and_live_config(tmp_path, tmp_db):
    original_bytes = tmp_db.read_bytes()
    with pytest.raises(RuntimeError, match="original DB"):
        copy_database(tmp_db, tmp_db, tmp_path, {"app": {"dry_run": True}})
    target = tmp_path / "smoke-copy.db"
    os.link(tmp_db, target)
    with pytest.raises(RuntimeError, match="hard link"):
        copy_database(tmp_db, target, tmp_path, {"app": {"dry_run": True}})
    target.unlink()
    with pytest.raises(RuntimeError, match="dry_run=true"):
        copy_database(tmp_db, target, tmp_path, {"app": {"dry_run": False}})
    assert not target.exists()
    copy_database(tmp_db, target, tmp_path, {"app": {"dry_run": True}})
    assert target.is_file() and not target.samefile(tmp_db)
    assert tmp_db.read_bytes() == original_bytes
