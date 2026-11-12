"""Fix 2 (2026-11-12): Gemini 503 / 5xx / network errors are outages, not failed hook attempts.

The 11 Nov office preview run (preview.db stage_log row 18, item_a35aead652d3): a grounding check's
HTTP 503 was counted as "attempt 1" of 3 and never retried. Now: backoff retries inside the call,
then the item waits in its stage for the next cycle. Fake transport (requests.post), fake clock."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from pipeline import config, db, hooks, render, telegram_bot, transcribe

CFG = config.get_config()
BACKOFF = [float(s) for s in hooks.gemini_settings(CFG)["transient_backoff_s"]]

WORDS = [{"word": w, "start": 0.4 * i, "end": 0.4 * i + 0.3, "prob": 0.9}
         for i, w in enumerate("Lucia drives through Vice City at night.".split())]
GOOD = {"authenticity": "unverified", "hook_style": "question", "hook_text": "IS LUCIA BACK IN VICE CITY?",
        "voiceover_script": "Lucia might be back in Vice City, look at this!", "keywords": ["LUCIA", "VICE", "CITY"]}


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _ok(text: str) -> _Resp:
    return _Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]})


def _503() -> _Resp:
    return _Resp(503, {"error": {"code": 503, "message": "The model is overloaded. Please try again later.",
                                 "status": "UNAVAILABLE"}})


@pytest.fixture
def fake_clock(monkeypatch):
    state = {"t": 1000.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        state["t"] += s

    monkeypatch.setattr(hooks, "_sleep", sleep)
    monkeypatch.setattr(hooks, "_now", lambda: state["t"])
    monkeypatch.setattr(hooks, "_LAST_CALL_T", None)
    monkeypatch.setattr(hooks, "_QUOTA_BLOCKED_UNTIL", None)
    return state


@pytest.fixture
def alerts(monkeypatch):
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(telegram_bot, "send_alert", lambda text, key=None, **kw: sent.append((text, key)) or True)
    return sent


def _cut_item(tmp_db: Path, tmp_path: Path) -> str:
    cut_p = tmp_path / "item_o_cut.mp4"
    cut_p.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_o", "source": "youtube", "source_id": "vid_o",
                               "source_url": "https://example.invalid/o", "author_name": "GamingBolt",
                               "original_title": "GTA 6 Lucia", "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage("item_o", "cut", extra_fields={"cut_path": str(cut_p), "cut_start": 10.0,
                                                       "cut_end": 30.0, "cut_duration": 20.0}, db_path=tmp_db)
    render.store_cut_words("item_o", db.get_item("item_o", db_path=tmp_db), WORDS,
                           transcribe.cache_key(CFG), db_path=tmp_db)
    return "item_o"


def _run(iid, tmp_db, responses):
    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks.config.load_env", return_value={"GEMINI_API_KEY": "k"}), \
         patch("pipeline.hooks.requests.post", side_effect=responses) as post:
        ok, msg = hooks.run_hooks_for_item(iid, cfg=CFG, db_path=tmp_db)
    return ok, msg, post


# ----------------------------------------------------------------------------- the transport

def test_503_then_success_is_retried_after_the_first_backoff(fake_clock):
    with patch("pipeline.hooks.requests.post", side_effect=[_503(), _ok("PASS")]) as post:
        assert hooks._call_gemini("p", api_key="k", cfg=CFG) == "PASS"
    assert post.call_count == 2
    assert BACKOFF[0] in fake_clock["sleeps"]


def test_timeouts_and_connection_errors_are_outages_too(fake_clock):
    side = [requests.ConnectTimeout("t"), requests.ConnectionError("reset"), _ok("PASS")]
    with patch("pipeline.hooks.requests.post", side_effect=side):
        assert hooks._call_gemini("p", api_key="k", cfg=CFG) == "PASS"
    assert BACKOFF[0] in fake_clock["sleeps"] and BACKOFF[1] in fake_clock["sleeps"]


def test_a_persistent_503_raises_a_transient_error_after_every_backoff(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_503()) as post:
        with pytest.raises(hooks.GeminiTransientError, match="503"):
            hooks._call_gemini("p", api_key="k", cfg=CFG)
    assert post.call_count == 1 + len(BACKOFF)                       # the first try + 3 retries
    for wait in BACKOFF:                                              # 10 s, 30 s, 90 s
        assert wait in fake_clock["sleeps"]


def test_the_12_second_pacing_still_applies_between_retries(fake_clock):
    with patch("pipeline.hooks.requests.post", side_effect=[_503(), _ok("PASS")]):
        hooks._call_gemini("p", api_key="k", cfg=CFG)
    rpm = hooks.gemini_settings(CFG)["requests_per_minute"]
    assert sum(fake_clock["sleeps"]) >= 60.0 / rpm                   # 10 s backoff + 2 s pacing top-up


def test_grounding_check_outage_is_not_a_verdict(fake_clock):
    with patch("pipeline.hooks.requests.post", return_value=_503()):
        with pytest.raises(hooks.GeminiTransientError):
            hooks.verify_hook_grounding("IS THIS REAL?", "unverified", "T", api_key="k", cfg=CFG)


# ----------------------------------------------------------------------------- the hooks stage

def test_503_then_success_through_the_stage_uses_one_attempt(tmp_db, tmp_path, fake_clock):
    iid = _cut_item(tmp_db, tmp_path)
    ok, msg, post = _run(iid, tmp_db, [_503(), _ok(json.dumps(GOOD)), _ok("PASS")])
    assert ok, msg
    row = db.get_item(iid, db_path=tmp_db)
    assert row["stage"] == "cut" and row["hook_text"] == GOOD["hook_text"] and row["transient_errors"] == 0


def test_503_on_every_retry_leaves_the_item_waiting_without_using_an_attempt(tmp_db, tmp_path, fake_clock, alerts):
    """The 11 Nov case: the GROUNDING CHECK hit the 503 after a good generation."""
    iid = _cut_item(tmp_db, tmp_path)
    responses = [_ok(json.dumps(GOOD))] + [_503()] * (1 + len(BACKOFF))
    ok, msg, post = _run(iid, tmp_db, responses)
    assert not ok and hooks.is_transient_result(msg) and "attempt" not in msg
    assert post.call_count == 1 + 1 + len(BACKOFF)                   # one generation, the check + its retries
    row = db.get_item(iid, db_path=tmp_db)
    assert row["stage"] == "cut" and row["error_stage"] is None      # not failed, not rejected
    assert row["transient_errors"] == 1
    assert alerts == []


def test_a_parse_failure_still_counts_as_an_attempt(tmp_db, tmp_path, fake_clock):
    iid = _cut_item(tmp_db, tmp_path)
    attempts = hooks.gemini_settings(CFG)["max_attempts"]
    ok, msg, post = _run(iid, tmp_db, [_ok("Sure! Here is a hook: IS THIS GTA 6?")] * attempts)
    assert not ok and "not valid JSON" in msg and f"attempt {attempts}" in msg
    assert post.call_count == attempts
    row = db.get_item(iid, db_path=tmp_db)
    assert row["stage"] == "failed" and row["error_stage"] == "hooks"


def test_six_cycles_of_outage_send_exactly_one_alert_and_the_item_keeps_waiting(tmp_db, tmp_path, fake_clock, alerts):
    iid = _cut_item(tmp_db, tmp_path)
    limit = int(hooks.gemini_settings(CFG)["transient_alert_after_cycles"])
    for cycle in range(1, limit + 3):
        ok, msg, _ = _run(iid, tmp_db, [_503()] * (1 + len(BACKOFF)))
        assert not ok and hooks.is_transient_result(msg)
        assert db.get_item(iid, db_path=tmp_db)["transient_errors"] == cycle
        assert len(alerts) == (1 if cycle >= limit else 0)
    assert alerts[0][1] == f"gemini_unavailable:{iid}" and f"{limit} pipeline cycles" in alerts[0][0]
    assert db.get_item(iid, db_path=tmp_db)["stage"] == "cut"
    # Gemini is back: the hook is made and the outage count starts over.
    ok, msg, _ = _run(iid, tmp_db, [_ok(json.dumps(GOOD)), _ok("PASS")])
    assert ok, msg
    assert db.get_item(iid, db_path=tmp_db)["transient_errors"] == 0


def test_render_does_not_fail_an_item_whose_hook_hit_an_outage(tmp_db, tmp_path, fake_clock, alerts):
    iid = _cut_item(tmp_db, tmp_path)
    with patch.object(render, "verify_video_file", return_value=(True, 20.0)), \
         patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks.config.load_env", return_value={"GEMINI_API_KEY": "k"}), \
         patch("pipeline.hooks.requests.post", return_value=_503()), \
         patch.object(render, "render_item_variants") as riv:
        ok, msg, _ = render.run_render_item(iid, cfg=CFG, db_path=tmp_db)
    assert not ok and hooks.is_transient_result(msg) and riv.call_count == 0
    row = db.get_item(iid, db_path=tmp_db)
    assert row["stage"] == "cut" and row["error_stage"] is None


def test_the_daily_quota_429_is_unchanged(tmp_db, tmp_path, fake_clock):
    iid = _cut_item(tmp_db, tmp_path)
    quota = _Resp(429, {"error": {"code": 429, "details": [{"retryDelay": "45243s"}]}})
    ok, msg, post = _run(iid, tmp_db, [quota])
    assert not ok and "daily quota exhausted" in msg and post.call_count == 1
    assert db.get_item(iid, db_path=tmp_db)["stage"] == "failed"
