"""Unit tests for Stage 5: Gemini hook generation, authenticity classification, and grounding checks."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import config, db, hooks, render


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


# ============================================================ transport: pacing, 429, truncation

class _Resp:
    def __init__(self, status: int, body: dict, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _ok(text: str, finish: str = "STOP") -> _Resp:
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
                       "usageMetadata": {"thoughtsTokenCount": 0}})


def _429(delay: str = "54s") -> _Resp:
    return _Resp(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


@pytest.fixture
def fake_clock(monkeypatch):
    """No real sleeping: a fake monotonic clock that advances only when _sleep is called."""
    state = {"t": 1000.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        state["t"] += s

    monkeypatch.setattr(hooks, "_sleep", sleep)
    monkeypatch.setattr(hooks, "_now", lambda: state["t"])
    monkeypatch.setattr(hooks, "_LAST_CALL_T", None)
    return state


def test_call_gemini_turns_thinking_off_asks_for_json_and_raises_the_cap(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_ok('{"a": 1}')) as post:
        hooks._call_gemini("p", api_key="k", json_mode=True)
    gen = post.call_args.kwargs["json"]["generationConfig"]
    assert gen["thinkingConfig"] == {"thinkingBudget": 0}
    assert gen["responseMimeType"] == "application/json"
    assert gen["maxOutputTokens"] >= 2048


def test_max_tokens_is_a_failed_attempt_not_a_partial_answer(fake_clock):
    truncated = '```json\n{\n  "authenticity": "unverified",\n  "hook_te'
    with patch("pipeline.hooks.requests.post", return_value=_ok(truncated, finish="MAX_TOKENS")):
        with pytest.raises(hooks.GeminiTruncatedError, match="MAX_TOKENS"):
            hooks._call_gemini("p", api_key="k")
        with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]):
            with pytest.raises(hooks.GeminiTruncatedError):
                hooks.generate_hook_and_voiceover("dummy.mp4", "T", api_key="k")


@pytest.mark.parametrize("answer", [
    '{"authenticity": "unverified", "hook_style": "question", "hook_te',   # cut-off JSON
    "Sure! Here is your hook: IS THIS GTA 6?",                              # prose
    '{"authenticity": "unverified", "voiceover_script": "Line."}',          # no hook_text
    '{"hook_text": "IS THIS REAL?", "voiceover_script": ""}',               # empty VO
    '["IS THIS REAL?"]',                                                    # not an object
])
def test_bad_json_is_a_failed_attempt_never_defaults(answer):
    with patch("pipeline.hooks._call_gemini", return_value=answer), \
         patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]):
        with pytest.raises(hooks.GeminiBadResponseError):
            hooks.generate_hook_and_voiceover("dummy.mp4", "T")


def test_429_waits_the_servers_retry_delay_then_retries(fake_clock):
    with patch("pipeline.hooks.requests.post", side_effect=[_429("54s"), _ok("PASS")]) as post:
        assert hooks._call_gemini("p", api_key="k") == "PASS"
    assert post.call_count == 2
    assert any(s >= 54 for s in fake_clock["sleeps"])


def test_429_that_persists_raises_a_rate_limit_error(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_429("3s")):
        with pytest.raises(hooks.GeminiRateLimitError):
            hooks._call_gemini("p", api_key="k")


def test_calls_are_spaced_to_the_free_tier_limit(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_ok("PASS")):
        hooks._call_gemini("p", api_key="k")
        hooks._call_gemini("p", api_key="k")
    rpm = hooks.gemini_settings()["requests_per_minute"]
    assert fake_clock["sleeps"] and fake_clock["sleeps"][-1] == pytest.approx(60.0 / rpm)


def test_errors_never_leak_the_api_key(fake_clock):
    import requests as rq
    secret = "AIza" + "S" * 31
    boom = rq.ConnectionError(f"Max retries exceeded with url: /v1beta/models/x:generateContent?key={secret}")
    with patch("pipeline.hooks.requests.post", side_effect=boom):
        with pytest.raises(hooks.GeminiError) as ei:
            hooks._call_gemini("p", api_key=secret)
    assert secret not in str(ei.value) and "REDACTED" in str(ei.value)


# ============================================================ grounding errors = NOT grounded

def test_grounding_check_429_is_not_grounded():
    with patch("pipeline.hooks._call_gemini", side_effect=hooks.GeminiRateLimitError("HTTP 429")):
        passed, msg = hooks.verify_hook_grounding("IS THIS REAL?", "unverified", "GTA 6 Leak")
    assert passed is False and msg.startswith("ERROR")


def test_grounding_check_429_through_the_transport_is_not_grounded(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_429("1s")):
        passed, msg = hooks.verify_hook_grounding("IS THIS REAL?", "unverified", "T", api_key="k")
    assert passed is False and "429" in msg


def test_grounding_unexpected_reply_is_not_grounded():
    with patch("pipeline.hooks._call_gemini", return_value="YES"):
        passed, msg = hooks.verify_hook_grounding("IS THIS REAL?", "unverified", "T")
    assert passed is False and msg.startswith("ERROR")


# ============================================================ the hooks stage

GOOD = {"authenticity": "unverified", "hook_style": "question", "hook_text": "IS LUCIA BACK IN VICE CITY?",
        "voiceover_script": "Lucia might be back in Vice City, look at this!", "keywords": ["LUCIA", "VICE", "CITY"],
        "keywords_source": "gemini"}
WORDS = [{"word": "Lucia", "start": 0.4, "end": 0.8, "prob": 0.9},
         {"word": "drives", "start": 0.8, "end": 1.1, "prob": 0.9},
         {"word": "through", "start": 1.1, "end": 1.3, "prob": 0.9},
         {"word": "Vice", "start": 1.3, "end": 1.6, "prob": 0.9},
         {"word": "City.", "start": 1.6, "end": 2.0, "prob": 0.9}]


def _cut_item(tmp_db: Path, tmp_path: Path, **extra) -> str:
    cut = tmp_path / "item_h_cut.mp4"
    cut.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_h", "source": "youtube", "source_id": "vid_h",
                               "source_url": "https://example.invalid/v", "author_name": "Creator",
                               "original_title": "GTA 6 Lucia Leak", "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage("item_h", "cut", extra_fields={"cut_path": str(cut), "cut_start": 10.0, "cut_end": 30.0,
                                                       "cut_duration": 20.0, **extra}, db_path=tmp_db)
    return "item_h"


def test_hooks_stage_fails_the_item_when_no_valid_gemini_hook(tmp_db, tmp_path):
    # A leftover placeholder hook from an older run must not survive the failure.
    iid = _cut_item(tmp_db, tmp_path, hook_text="IS THIS REALLY GTA 6?", voiceover_script="Wait until you see this!",
                    keywords='["GTA", "6"]')
    with patch.object(render, "transcribe_words", return_value=WORDS), \
         patch("pipeline.hooks.generate_hook_and_voiceover",
               side_effect=hooks.GeminiTruncatedError("Gemini answer truncated (finishReason MAX_TOKENS)")) as gen, \
         patch("pipeline.hooks.verify_hook_grounding") as ver:
        ok, msg = hooks.run_hooks_for_item(iid, cfg=config.get_config(), db_path=tmp_db)
    assert ok is False and "MAX_TOKENS" in msg
    assert gen.call_count == hooks.gemini_settings()["max_attempts"]
    ver.assert_not_called()
    item = db.get_item(iid, db_path=tmp_db)
    assert item["stage"] == "failed" and item["error_stage"] == "hooks"
    assert "MAX_TOKENS" in item["error_message"]
    assert item["hook_text"] is None and item["voiceover_script"] is None and item["keywords"] is None


def test_hooks_stage_grounding_errors_never_pass_and_never_fall_back(tmp_db, tmp_path):
    iid = _cut_item(tmp_db, tmp_path)
    with patch.object(render, "transcribe_words", return_value=WORDS), \
         patch("pipeline.hooks.generate_hook_and_voiceover", return_value=dict(GOOD)), \
         patch("pipeline.hooks.verify_hook_grounding", return_value=(False, "ERROR: grounding check failed: HTTP 429")):
        ok, msg = hooks.run_hooks_for_item(iid, cfg=config.get_config(), db_path=tmp_db)
    item = db.get_item(iid, db_path=tmp_db)
    assert ok is False and item["stage"] == "failed" and "429" in item["error_message"]
    assert item["hook_text"] is None


def test_hooks_stage_two_grounding_fails_use_the_fixed_question_hook(tmp_db, tmp_path):
    iid = _cut_item(tmp_db, tmp_path)
    with patch.object(render, "transcribe_words", return_value=WORDS), \
         patch("pipeline.hooks.generate_hook_and_voiceover", return_value=dict(GOOD)) as gen, \
         patch("pipeline.hooks.verify_hook_grounding", return_value=(False, "FAIL: Lucia is not in the clip")):
        ok, msg = hooks.run_hooks_for_item(iid, cfg=config.get_config(), db_path=tmp_db)
    item = db.get_item(iid, db_path=tmp_db)
    assert ok is True and gen.call_count == 2
    assert item["stage"] == "cut"
    assert item["hook_text"] == "IS THIS REALLY GTA 6?" and item["hook_grounded"] == 0
    assert item["authenticity"] == "unverified"


def test_hooks_stage_passes_the_cut_transcript_into_the_prompt_and_caches_it(tmp_db, tmp_path):
    iid = _cut_item(tmp_db, tmp_path)
    prompts: list[str] = []

    def fake_gemini(prompt, images=None, **kw):
        prompts.append(prompt)
        if "Return ONLY a valid JSON object" in prompt:
            assert images == [b"frame"] * 4 and kw.get("json_mode") is True
            return json.dumps({k: v for k, v in GOOD.items() if k != "keywords_source"})
        return "PASS"

    with patch.object(render, "transcribe_words", return_value=WORDS) as tw, \
         patch("pipeline.hooks.extract_sample_frames", return_value=[b"frame"] * 4), \
         patch("pipeline.hooks._call_gemini", side_effect=fake_gemini):
        ok, msg = hooks.run_hooks_for_item(iid, cfg=config.get_config(), db_path=tmp_db)

    assert ok is True and "source=gemini" in msg
    tw.assert_called_once()
    gen_prompt, check_prompt = prompts
    assert 'AUDIO TRANSCRIPT: "Lucia drives through Vice City."' in gen_prompt
    assert 'SOURCE TITLE: "GTA 6 Lucia Leak"' in gen_prompt
    assert "Lucia drives through Vice City." in check_prompt
    item = db.get_item(iid, db_path=tmp_db)
    assert item["hook_text"] == GOOD["hook_text"] and item["hook_grounded"] == 1
    assert json.loads(item["keywords"]) == ["LUCIA", "VICE", "CITY"]
    cached = json.loads(item["transcript_words"])
    assert cached["words"] == WORDS and (cached["cut_start"], cached["cut_end"]) == (10.0, 30.0)
    assert render.cached_cut_words(item, render.render_settings()["whisper_model"]) == WORDS


def test_hooks_stage_reuses_its_own_cached_transcript(tmp_db, tmp_path):
    iid = _cut_item(tmp_db, tmp_path)
    item = db.get_item(iid, db_path=tmp_db)
    render.store_cut_words(iid, item, WORDS, render.render_settings()["whisper_model"], db_path=tmp_db)
    with patch.object(render, "transcribe_words") as tw, \
         patch("pipeline.hooks.generate_hook_and_voiceover", return_value=dict(GOOD)), \
         patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "ok")):
        ok, _ = hooks.run_hooks_for_item(iid, cfg=config.get_config(), db_path=tmp_db)
    assert ok is True
    tw.assert_not_called()

