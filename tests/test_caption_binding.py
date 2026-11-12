"""The wrong-caption bug: a hook/voiceover written for one item or one segment must never be rendered
over another. Covers pipeline.binding, the hard render-time guard, the cut stage clearing a moved
window's hook, the segment-only hook prompt, the post-generation check and the manual-review flow.
Zero network, no ffmpeg renders."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pipeline import binding, config, cut, db, hooks, render, telegram_bot, transcribe, voiceover

CFG = config.get_config()
KEY = transcribe.cache_key(CFG)
ROCKSTAR = "UC6VcWc1rAoWdBCM0JxrRQ3A"       # listed in sources.official_channels

SEG_WORDS = [{"word": w, "start": 0.4 + i * 0.4, "end": 0.7 + i * 0.4, "prob": 0.9}
             for i, w in enumerate(["Lucia", "drives", "through", "Vice", "City."])]
GOOD = {"authenticity": "unverified", "hook_style": "question", "hook_text": "IS LUCIA BACK IN VICE CITY?",
        "voiceover_script": "Lucia might be back in Vice City, look at this!", "keywords": ["LUCIA", "VICE", "CITY"]}


# ----------------------------------------------------------------------------- fixtures

def make_item(tmp_db: Path, tmp_path: Path, item_id: str, start: float = 10.0, end: float = 30.0,
              hook: str = "IS LUCIA BACK IN VICE CITY?", script: str = "Lucia is back!", words=SEG_WORDS,
              stage: str = "cut", channel_id: str = "UCcreator", bind: bool = True) -> dict[str, Any]:
    """An item cut to [start, end] whose hook and VO were made for exactly that segment."""
    cut_p, vo = tmp_path / f"{item_id}_cut.mp4", tmp_path / f"{item_id}_vo.wav"
    cut_p.write_bytes(b"0")
    vo.write_bytes(b"0")
    db.insert_discovered_item({"id": item_id, "source": "youtube", "source_id": item_id, "channel_id": channel_id,
                               "source_url": f"https://youtu.be/{item_id}", "author_name": "Creator",
                               "original_title": "GTA 6 ONLINE MULTIPLAYER LEAKED", "raw_velocity": 1.0}, db_path=tmp_db)
    seg = {"id": item_id, "cut_path": str(cut_p), "cut_start": start, "cut_end": end}
    fields: dict[str, Any] = {"cut_path": str(cut_p), "cut_start": start, "cut_end": end, "cut_duration": end - start,
                              "hook_text": hook, "hook_style": "question", "voiceover_script": script,
                              "voiceover_audio_path": str(vo), "keywords": '["LUCIA", "VICE", "CITY"]'}
    if bind:
        fields["hook_binding"] = binding.hook_record(seg, hook, script, binding.transcript_text(words), "segment_check")
        fields["voiceover_binding"] = binding.voiceover_record(seg, script, vo)
    db.transition_stage(item_id, stage, extra_fields=fields, db_path=tmp_db)
    render.store_cut_words(item_id, db.get_item(item_id, db_path=tmp_db), words, KEY, db_path=tmp_db)
    return db.get_item(item_id, db_path=tmp_db)


def _variants(item, hook_text, tmp_path):
    """render_item_variants with everything after the guard stubbed out (no ffmpeg)."""
    return render.render_item_variants(
        item["cut_path"], item["voiceover_audio_path"], hook_text, SEG_WORDS, [],
        tmp_path / "A.mp4", tmp_path / "B.mp4", tmp_path / "subs", cfg=CFG, name=item["id"], item=item,
    )


# ----------------------------------------------------------------------------- the hard render guard

def test_mixup_hook_text_of_another_item_is_refused_before_anything_renders(tmp_db, tmp_path):
    """Cross-item mixup: item A's clip with item B's hook (both rows individually valid)."""
    a = make_item(tmp_db, tmp_path, "item_a")
    b = make_item(tmp_db, tmp_path, "item_b", hook="IS THIS THE NEW ONLINE MODE?")
    with patch.object(render.planning, "analyze_clip") as analyze:
        with pytest.raises(render.CaptionMismatchError, match="not the text the record was made for"):
            _variants(a, b["hook_text"], tmp_path)
    analyze.assert_not_called()                                   # refused before any analysis/ffmpeg
    assert not (tmp_path / "A.mp4").exists() and not (tmp_path / "B.mp4").exists()


def test_mixup_caption_record_copied_from_another_item_is_refused(tmp_db, tmp_path):
    """A row that carries another item's hook + record (e.g. a reused key / misaligned batch)."""
    a = make_item(tmp_db, tmp_path, "item_a")
    b = make_item(tmp_db, tmp_path, "item_b", hook="IS THIS THE NEW ONLINE MODE?")
    swapped = {**a, "hook_text": b["hook_text"], "hook_binding": b["hook_binding"]}
    with pytest.raises(render.CaptionMismatchError, match="belongs to item 'item_b', not 'item_a'"):
        _variants(swapped, swapped["hook_text"], tmp_path)


