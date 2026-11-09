"""Unit tests for Stage 6: Kokoro TTS voiceover synthesis."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np

import pytest

from pipeline import config, db, voiceover


def test_synthesize_voiceover_mocked(tmp_path: Path):
    mock_kokoro = MagicMock()
    # 24,000 samples at 24kHz = 1.0 second duration
    mock_samples = np.zeros(24000, dtype=np.float32)
    mock_kokoro.create.return_value = (mock_samples, 24000)

    out_file = tmp_path / "mock_vo.wav"
    with patch("pipeline.voiceover.get_kokoro", return_value=mock_kokoro):
        dur = voiceover.synthesize_voiceover("Check out this crazy GTA 6 moment!", out_file)

    assert dur == 1.0
    assert out_file.exists()


def test_run_voiceover_for_item(tmp_db: Path, tmp_path: Path):
    db.init_db(tmp_db)
    item_id = "test_item_vo"
    db.insert_discovered_item(
        {
            "id": item_id,
            "source": "youtube",
            "source_id": "vid_vo",
            "source_url": "https://youtube.com/watch?v=vid_vo",
            "author_name": "Gamer123",
            "original_title": "GTA 6 Leak",
        },
        db_path=tmp_db,
    )

    mock_kokoro = MagicMock()
    mock_samples = np.zeros(48000, dtype=np.float32)  # 2.0s
    mock_kokoro.create.return_value = (mock_samples, 24000)

    with patch("pipeline.voiceover.get_kokoro", return_value=mock_kokoro):
        ok, msg = voiceover.run_voiceover_for_item(item_id, db_path=tmp_db)

    assert ok is True
    assert "synthesized" in msg.lower()
    item = db.get_item(item_id, db_path=tmp_db)
    assert item["voiceover_audio_path"] is not None
    assert Path(item["voiceover_audio_path"]).exists()
    Path(item["voiceover_audio_path"]).unlink(missing_ok=True)

