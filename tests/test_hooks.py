"""Unit tests for Stage 5: Gemini hook generation, authenticity classification, and grounding checks."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline import hooks


def test_hook_json_parsing_clean():
    mock_json = """{
        "authenticity": "official",
        "hook_style": "hype_analysis",
        "hook_text": "OFFICIAL GTA 6 TRAILER BREAKDOWN",
        "voiceover_script": "Look closely at this incredible detail from the official trailer!"
    }"""
    with patch("pipeline.hooks._call_gemini", return_value=mock_json):
        with patch("pipeline.hooks.extract_sample_frames", return_value=[b"fake_frame"]):
            res = hooks.generate_hook_and_voiceover("dummy.mp4", "GTA 6 Official Trailer")

    assert res["authenticity"] == "official"
    assert res["hook_style"] == "hype_analysis"
    assert res["hook_text"] == "OFFICIAL GTA 6 TRAILER BREAKDOWN"
    assert "Look closely" in res["voiceover_script"]


def test_hook_json_with_markdown_code_block():
    mock_resp = """```json
    {
        "authenticity": "unverified",
        "hook_style": "question",
        "hook_text": "IS THIS THE NEW GTA 6 MAP",
        "voiceover_script": "Could this leaked map layout actually be the real deal?"
    }
    ```"""
    with patch("pipeline.hooks._call_gemini", return_value=mock_resp):
        with patch("pipeline.hooks.extract_sample_frames", return_value=[b"fake_frame"]):
            res = hooks.generate_hook_and_voiceover("dummy.mp4", "GTA 6 Map Leaked")

    assert res["authenticity"] == "unverified"
    assert res["hook_style"] == "question"
    # Unverified hooks must end in '?'
    assert res["hook_text"].endswith("?")


def test_unverified_authenticity_default_and_question_framing():
    """Unrecognized or missing authenticity must strictly default to 'unverified' and question."""
    mock_resp = """{
        "authenticity": "unknown_rumor_leak",
        "hook_style": "curiosity",
        "hook_text": "LEAKED VICE CITY GAMEPLAY",
        "voiceover_script": "Check out this leaked clip from Vice City!"
    }"""
    with patch("pipeline.hooks._call_gemini", return_value=mock_resp):
        with patch("pipeline.hooks.extract_sample_frames", return_value=[b"fake_frame"]):
            res = hooks.generate_hook_and_voiceover("dummy.mp4", "Leaked clip")

    # Strict fallback rule: missing or unparseable authenticity = unverified
    assert res["authenticity"] == "unverified"
    assert res["hook_text"].endswith("?")


def test_verify_hook_grounding_pass():
    with patch("pipeline.hooks._call_gemini", return_value="PASS"):
        passed, msg = hooks.verify_hook_grounding("IS THIS REAL?", "unverified", "GTA 6 Leak")
        assert passed is True
        assert "Passed" in msg


def test_verify_hook_grounding_fail():
    with patch("pipeline.hooks._call_gemini", return_value="FAIL: Hook claims confirmed gameplay when clip is a GTA 5 mod"):
        passed, msg = hooks.verify_hook_grounding("CONFIRMED GTA 6 GAMEPLAY", "unverified", "GTA 5 Mod")
        assert passed is False
        assert "FAIL:" in msg

