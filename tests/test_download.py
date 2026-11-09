"""Unit tests for downloader, ffprobe validation, atomic rename, and crash resume."""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pipeline import config, db, download


def test_verify_video_file(synthetic_video: Path, tmp_path: Path) -> None:
    ok, dur = download.verify_video_file(synthetic_video)
    assert ok is True
    assert dur > 2.0

    # Test corrupted/empty file
    bad_file = tmp_path / "corrupt.mp4"
    bad_file.write_bytes(b"not a valid video payload")
    ok_bad, _ = download.verify_video_file(bad_file)
    assert ok_bad is False


def test_download_single_item_mocked(
    synthetic_video: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Setup test item
    item_data = {
        "id": "dl_item_1",
        "source": "youtube",
        "source_id": "test_dl_vid",
        "source_url": "https://youtube.com/watch?v=test_dl_vid",
        "author_name": "TestCreator",
        "raw_velocity": 100.0,
        "percentile_rank": 0.8,
        "discovery_score": 80.0,
    }
    db.insert_discovered_item(item_data, db_path=tmp_db)

    # Mock yt_dlp download to copy synthetic_video to destination
    def mock_download_func(urls: list[str]) -> None:
        downloads_dir = config.media_dir("downloads")
        target_temp = downloads_dir / "tmp_dl_item_1.mp4"
        shutil.copyfile(synthetic_video, target_temp)

    mock_ydl = MagicMock()
    mock_ydl.download.side_effect = mock_download_func
    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", lambda *a, **k: MagicMock(__enter__=lambda s: mock_ydl, __exit__=lambda *a: None))

    item = db.get_item("dl_item_1", db_path=tmp_db)
    assert item is not None
    ok, msg = download.download_single_item(item, db_path=tmp_db)
    assert ok is True

    # Verify stage updated to 'downloaded'
    updated = db.get_item("dl_item_1", db_path=tmp_db)
    assert updated["stage"] == "downloaded"
    final_file = Path(updated["download_path"])
    assert final_file.exists()
    assert final_file.name == "dl_item_1.mp4"


def test_crash_recovery_missing_file_requeues(tmp_db: Path) -> None:
    # Item marked downloaded but file deleted from disk
    item_data = {
        "id": "crashed_item",
        "source": "youtube",
        "source_id": "crash_vid",
        "source_url": "https://youtube.com/watch?v=crash_vid",
        "author_name": "Test",
        "raw_velocity": 50.0,
        "percentile_rank": 0.5,
        "discovery_score": 25.0,
    }
    db.insert_discovered_item(item_data, db_path=tmp_db)
    db.transition_stage(
        "crashed_item",
        "downloaded",
        extra_fields={"download_path": "/nonexistent/video.mp4"},
        db_path=tmp_db,
    )

    # Run download pipeline: resume check must detect missing file and re-queue to discovered
    download.run_download_pipeline(max_items=0, db_path=tmp_db)
    rechecked = db.get_item("crashed_item", db_path=tmp_db)
    assert rechecked["stage"] == "discovered"
