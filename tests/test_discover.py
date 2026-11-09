"""Unit tests for YouTube discovery, recency decay, and official channel down-weighting."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline import config, db, discover, rate_limit


def test_discover_youtube_mocked(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_flat_entries = {
        "entries": [
            {"id": "yt_creator_1", "title": "GTA 6 Gameplay"},
            {"id": "yt_official_1", "title": "GTA 6 Trailer"},
        ]
    }

    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = fake_flat_entries

    # Mock metadata calls
    def mock_meta_extract(url: str, download: bool = False) -> dict[str, Any]:
        if "yt_creator_1" in url:
            return {
                "id": "yt_creator_1",
                "title": "GTA 6 Secret Gameplay Breakdown",
                "duration": 45.0,
                "view_count": 50000,
                "timestamp": 1794100000,
                "channel_id": "UC_creator_normal",
                "uploader": "GTA Creator",
            }
        else:
            return {
                "id": "yt_official_1",
                "title": "GTA 6 Official Trailer",
                "duration": 90.0,
                "view_count": 100000000,
                "timestamp": 1701731230,  # Dec 2023
                "channel_id": "UC6VcWc1rAoWdBCM0JxrRQ3A",  # Rockstar Games official
                "uploader": "Rockstar Games",
            }

    mock_meta_ydl = MagicMock()
    mock_meta_ydl.extract_info.side_effect = mock_meta_extract

    def ydl_factory(opts: dict[str, Any]) -> Any:
        if opts.get("extract_flat"):
            return MagicMock(__enter__=lambda s: mock_ydl, __exit__=lambda *a: None)
        return MagicMock(__enter__=lambda s: mock_meta_ydl, __exit__=lambda *a: None)

    monkeypatch.setattr(discover.yt_dlp, "YoutubeDL", ydl_factory)

    items = discover.discover_youtube(db_path=tmp_db)
    assert len(items) == 2

    creator_item = next(i for i in items if i["source_id"] == "yt_creator_1")
    official_item = next(i for i in items if i["source_id"] == "yt_official_1")

    assert creator_item["is_official"] is False
    assert creator_item["effective_weight"] == 1.0

    assert official_item["is_official"] is True
    assert official_item["effective_weight"] == 0.20


def test_decay_ordering_with_known_ages(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Item A: Fresh creator upload (24h old, 50,000 views)
    # Item B: Old viral trailer (25,000h old, 200,000,000 views)
    mock_candidates = [
        {
            "id": "item_fresh_creator",
            "source": "youtube",
            "source_id": "fresh_1",
            "channel_id": "UC_creator",
            "source_url": "https://yt.com/fresh_1",
            "author_name": "Fresh Creator",
            "original_title": "GTA 6 Fresh Breakdown",
            "original_duration": 30.0,
            "view_count": 50000,
            "upload_time": "2026-11-08T00:00:00Z",
            "raw_velocity": 50000 / 24.0,  # ~2083 vph
            "age_hours": 24.0,
            "is_official": False,
            "effective_weight": 1.0,
        },
        {
            "id": "item_old_trailer",
            "source": "youtube",
            "source_id": "old_trailer",
            "channel_id": "UC6VcWc1rAoWdBCM0JxrRQ3A",
            "source_url": "https://yt.com/old_trailer",
            "author_name": "Rockstar Games",
            "original_title": "Grand Theft Auto VI Trailer 1",
            "original_duration": 91.0,
            "view_count": 200000000,
            "upload_time": "2023-12-04T00:00:00Z",
            "raw_velocity": 200000000 / 25000.0,  # ~8000 vph
            "age_hours": 25000.0,
            "is_official": True,
            "effective_weight": 0.20,
        },
    ]

    monkeypatch.setattr(discover, "discover_youtube", lambda cfg, db_path: mock_candidates)

    scored = discover.run_discovery(db_path=tmp_db)
    assert len(scored) == 2

    fresh_res = next(s for s in scored if s["id"] == "item_fresh_creator")
    old_res = next(s for s in scored if s["id"] == "item_old_trailer")

    # Due to exponential decay (0.5 ** (25000 / 36)), old trailer decay factor is ~0.0
    assert old_res["decay_factor"] < 1e-10
    assert old_res["discovery_score"] < 1e-10

    # Fresh creator has substantial decay factor and scores much higher
    assert fresh_res["decay_factor"] > 0.5
    assert fresh_res["discovery_score"] > 0.1
    assert fresh_res["discovery_score"] > old_res["discovery_score"]


def test_paused_source_skipped(tmp_db: Path) -> None:
    rate_limit.record_error("youtube", "Testing cooldown pause", pause_hours=1.0, db_path=tmp_db)
    items = discover.discover_youtube(db_path=tmp_db)
    assert items == []
