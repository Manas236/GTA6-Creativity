"""Unit tests for Stage 7: Subtitle generation and vertical 9:16 rendering."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import render
from pipeline.download import verify_video_file


def test_format_ass_timestamp():
    assert render.format_ass_timestamp(0.0) == "0:00:00.00"
    assert render.format_ass_timestamp(12.34) == "0:00:12.34"
    assert render.format_ass_timestamp(65.50) == "0:01:05.50"
    assert render.format_ass_timestamp(3661.05) == "1:01:01.05"


def test_build_ass_subtitles_no_speech():
    """When speech words are empty, ASS file must contain Hook line and NO caption lines."""
    ass_text = render.build_ass_subtitles("IS THIS REALLY GTA 6?", video_duration=20.0, words=[])
    assert "[Script Info]" in ass_text
    assert "PlayResX: 1080" in ass_text
    assert "PlayResY: 1920" in ass_text
    assert "Style: Hook" in ass_text
    assert "IS THIS REALLY GTA 6?" in ass_text
    # No Caption dialogue events
    assert "Caption" not in [line.split(",")[3] for line in ass_text.splitlines() if line.startswith("Dialogue:")]


def test_build_ass_subtitles_with_speech():
    """When speech words exist, ASS file must contain Hook and highlighted Caption lines."""
    sample_words = [
        {"word": "LOOK", "start": 1.0, "end": 1.5, "prob": 0.99},
        {"word": "AT", "start": 1.5, "end": 1.8, "prob": 0.99},
        {"word": "THIS", "start": 1.8, "end": 2.2, "prob": 0.99},
    ]
    ass_text = render.build_ass_subtitles("OFFICIAL GTA 6 TRAILER", video_duration=20.0, words=sample_words)
    assert "OFFICIAL GTA 6 TRAILER" in ass_text
    # Check that word highlighting tag is present
    assert r"{\c&H0000FFFF&}" in ass_text
    assert "LOOK" in ass_text
    assert "THIS" in ass_text


def test_render_variant_a_synthetic(tmp_path: Path):
    """Test fast 1-second synthetic render of Variant A to verify filter graph and 1080x1920 canvas."""
    cut_path = tmp_path / "test_cut.mp4"
    ass_path = tmp_path / "test.ass"
    out_path = tmp_path / "render_A.mp4"

    # 1s synthetic cut video with audio
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac",
            str(cut_path),
        ],
        capture_output=True,
        check=True,
    )

    ass_text = render.build_ass_subtitles("TEST HOOK", video_duration=1.0, words=[])
    ass_path.write_text(ass_text, encoding="utf-8")

    ok, elapsed = render.render_variant_a(cut_path, ass_path, out_path)
    assert ok is True
    assert elapsed > 0.0
    assert out_path.exists()

    is_valid, dur = verify_video_file(out_path)
    assert is_valid is True
    assert 0.8 <= dur <= 1.5


def test_render_variant_b_synthetic(tmp_path: Path):
    """Test fast 1-second synthetic render of Variant B with audio ducking graph."""
    cut_path = tmp_path / "test_cut.mp4"
    vo_path = tmp_path / "test_vo.wav"
    ass_path = tmp_path / "test_b.ass"
    out_path = tmp_path / "render_B.mp4"

    # 1s synthetic cut video with audio
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "color=c=red:s=320x180:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-c:a", "aac",
            str(cut_path),
        ],
        capture_output=True,
        check=True,
    )

    # 0.5s VO WAV
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=0.5",
            "-c:a", "pcm_s16le",
            str(vo_path),
        ],
        capture_output=True,
        check=True,
    )

    ass_text = render.build_ass_subtitles("TEST HOOK B", video_duration=1.0, words=[])
    ass_path.write_text(ass_text, encoding="utf-8")

    ok, elapsed = render.render_variant_b(cut_path, vo_path, ass_path, out_path)
    assert ok is True
    assert elapsed > 0.0
    assert out_path.exists()

    is_valid, dur = verify_video_file(out_path)
    assert is_valid is True
    assert 0.8 <= dur <= 1.5
