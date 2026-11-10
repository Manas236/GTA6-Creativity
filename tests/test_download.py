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



# ---------------------------------------------------------------------------
# Format selector: 1080p H.264 cap, AV1 never picked (HANDOFF known issue 1)
# ---------------------------------------------------------------------------

def _fmt(fid: str, vcodec: str | None, height: int | None, acodec: str | None = "none", ext: str = "mp4") -> dict:
    f = {"format_id": fid, "ext": ext, "url": "https://x.test/" + fid, "protocol": "https"}
    if vcodec is not None:
        f["vcodec"] = vcodec
    if acodec is not None:
        f["acodec"] = acodec
    if height:
        f["height"] = height
        f["width"] = height * 16 // 9
    return f


AUDIO_M4A = _fmt("140", "none", None, "mp4a.40.2", "m4a")
AUDIO_OPUS = _fmt("251", "none", None, "opus", "webm")


def _pick(formats: list[dict], cfg: dict | None = None) -> str | None:
    """Run yt-dlp's real selector offline. `formats` is ordered worst -> best, as yt-dlp sorts them."""
    import yt_dlp

    selector = yt_dlp.YoutubeDL({"quiet": True}).build_format_selector(download.format_selector(cfg))
    picked = list(selector({"formats": formats, "incomplete_formats": False, "has_merged_format": True}))
    return picked[0]["format_id"] if picked else None


def test_selector_never_picks_av1_1440p_when_h264_1080p_exists() -> None:
    formats = [
        AUDIO_OPUS, AUDIO_M4A,
        _fmt("136", "avc1.4d401f", 720),
        _fmt("137", "avc1.640028", 1080),
        _fmt("248", "vp09.00.40.08", 1080, ext="webm"),
        _fmt("399", "av01.0.08M.08", 1080),
        _fmt("271", "vp09.00.50.08", 1440, ext="webm"),
        _fmt("400", "av01.0.12M.08", 1440),          # what the old selector picked
    ]
    assert _pick(formats) == "137+140"               # H.264 1080p + AAC


def test_selector_caps_h264_at_1080p() -> None:
    formats = [AUDIO_M4A, _fmt("136", "avc1.4d401f", 720), _fmt("266", "avc1.640033", 2160)]
    assert _pick(formats) == "136+140"


def test_selector_falls_back_to_h264_with_any_audio_then_progressive_h264() -> None:
    assert _pick([AUDIO_OPUS, _fmt("137", "avc1.640028", 1080)]) == "137+251"
    assert _pick([_fmt("18", "avc1.42001E", 360, "mp4a.40.2"), _fmt("400", "av01.0.12M.08", 1440)]) == "18"


def test_selector_falls_back_to_vp9_but_still_not_av1_and_still_capped() -> None:
    formats = [
        AUDIO_M4A,
        _fmt("247", "vp09.00.31.08", 720, ext="webm"),
        _fmt("399", "av01.0.08M.08", 1080),
        _fmt("271", "vp09.00.50.08", 1440, ext="webm"),
    ]
    assert _pick(formats) == "247+140"


def test_selector_refuses_a_source_that_only_exists_as_av1_or_above_the_cap() -> None:
    assert _pick([AUDIO_M4A, _fmt("399", "av01.0.08M.08", 1080), _fmt("400", "av01.0.12M.08", 1440)]) is None
    assert _pick([AUDIO_M4A, _fmt("271", "vp09.00.50.08", 1440, ext="webm")]) is None


def test_selector_accepts_twitch_clips_that_report_no_codec() -> None:
    # Twitch clip formats carry a height but no vcodec/acodec at all.
    formats = [_fmt("480", None, 480, None), _fmt("720", None, 720, None), _fmt("1080", None, 1080, None)]
    assert _pick(formats) == "1080"
    formats.append(_fmt("1440", None, 1440, None))
    assert _pick(formats) == "1080"


def test_selector_is_configurable_and_is_what_the_downloader_passes_to_ytdlp(
    synthetic_video: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "height<=720" in download.format_selector({"download": {"max_height": 720}})
    assert download.format_selector({"download": {"format": "worst"}}) == "worst"
    assert "av01" in download.format_selector({}) and "bestvideo[ext=mp4]" not in download.format_selector({})

    seen: dict = {}
    db.insert_discovered_item({"id": "fmt_item", "source": "youtube", "source_id": "fmt",
                               "source_url": "https://youtube.com/watch?v=fmt"}, db_path=tmp_db)

    def fake_ydl(opts):
        seen.update(opts)
        ydl = MagicMock()
        ydl.download.side_effect = lambda urls: shutil.copyfile(
            synthetic_video, config.media_dir("downloads") / "tmp_fmt_item.mp4")
        return MagicMock(__enter__=lambda s: ydl, __exit__=lambda *a: None)

    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", fake_ydl)
    ok, _ = download.download_single_item(db.get_item("fmt_item", db_path=tmp_db), cfg={}, db_path=tmp_db)
    assert ok is True
    assert seen["format"] == download.format_selector({})


def test_a_download_that_still_came_out_as_av1_is_failed_not_processed(
    synthetic_video: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.insert_discovered_item({"id": "av1_item", "source": "youtube", "source_id": "av1",
                               "source_url": "https://youtube.com/watch?v=av1"}, db_path=tmp_db)
    ydl = MagicMock()
    ydl.download.side_effect = lambda urls: shutil.copyfile(
        synthetic_video, config.media_dir("downloads") / "tmp_av1_item.mp4")
    monkeypatch.setattr(download.yt_dlp, "YoutubeDL",
                        lambda *a, **k: MagicMock(__enter__=lambda s: ydl, __exit__=lambda *a: None))
    monkeypatch.setattr(download, "probe_video_stream", lambda p: ("av1", 1440))

    ok, msg = download.download_single_item(db.get_item("av1_item", db_path=tmp_db), cfg={}, db_path=tmp_db)

    assert ok is False and "av1" in msg
    item = db.get_item("av1_item", db_path=tmp_db)
    assert item["stage"] == "failed" and item["error_stage"] == "download"
    assert list(config.media_dir("downloads").glob("*av1_item*")) == []     # nothing left on disk


def test_real_h264_file_passes_the_source_check(synthetic_video: Path) -> None:
    assert download.probe_video_stream(synthetic_video) == ("h264", 240)
    assert download.source_is_acceptable(synthetic_video, {})[0] is True
