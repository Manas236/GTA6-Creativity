"""Unit tests for discovery stage with zero network requests."""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from pipeline import config, db, discover, rate_limit


def test_discover_youtube_mocked(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_yt_response = {
        "entries": [
            {
                "id": "yt_mock_1",
                "title": "GTA 6 Leaked Gameplay Vice City",
                "duration": 40.0,
                "view_count": 250000,
                "like_count": 12000,
                "channel": "GTA Leaks Official",
                "upload_date": "20261108",
                "url": "https://www.youtube.com/watch?v=yt_mock_1",
            },
            {
                "id": "yt_mock_2_toolong",
                "title": "GTA 6 2-Hour Livestream Breakdown",
                "duration": 7200.0,  # Exceeds max 600s
                "view_count": 10000,
                "channel": "Streamer",
                "url": "https://www.youtube.com/watch?v=yt_mock_2_toolong",
            },
        ]
    }

    mock_ydl = MagicMock()
    mock_ydl.extract_info.return_value = fake_yt_response
    monkeypatch.setattr(discover.yt_dlp, "YoutubeDL", lambda *args, **kwargs: MagicMock(__enter__=lambda s: mock_ydl, __exit__=lambda *a: None))

    items = discover.discover_youtube(db_path=tmp_db)
    assert len(items) == 1
    assert items[0]["source_id"] == "yt_mock_1"
    assert items[0]["view_count"] == 250000
    assert items[0]["raw_velocity"] > 0


def test_discover_reddit_mocked(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_reddit_payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "red_mock_1",
                        "title": "Found this secret building in GTA 6 trailer!",
                        "author": "LuciaFan",
                        "is_video": True,
                        "media": {"reddit_video": {"duration": 15}},
                        "score": 450,
                        "num_comments": 80,
                        "created_utc": 1794100000.0,
                        "permalink": "/r/GTA6/comments/red_mock_1",
                        "over_18": False,
                    }
                },
                {
                    "data": {
                        "id": "red_mock_text",
                        "title": "Discussion: when will trailer 2 drop?",
                        "author": "DiscussionPoster",
                        "is_video": False,
                        "media": None,
                        "over_18": False,
                    }
                },
            ]
        }
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_reddit_payload
    monkeypatch.setattr(discover.requests, "get", lambda *args, **kwargs: mock_resp)

    items = discover.discover_reddit(db_path=tmp_db)
    # The duplicate red_mock_1 across r/GTA6 and r/GTA is properly deduped by ID
    assert len(items) == 1
    assert items[0]["source"] == "reddit"
    assert items[0]["source_id"] == "red_mock_1"
    assert items[0]["upvote_count"] == 450
    assert items[0]["comment_count"] == 80


def test_run_discovery_end_to_end_mocked(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_yt = [
        {
            "id": "item_yt",
            "source": "youtube",
            "source_id": "yt1",
            "source_url": "https://yt.com/1",
            "author_name": "YT Creator",
            "original_title": "Video 1",
            "original_duration": 30.0,
            "view_count": 100000,
            "upvote_count": 5000,
            "comment_count": 200,
            "upload_time": "2026-11-09T00:00:00Z",
            "raw_velocity": 10000.0,
            "age_hours": 10.0,
        }
    ]
    mock_red = [
        {
            "id": "item_red",
            "source": "reddit",
            "source_id": "r1",
            "source_url": "https://reddit.com/r1",
            "author_name": "Redditor",
            "original_title": "Reddit Video",
            "original_duration": 20.0,
            "view_count": 600,
            "upvote_count": 500,
            "comment_count": 50,
            "upload_time": "2026-11-09T05:00:00Z",
            "raw_velocity": 120.0,
            "age_hours": 5.0,
        }
    ]

    monkeypatch.setattr(discover, "discover_youtube", lambda cfg, db_path: mock_yt)
    monkeypatch.setattr(discover, "discover_reddit", lambda cfg, db_path: mock_red)

    results = discover.run_discovery(db_path=tmp_db)
    assert len(results) == 2

    # Verify both exist in DB with discovery scores
    items_in_db = db.get_items_by_stage("discovered", db_path=tmp_db)
    assert len(items_in_db) == 2
    for it in items_in_db:
        assert it["discovery_score"] > 0
        assert 0.0 < it["percentile_rank"] <= 1.0


def test_paused_source_skipped(tmp_db: Path) -> None:
    rate_limit.record_error("youtube", "Testing cooldown pause", pause_hours=1.0, db_path=tmp_db)
    items = discover.discover_youtube(db_path=tmp_db)
    assert items == []