def test_hook_made_for_an_older_window_is_refused(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_a", start=10.0, end=30.0)
    moved = {**item, "cut_start": 112.4, "cut_end": 132.1}          # the window moved, the hook did not
    with pytest.raises(render.CaptionMismatchError, match=r"cut_start=10.0.*cut_start=112.4"):
        _variants(moved, moved["hook_text"], tmp_path)


def test_rendering_a_different_cut_file_is_refused(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_a")
    other = tmp_path / "item_b_cut.mp4"
    other.write_bytes(b"0")
    with pytest.raises(render.CaptionMismatchError, match="the hook was made from"):
        render.assert_item_caption_binding(item, item["hook_text"], other, item["voiceover_audio_path"], CFG)


def test_voiceover_spoken_from_another_script_is_refused(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_a")
    newer = {**item, "voiceover_script": "A different line for a different hook."}
    with pytest.raises(render.CaptionMismatchError, match="different script"):
        render.assert_item_caption_binding(newer, item["hook_text"], item["cut_path"], item["voiceover_audio_path"], CFG)


def test_segment_transcript_changed_since_the_hook_is_refused(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_a")
    other_words = [{"word": "Jason", "start": 0.2, "end": 0.6}, {"word": "swims.", "start": 0.6, "end": 1.0}]
    render.store_cut_words("item_a", item, other_words, KEY, db_path=tmp_db)
    with pytest.raises(render.CaptionMismatchError, match="segment transcript differs"):
        _variants(db.get_item("item_a", db_path=tmp_db), item["hook_text"], tmp_path)


def test_unchecked_or_unbound_hooks_are_refused(tmp_db, tmp_path):
    legacy = make_item(tmp_db, tmp_path, "item_old", bind=False)              # a pre-fix row
    assert "no segment record" in binding.hook_binding_problem(legacy)
    review = make_item(tmp_db, tmp_path, "item_r")
    review["hook_binding"] = binding.with_check(review["hook_binding"], "needs_review")
    with pytest.raises(render.CaptionMismatchError, match="needs_review"):
        _variants(review, review["hook_text"], tmp_path)


def test_matching_item_and_segment_pass_the_guard(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_a")
    render.assert_item_caption_binding(item, item["hook_text"], item["cut_path"], item["voiceover_audio_path"], CFG)


# ----------------------------------------------------------------------------- run_render_item

def _render_env(tmp_path):
    stack = contextlib.ExitStack()
    for p in (patch.object(render.config, "media_dir", side_effect=lambda sub="": tmp_path / sub),
              patch.object(render, "verify_video_file", return_value=(True, 20.0)),
              patch.object(render, "media_duration", return_value=3.0),
              patch.object(render, "transcribe_words", return_value=[])):
        stack.enter_context(p)
    return stack


def test_run_render_item_regenerates_a_hook_left_over_from_an_older_window(tmp_db, tmp_path):
    """THE BUG: the window moved (re-cut) but hook_text was still set, so render reused it.
    Now the stale record makes render run the hooks stage for the new segment first."""
    make_item(tmp_db, tmp_path, "item_a", start=10.0, end=30.0, hook="LUCIA ROBS A BANK?")
    with db.db(tmp_db) as conn:                               # what a re-cut did before the cut-stage fix
        conn.execute("UPDATE items SET cut_start = 41.2, cut_end = 61.0 WHERE id = 'item_a'")
    item = db.get_item("item_a", db_path=tmp_db)
    render.store_cut_words("item_a", item, SEG_WORDS, KEY, db_path=tmp_db)

    def fresh_hooks(item_id, cfg=None, db_path=None):
        it = db.get_item(item_id, db_path=db_path)
        with db.db(db_path) as conn:
            conn.execute("UPDATE items SET hook_text = ?, voiceover_script = ?, hook_binding = ?, "
                         "voiceover_audio_path = NULL, voiceover_binding = NULL WHERE id = ?",
                         (GOOD["hook_text"], GOOD["voiceover_script"],
                          binding.hook_record(it, GOOD["hook_text"], GOOD["voiceover_script"],
                                              binding.transcript_text(SEG_WORDS), "segment_check"), item_id))
        return True, "ok"

    def fresh_vo(item_id, cfg=None, db_path=None):
        it = db.get_item(item_id, db_path=db_path)
        wav = tmp_path / f"{item_id}_vo2.wav"
        wav.write_bytes(b"0")
        with db.db(db_path) as conn:
            conn.execute("UPDATE items SET voiceover_audio_path = ?, voiceover_binding = ? WHERE id = ?",
                         (str(wav), binding.voiceover_record(it, it["voiceover_script"], wav), item_id))
        return True, "ok"

    burned: dict[str, Any] = {}

    def guarded_variants(*a, **k):                # the real guard, then a fake render
        render.assert_item_caption_binding(k["item"], a[2], a[0], a[1], CFG)
        burned.update(hook=a[2], item=k["item"]["id"])
        return {"elapsed_a_s": 1.0, "elapsed_b_s": 1.0, "probe_a": {"video_duration": 19.8},
                "probe_b": {"video_duration": 23.0}, "preset": "P0"}

    with _render_env(tmp_path), \
         patch.object(render.hooks, "run_hooks_for_item", side_effect=fresh_hooks) as hk, \
         patch.object(render.voiceover, "run_voiceover_for_item", side_effect=fresh_vo) as vo, \
         patch.object(render, "render_item_variants", side_effect=guarded_variants):
        ok, msg, _ = render.run_render_item("item_a", cfg=CFG, db_path=tmp_db)
    assert ok, msg
    hk.assert_called_once()
    vo.assert_called_once()                          # the old wav was spoken from the old script
    assert burned == {"hook": GOOD["hook_text"], "item": "item_a"}      # never "LUCIA ROBS A BANK?"


def test_run_render_item_fails_loudly_and_marks_the_item_on_a_mismatch(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_a")
    item = db.get_item("item_a", db_path=tmp_db)
    render.store_cut_words("item_a", item, [{"word": "Jason", "start": 0.1, "end": 0.5}], KEY, db_path=tmp_db)
    with _render_env(tmp_path), patch.object(render.planning, "analyze_clip") as analyze:
        ok, msg, _ = render.run_render_item("item_a", cfg=CFG, db_path=tmp_db)
    assert not ok and msg.startswith("caption_mismatch for item_a")
    analyze.assert_not_called()
    row = db.get_item("item_a", db_path=tmp_db)
    assert row["stage"] == "failed" and row["error_stage"] == "render" and "caption_mismatch" in row["error_message"]


# ----------------------------------------------------------------------------- the cut stage

def _talker(total, ends, word_s=0.25, gap_s=0.3):
    words, t = [], 0.0
    while t < total - word_s:
        nxt = next((e for e in sorted(ends) if t < e <= t + word_s + 1e-9), None)
        if nxt is not None:
            words.append({"word": "done.", "start": round(t, 3), "end": round(nxt, 3), "prob": 0.9})
            t = nxt + gap_s
            continue
        words.append({"word": "word", "start": round(t, 3), "end": round(t + word_s, 3), "prob": 0.9})
        t += word_s
    return words


def _run_cut(tmp_db, item_id, total, words):
    def fake_extract(_src, a, b, out):
        Path(out).write_bytes(b"cut")
    sc = cut.CutScan(total, [], [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 22.0, 20.0)
    with patch.object(cut, "scan_cut_windows", return_value=sc), \
         patch.object(cut, "_extract_cut", side_effect=fake_extract), \
         patch.object(cut, "verify_video_file", return_value=(True, 19.5)), \
         patch.object(cut.dedupe, "extract_1fps_hashes", return_value=[(0, 1), (1, 2)]), \
         patch.object(cut.db, "get_stored_fingerprints_by_item", return_value={}), \
         patch.object(telegram_bot, "send_alert", return_value=True):
        return cut.run_cut_item(item_id, cfg=CFG, db_path=tmp_db, transcriber=lambda _p, a, b: words,
                                envelope=lambda *a: None)


def test_cut_stage_clears_the_old_windows_hook_vo_keywords_and_renders_when_the_window_moves(tmp_db, tmp_path):
    item = make_item(tmp_db, tmp_path, "item_c", start=100.0, end=120.0, stage="downloaded")
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 64)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE items SET download_path = ?, render_a_path = 'old_A.mp4', render_b_path = 'old_B.mp4', "
                     "caption_text = 'old caption' WHERE id = 'item_c'", (str(src),))
    ok, msg = _run_cut(tmp_db, "item_c", 45.0, _talker(45.0, [12.0, 24.5, 38.0]))
    assert ok, msg
    row = db.get_item("item_c", db_path=tmp_db)
    assert (row["cut_start"], row["cut_end"]) != (100.0, 120.0)
    for k in ("hook_text", "voiceover_script", "voiceover_audio_path", "keywords", "hook_binding",
              "voiceover_binding", "render_a_path", "render_b_path", "caption_text"):
        assert row[k] is None, k
    assert row["hook_grounded"] == 0 and item["hook_text"]          # it had one before the re-cut

    # A re-cut that lands on the SAME window keeps a hook made for it (no wasted Gemini calls).
    seg = {**row}
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE items SET hook_text = 'IS THIS IT?', voiceover_script = 'v', hook_binding = ? WHERE id = 'item_c'",
                     (binding.hook_record(seg, "IS THIS IT?", "v", "", "segment_check"),))
    ok, msg = _run_cut(tmp_db, "item_c", 45.0, _talker(45.0, [12.0, 24.5, 38.0]))
    assert ok, msg
    again = db.get_item("item_c", db_path=tmp_db)
    assert (again["cut_start"], again["cut_end"]) == (row["cut_start"], row["cut_end"])
    assert again["hook_text"] == "IS THIS IT?" and binding.hook_binding_problem(again) is None


# ----------------------------------------------------------------------------- the hooks stage

def _gemini(answers: list[str], prompts: list[tuple[str, Any]]):
    queue = list(answers)

    def fake(prompt, images=None, **kw):
        prompts.append((prompt, images))
        return queue.pop(0)
    return fake


def test_hook_prompt_gets_the_cut_segment_transcript_and_the_source_tag_not_the_whole_source(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_h", channel_id=ROCKSTAR, bind=False)
    with db.db(tmp_db) as conn:          # the whole-source transcript also sits on the row (cut stage)
        conn.execute("UPDATE items SET source_transcript = ? WHERE id = 'item_h'", (json.dumps(
            {"key": "x", "words": [{"word": "multiplayer", "start": 300.0, "end": 300.5}]}),))
    prompts: list[tuple[str, Any]] = []
    answers = [json.dumps({**GOOD, "authenticity": "official"}), "PASS"]
    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"frame"] * 4), \
         patch("pipeline.hooks._call_gemini", side_effect=_gemini(answers, prompts)):
        ok, msg = hooks.run_hooks_for_item("item_h", cfg=CFG, db_path=tmp_db)
    assert ok, msg
    gen, check = prompts[0][0], prompts[1][0]
    assert 'CLIP TRANSCRIPT (this segment only): "Lucia drives through Vice City."' in gen
    assert "multiplayer" not in gen.lower().replace("online multiplayer leaked", "")   # only in the title line
    assert 'AUTHENTICITY TAG: "official"' in gen and "Never change \"unverified\" to \"official\"" in gen
    assert "only refer to things\nthat are said in the CLIP TRANSCRIPT" in gen
    assert "The source title is NOT evidence" in check and prompts[1][1] == [b"frame"] * 4
    row = db.get_item("item_h", db_path=tmp_db)
    assert row["authenticity"] == "official" and row["hook_grounded"] == 1
    assert binding.hook_binding_problem(row) is None


def test_creator_clip_can_never_be_upgraded_to_official(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_h", bind=False)                 # a creator channel
    prompts: list[tuple[str, Any]] = []
    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks._call_gemini",
               side_effect=_gemini([json.dumps({**GOOD, "authenticity": "official"}), "PASS"], prompts)):
        ok, _ = hooks.run_hooks_for_item("item_h", cfg=CFG, db_path=tmp_db)
    assert ok and 'AUTHENTICITY TAG: "unverified"' in prompts[0][0]
    assert db.get_item("item_h", db_path=tmp_db)["authenticity"] == "unverified"


def test_off_segment_hook_fails_the_overlap_check_and_is_regenerated_once_with_the_reason(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_h", bind=False)
    prompts: list[tuple[str, Any]] = []
    off = {**GOOD, "hook_text": "ONLINE MULTIPLAYER LEAKED?"}          # what the TITLE is about, not the clip
    answers = [json.dumps(off), json.dumps(GOOD), "PASS"]
    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks._call_gemini", side_effect=_gemini(answers, prompts)):
        ok, msg = hooks.run_hooks_for_item("item_h", cfg=CFG, db_path=tmp_db)
    assert ok, msg
    assert len(prompts) == 3                    # gen, gen, check: the overlap FAIL cost no Gemini call
    assert "PREVIOUS ATTEMPT REJECTED" in prompts[1][0] and "ONLINE MULTIPLAYER LEAKED?" in prompts[1][0]
    row = db.get_item("item_h", db_path=tmp_db)
    assert row["hook_text"] == GOOD["hook_text"] and row["stage"] == "cut"
    assert json.loads(row["hook_binding"])["check"] == "segment_check"


@pytest.mark.parametrize("hook,transcript,verdict", [
    ("IS LUCIA BACK IN VICE CITY?", "Lucia drives through Vice City.", "PASS"),
    ("BANK HEISTS ARE BACK?", "you can rob banks again", "FAIL"),           # 1 of 2 content words said (was PASS: 1 was enough)
    ("BANK HEISTS ARE BACK?", "you can rob banks and do heists again", "PASS"),  # stems: BANK ~ banks, HEISTS ~ heists
    ("WHO IS LUCIA?", "Lucia drives through Vice City.", "PASS"),           # one content word, and it is said
    ("ONLINE MULTIPLAYER LEAKED?", "Lucia drives through Vice City.", "FAIL"),
    ("IS THIS REALLY GTA 6?", "Lucia drives through Vice City.", "PASS"),   # generic: claims nothing
    ("IS LUCIA BACK?", "", "SKIP"),                                          # no speech: Gemini + frames decide
])
def test_segment_overlap_check(hook, transcript, verdict):
    assert hooks.segment_overlap_check(hook, transcript)[0] == verdict


# ----------------------------------------------------------------------------- manual review on Telegram

class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.mid = 500

    def __call__(self, method, params, files=None, http_timeout=30.0):
        self.calls.append((method, params))
        if method in ("sendMessage", "sendVideo"):
            self.mid += 1
            return {"message_id": self.mid}
        return [] if method == "getUpdates" else True


def _tap(item_id, action, mid):
    return {"update_id": 1, "callback_query": {
        "id": "cb", "data": f"a:{action}:{item_id}", "from": {"id": 4242},
        "message": {"message_id": mid, "chat": {"id": 4242, "type": "private"}}}}


def test_two_failed_hooks_go_to_a_review_card_instead_of_rendering(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_h", bind=False)
    off = {**GOOD, "hook_text": "ONLINE MULTIPLAYER LEAKED?"}
    with patch("pipeline.hooks.extract_sample_frames", return_value=[b"f"]), \
         patch("pipeline.hooks._call_gemini", side_effect=_gemini([json.dumps(off), json.dumps(off)], [])):
        ok, msg = hooks.run_hooks_for_item("item_h", cfg=CFG, db_path=tmp_db)
    assert not ok and msg.startswith("needs_review")
    row = db.get_item("item_h", db_path=tmp_db)
    assert row["stage"] == "needs_review" and row["voiceover_audio_path"] is None

    # Not rendered, and not marked failed either: it waits for a human.
    with patch.object(render, "render_item_variants") as riv:
        ok, msg, _ = render.run_render_item("item_h", cfg=CFG, db_path=tmp_db)
    assert not ok and msg.startswith("needs_review") and riv.call_count == 0
    assert db.get_item("item_h", db_path=tmp_db)["stage"] == "needs_review"

    cfg = {**CFG, "telegram": {"enabled": True, "poll_timeout_s": 1, "edit_throttle_gap_s": 0}}
    t = FakeTransport()
    bot = telegram_bot.ApprovalBot(transport=t, chat_id="4242", cfg=cfg, db_path=tmp_db, clock=lambda: 0.0)
    assert bot.ensure_card() == "item_h"
    (method, params), = t.calls
    assert method == "sendMessage"                                         # text card: nothing was rendered
    assert "HOOK REVIEW" in params["text"] and "ONLINE MULTIPLAYER LEAKED?" in params["text"]
    assert "Lucia drives through Vice City." in params["text"]             # what the clip actually says
    assert [b["callback_data"] for row_ in params["reply_markup"]["inline_keyboard"] for b in row_] == \
        ["a:hookok:item_h", "a:no:item_h"]
    mid = t.mid
    assert "does not apply" in bot.on_button("a:ok:item_h", mid)          # cannot "approve for posting"
    assert db.get_item("item_h", db_path=tmp_db)["stage"] == "needs_review"
    assert bot.release_stuck_card() is False                               # a review card is not stuck

    bot.handle_update(_tap("item_h", "hookok", mid))
    row = db.get_item("item_h", db_path=tmp_db)
    assert row["stage"] == "cut" and row["hook_grounded"] == 1
    assert json.loads(row["hook_binding"])["check"] == "human_approved"
    assert binding.hook_binding_problem(row) is None                       # renderable now, same segment
    assert telegram_bot.card_state(tmp_db) is None
    assert any(m == "editMessageText" and "Hook approved" in p.get("text", "") for m, p in t.calls)


def test_auto_mode_rejects_needs_review_items(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_h", stage="needs_review")
    out = telegram_bot.run_auto_approval(cfg=CFG, db_path=tmp_db, probe=lambda p: 20.0)
    assert "item_h" in out["rejected"]
    assert db.get_item("item_h", db_path=tmp_db)["stage"] == "rejected"


# ----------------------------------------------------------------------------- voiceover stage

def test_voiceover_stage_binds_the_wav_to_its_script_and_segment(tmp_db, tmp_path):
    make_item(tmp_db, tmp_path, "item_v", bind=False)

    def synth(script, out_path, voice=None, speed=None):
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(b"wav")
        return 3.0

    with patch.object(voiceover, "synthesize_voiceover", side_effect=synth):
        ok, _ = voiceover.run_voiceover_for_item("item_v", cfg=CFG, db_path=tmp_db)
    assert ok
    row = db.get_item("item_v", db_path=tmp_db)
    assert binding.voiceover_binding_problem(row) is None
    assert "different script" in binding.voiceover_binding_problem({**row, "voiceover_script": "new line"})
