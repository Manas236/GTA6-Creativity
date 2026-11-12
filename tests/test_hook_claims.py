"""Fix 1 (2026-11-12): hook / voiceover claims that the cut segment does not make.

The Tridzo case from the 11 Nov office preview run: an unverified creator clip got a "LEAKED ..." hook
that passed the overlap check on ONE shared word ("VERSION"), and its voiceover said the creator had
"posted this leak" as fact. The real hook text is not in this PC's databases, so the case is rebuilt
synthetically from what was reported: a LEAKED hook whose only word in common with the segment is
VERSION, over a segment about the PC version and a one-megabyte download (the Tridzo window's words).
Zero network: Gemini is faked."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import config, db, hooks, render, transcribe

CFG = config.get_config()

TRIDZO_SEGMENT = ("Now when it comes to the PC version, it works the same way. You just log in, "
                  "no extra download, one megabyte pack.")
TRIDZO_HOOK = "LEAKED GTA 6 ONLINE BETA VERSION?"
TRIDZO_VO = "Tridzo just posted this leak of the online beta, look at this!"
LEAK_SEGMENT = "This was leaked last night: Lucia driving through Vice City at night."


def _data(hook: str, vo: str = "Look at Lucia in Vice City right now!", auth: str = "unverified") -> dict:
    return {"authenticity": auth, "hook_style": "question", "hook_text": hook, "voiceover_script": vo,
            "keywords": ["LUCIA"]}


# ----------------------------------------------------------------------------- the Tridzo case

def test_tridzo_hook_passed_the_old_rule_on_one_generic_word():
    """What went wrong: the old overlap check let a hook through on any one shared word."""
    old_generic = {"A", "LEAKED", "GTA", "6"}
    content = [t.upper() for t in hooks._tokens(TRIDZO_HOOK) if t.upper() not in old_generic]
    spoken = {hooks._stem(t) for t in hooks._tokens(TRIDZO_SEGMENT)}
    assert [t for t in content if hooks._stem(t) in spoken] == ["VERSION"]      # the single shared word


def test_tridzo_hook_fails_the_overlap_now():
    verdict, why = hooks.segment_overlap_check(TRIDZO_HOOK, TRIDZO_SEGMENT, CFG)
    assert verdict == "FAIL" and "VERSION" not in why.split("not said:")[0]     # VERSION is a generic word now
    assert hooks.content_words(TRIDZO_HOOK, CFG) == ["ONLINE", "BETA"]           # LEAKED is judged as a claim


def test_tridzo_hook_and_voiceover_fail_the_claim_rule():
    assert hooks.claim_check(TRIDZO_HOOK, TRIDZO_SEGMENT, "unverified", CFG)[0] == "FAIL"
    verdict, why = hooks.claim_check(TRIDZO_VO, TRIDZO_SEGMENT, "unverified", CFG, label="voiceover")
    assert verdict == "FAIL" and "voiceover" in why and "LEAK" in why


def test_tridzo_case_fails_the_whole_check_before_any_gemini_call():
    with patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "PASS")) as ver:
        ok, reason = hooks.check_hook_against_segment(_data(TRIDZO_HOOK, TRIDZO_VO), TRIDZO_SEGMENT, "T", cfg=CFG)
    assert not ok and reason.startswith("FAIL") and "LEAKED" in reason
    ver.assert_not_called()                                                     # the free check decided


def _cut_item(tmp_db: Path, tmp_path: Path, channel_id: str = "UC9CKhua6_qMznuhElgBDkWQ") -> str:
    cut_p = tmp_path / "item_t_cut.mp4"
    cut_p.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_t", "source": "youtube", "source_id": "vid_t", "channel_id": channel_id,
                               "source_url": "https://example.invalid/t", "author_name": "Tridzo GTA",
                               "original_title": "BREAKING: GTA 6 ONLINE MULTIPLAYER LEAKED..",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage("item_t", "cut", extra_fields={"cut_path": str(cut_p), "cut_start": 113.11,
                                                       "cut_end": 135.01, "cut_duration": 21.9}, db_path=tmp_db)
    words = [{"word": w, "start": 0.3 * i, "end": 0.3 * i + 0.25, "prob": 0.9}
             for i, w in enumerate(TRIDZO_SEGMENT.split())]
    render.store_cut_words("item_t", db.get_item("item_t", db_path=tmp_db), words,
                           transcribe.cache_key(CFG), db_path=tmp_db)
    return "item_t"


def test_tridzo_case_is_regenerated_once_with_the_reason_then_needs_review(tmp_db, tmp_path):
    iid = _cut_item(tmp_db, tmp_path)
    prompts: list[str] = []
    answer = json.dumps(_data(TRIDZO_HOOK, TRIDZO_VO))

    def fake(prompt, images=None, **kw):
        prompts.append(prompt)
        return answer

    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks._call_gemini", side_effect=fake):
        ok, msg = hooks.run_hooks_for_item(iid, cfg=CFG, db_path=tmp_db)
    assert not ok and msg.startswith("needs_review")
    assert len(prompts) == 2                                         # generate, regenerate; no Gemini check
    assert "PREVIOUS ATTEMPT REJECTED" in prompts[1] and "LEAKED" in prompts[1]
    assert "never use any of them" in prompts[0]                     # the rule is in the prompt up front
    row = db.get_item(iid, db_path=tmp_db)
    assert row["stage"] == "needs_review" and json.loads(row["hook_review"])["reason"].startswith("FAIL")


# ----------------------------------------------------------------------------- official vs unverified

@pytest.mark.parametrize("auth,expected", [("official", "PASS"), ("unverified", "FAIL")])
def test_a_segment_saying_this_was_leaked_allows_the_claim_only_for_an_official_item(auth, expected):
    hook = "LUCIA'S LEAKED VICE CITY DRIVE"
    assert hooks.claim_check(hook, LEAK_SEGMENT, auth, CFG)[0] == expected
    with patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "PASS")):
        ok, _ = hooks.check_hook_against_segment(_data(hook, auth=auth), LEAK_SEGMENT, "T", cfg=CFG)
    assert ok is (expected == "PASS")


def test_an_official_claim_the_segment_does_not_make_fails():
    verdict, why = hooks.claim_check("CONFIRMED: LUCIA IN VICE CITY", LEAK_SEGMENT, "official", CFG)
    assert verdict == "FAIL" and "CONFIRMED" in why


@pytest.mark.parametrize("auth,expected", [("official", True), ("unverified", False)])
def test_the_voiceover_is_checked_the_same_way(auth, expected):
    vo = "This footage of Lucia in Vice City was leaked last night!"
    with patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "PASS")):
        ok, reason = hooks.check_hook_against_segment(
            _data("WHAT IS LUCIA DOING IN VICE CITY?", vo=vo, auth=auth), LEAK_SEGMENT, "T", cfg=CFG)
    assert ok is expected
    if not expected:
        assert reason.startswith("FAIL: the voiceover")


def test_a_voiceover_claim_the_segment_does_not_make_fails_even_for_official():
    vo = "Rockstar just confirmed Lucia is in Vice City!"
    with patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "PASS")):
        ok, reason = hooks.check_hook_against_segment(
            _data("WHAT IS LUCIA DOING IN VICE CITY?", vo=vo, auth="official"), LEAK_SEGMENT, "T", cfg=CFG)
    assert not ok and "voiceover" in reason and "CONFIRMED" in reason


def test_the_voiceover_needs_the_same_content_overlap():
    vo = "Michael and Trevor rob a bank in Liberty City!"
    with patch("pipeline.hooks.verify_hook_grounding", return_value=(True, "PASS")):
        ok, reason = hooks.check_hook_against_segment(
            _data("WHAT IS LUCIA DOING IN VICE CITY?", vo=vo), LEAK_SEGMENT, "T", cfg=CFG)
    assert not ok and reason.startswith("FAIL: the voiceover shares 1 of the 2")      # only CITY is said


def test_an_unverified_item_keeps_its_question_framed_hook_and_its_generic_question_passes():
    with patch("pipeline.hooks._call_gemini", return_value=json.dumps(_data("LUCIA IN VICE CITY"))):
        data = hooks.generate_hook_and_voiceover("x.mp4", "T", transcript_text=LEAK_SEGMENT, frames=[b"f"],
                                                 authenticity="unverified", cfg=CFG)
    assert data["hook_text"].endswith("?")
    for hook in ("IS THIS REALLY GTA 6?", hooks.FALLBACK_VOICEOVER):     # "really" is not the claim "real"
        assert hooks.claim_check(hook, LEAK_SEGMENT, "unverified", CFG)[0] == "PASS"


def test_word_lists_and_threshold_come_from_config():
    cfg = json.loads(json.dumps(CFG))
    cfg["hook_check"]["min_shared_content_words"] = 1
    assert hooks.segment_overlap_check("BANK HEISTS ARE BACK?", "you can rob banks again", cfg)[0] == "PASS"
    cfg["hook_check"]["claim_words"] = ["leak"]
    assert hooks.claim_check("CONFIRMED LUCIA?", LEAK_SEGMENT, "unverified", cfg)[0] == "PASS"
    cfg["hook_check"]["generic_topic_words"] = []
    assert "VERSION" in hooks.content_words(TRIDZO_HOOK, cfg)
