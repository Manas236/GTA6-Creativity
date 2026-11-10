"""Speech-safe endings (pipeline/endings.py) through the cut stage, render snap, A4 and A3.

Every word list here is synthetic; nothing calls whisper, ffmpeg or the network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import config, cut, db, edits, endings, hooks, plan as planning, render, style

ES = endings.ending_settings({"cut": {}})
SS = style.style_settings()
from pipeline import transcribe  # noqa: E402

MODEL = transcribe.cache_key()          # the transcript cache key (model + glossary)


def W(word: str, start: float, end: float) -> dict:
    return {"word": word, "start": float(start), "end": float(end), "prob": 0.9}


def continuous(t0: float, t1: float, step: float = 0.25, word: str = "word") -> list[dict]:
    """Back-to-back words with no gap and no punctuation: speech that never pauses."""
    out, t = [], t0
    while t < t1 - 1e-9:
        out.append(W(word, t, min(t + step, t1)))
        t += step
    return out


def scan(windows, total=120.0, loudness=None) -> cut.CutScan:
    return cut.CutScan(total, loudness or [], [], [cut.CutWindow(s, s + 20.0, sc) for s, sc in windows],
                       18.0, 22.0, 20.0)


# ----------------------------------------------------------------------------- the rule

@pytest.mark.parametrize("word,expected", [
    ("so.", True), ("what?", True), ("now!", True), ('done."', True), ("level.)", True),
    ("packs,", False), ("wait...", False), ("then…", False), ("Mr.", False), ("so", False),
])
def test_ends_sentence(word, expected):
    assert endings.ends_sentence(word, ES) is expected


def test_a_word_gap_is_not_a_sentence_end_but_punctuation_or_a_long_pause_is():
    words = [W("going", 10.0, 10.4), W("to", 10.8, 11.0),          # 0.4 s gap, no punctuation
             W("done.", 11.0, 11.4), W("Next", 11.6, 12.0),        # 0.2 s after a full stop
             W("and", 12.0, 12.3), W("then", 13.0, 13.4)]          # 0.7 s pause
    breaks = endings.sentence_breaks(words, 14.0, ES)
    reasons = {round(b.word_end, 2): b.reason for b in breaks if b.word_end > 0}
    assert 10.4 not in reasons
    assert reasons[11.4] == "punct" and reasons[12.3] == "pause" and reasons[13.4] == "pause"


# ----------------------------------------------------------------------------- cut stage, one window

def test_continuous_speech_across_the_target_has_no_clean_end():
    words = continuous(0.0, 26.0)
    sc = scan([(0.0, 1.0)])
    assert cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES) is None


def test_sentence_end_at_19_4_beats_21_8_for_a_20s_target():
    words = continuous(0.0, 19.0) + [W("done.", 19.0, 19.4)] + continuous(19.7, 21.4) + \
        [W("again.", 21.4, 21.8)] + continuous(22.1, 25.0)
    sc = scan([(0.0, 1.0)])
    choice = cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES)
    assert choice is not None and choice.kind == "sentence"
    assert 19.4 + ES["min_after_word_s"] - 1e-6 <= choice.end <= 19.7 - ES["next_word_margin_s"] + 1e-6
    assert not endings.speaks_through(choice.end, words)
    # the cached words are on the cut timeline and stop at the end
    assert choice.words[-1]["word"] == "done." and all(w["start"] < choice.end for w in choice.words)


def test_only_a_later_sentence_end_is_taken_when_it_is_the_only_one():
    words = continuous(0.0, 21.4) + [W("again.", 21.4, 21.8)] + continuous(22.1, 25.0)
    sc = scan([(0.0, 1.0)])
    choice = cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES)
    assert choice is not None and 21.8 < choice.end < 22.1


def test_equal_distance_prefers_the_earlier_end():
    spans = [endings.SafeSpan(19.0, 19.0, 18.9, 25.0, "punct"), endings.SafeSpan(21.0, 21.0, 20.9, 25.0, "punct")]
    assert endings.choose_end(spans, 20.0, ES) == pytest.approx(19.0)


def test_no_speech_window_keeps_the_loudness_trough():
    loud = [(t / 10.0, -20.0) for t in range(0, 300)]
    loud[205] = (20.5, -60.0)                                    # one quiet trough at 20.5 s
    sc = scan([(0.0, 1.0)], loudness=loud)
    choice = cut.clean_end_for_window(sc.windows[0], sc, [], 24.0, ES)
    expected = cut.snap_end_to_pause(0.0, 20.0, loudness=loud, min_dur=18.0, max_dur=22.0, hard_max_end=120.0)
    assert choice.kind == "no_speech" and choice.end == pytest.approx(round(expected, 2))
    assert choice.words == []


def test_speech_that_stops_early_ends_in_the_silence_after_it():
    words = continuous(0.0, 6.0) + [W("over", 6.0, 6.3)]          # talking stops at 6.3 s
    sc = scan([(0.0, 1.0)])
    choice = cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES)
    assert choice.kind == "sentence" and choice.end == pytest.approx(20.0)


# ----------------------------------------------------------------------------- cut stage, run_cut_item

def _seed_item(tmp_db: Path, tmp_path: Path, transcript: dict | None = None) -> Path:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_c", "source": "youtube", "source_id": "vid_c",
                               "source_url": "https://example.invalid/c", "author_name": "Creator",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    extra = {"download_path": str(src)}
    if transcript is not None:
        extra.update(cut_start=transcript["cut_start"], cut_end=transcript["cut_end"],
                     transcript_words=json.dumps(transcript))
    db.transition_stage("item_c", "downloaded", extra_fields=extra, db_path=tmp_db)
    return src


def _run_cut(tmp_db, tmp_path, sc, transcriber):
    def fake_extract(_src, a, b, out):
        Path(out).write_bytes(b"cut")
    with patch.object(cut, "scan_cut_windows", return_value=sc), \
         patch.object(cut, "_extract_cut", side_effect=fake_extract), \
         patch.object(cut, "verify_video_file", return_value=(True, 19.5)), \
         patch.object(cut.dedupe, "extract_1fps_hashes", return_value=[(0, 1), (1, 2)]):
        return cut.run_cut_item("item_c", db_path=tmp_db, transcriber=transcriber)


def test_no_sentence_end_in_the_best_window_moves_to_the_next_best(tmp_db, tmp_path):
    _seed_item(tmp_db, tmp_path)
    sc = scan([(10.0, 0.9), (50.0, 0.8)])
    calls = []

    def transcriber(_p, a, b):
        calls.append((a, b))
        if a == 10.0:
            return continuous(a, b)                              # talks straight through
        return continuous(a, a + 19.0) + [W("finished.", a + 19.0, a + 19.4)] + continuous(a + 19.8, b)

    ok, msg = _run_cut(tmp_db, tmp_path, sc, transcriber)
    assert ok, msg
    item = db.get_item("item_c", db_path=tmp_db)
    assert item["stage"] == "cut" and item["cut_start"] == 50.0
    assert 69.4 < item["cut_end"] < 69.8
    assert calls == [(10.0, 34.0), (50.0, 74.0)]                 # window + max 22 s + 2 s tail


def test_no_window_with_a_sentence_end_fails_with_no_clean_ending(tmp_db, tmp_path):
    _seed_item(tmp_db, tmp_path)
    sc = scan([(10.0, 0.9), (50.0, 0.8), (90.0, 0.7)])
    ok, msg = _run_cut(tmp_db, tmp_path, sc, lambda _p, a, b: continuous(a, b))
    assert not ok and msg.startswith("no_clean_ending")
    item = db.get_item("item_c", db_path=tmp_db)
    assert item["stage"] == "failed" and item["error_stage"] == "cut"
    assert item["error_message"].startswith("no_clean_ending")
    assert not (tmp_path / "media" / "cuts" / "item_c_cut.mp4").exists()


def test_whisper_failure_fails_the_cut_rather_than_shipping_an_unchecked_end(tmp_db, tmp_path):
    _seed_item(tmp_db, tmp_path)

    def broken(_p, a, b):
        raise RuntimeError("model cache missing")

    ok, msg = _run_cut(tmp_db, tmp_path, scan([(10.0, 0.9)]), broken)
    assert not ok and "Whisper" in msg
    assert db.get_item("item_c", db_path=tmp_db)["error_stage"] == "cut"


def test_cut_caches_its_words_for_hooks_and_a_new_window_invalidates_the_old_cache(tmp_db, tmp_path):
    old = {"cut_start": 5.0, "cut_end": 25.0, "key": MODEL, "words": [W("stale", 0.0, 0.5)]}
    _seed_item(tmp_db, tmp_path, transcript=old)
    before = db.get_item("item_c", db_path=tmp_db)
    assert render.cached_cut_words(before, MODEL) == old["words"]

    def transcriber(_p, a, b):
        return continuous(a, a + 18.6) + [W("done.", a + 18.6, a + 19.0)] + continuous(a + 19.4, b)

    ok, msg = _run_cut(tmp_db, tmp_path, scan([(30.0, 0.9)]), transcriber)
    assert ok, msg
    item = db.get_item("item_c", db_path=tmp_db)
    payload = json.loads(item["transcript_words"])
    assert (payload["cut_start"], payload["cut_end"]) == (item["cut_start"], item["cut_end"]) != (5.0, 25.0)
    assert payload["key"] == MODEL and payload["ending"] == "sentence"
    words = render.cached_cut_words(item, MODEL)
    assert words and words[-1]["word"] == "done." and words[0]["start"] == pytest.approx(0.0)
    assert render.cut_end_verified(item, MODEL) is True
    # the old window's cache is gone, and would not validate against the new window anyway
    assert render.cached_cut_words({**item, "transcript_words": json.dumps(old)}, MODEL) is None

    # hooks reuses the cut-stage words: whisper is never called again
    with patch.object(render, "transcribe_words", side_effect=AssertionError("re-transcribed")):
        assert hooks._cut_transcript(item, Path(item["cut_path"]), config.get_config(), tmp_db) == words


def test_a_cache_without_the_cut_stage_verdict_does_not_verify_the_end():
    item = {"cut_start": 1.0, "cut_end": 21.0,
            "transcript_words": json.dumps({"cut_start": 1.0, "cut_end": 21.0, "key": MODEL, "words": []})}
    assert render.cut_end_verified(item, MODEL) is False                       # hooks-written cache
    item2 = {**item, "cut_end": 22.0}
    item2["transcript_words"] = json.dumps({"cut_start": 1.0, "cut_end": 21.0, "key": MODEL,
                                            "words": [], "ending": "sentence"})
    assert render.cut_end_verified(item2, MODEL) is False                      # other window


# ----------------------------------------------------------------------------- render snap

CUT_WORDS = continuous(0.0, 18.2) + [W("one.", 18.2, 18.5)] + continuous(18.8, 20.3) + \
    [W("two.", 20.3, 20.6)] + continuous(20.8, 20.95)


def test_snap_render_end_never_cuts_before_the_last_completed_sentence():
    end = render.snap_render_end("unused.mp4", 21.0, CUT_WORDS, cfg={"cut": {}})
    assert end >= 20.6 + ES["min_after_word_s"] - 1e-6
    assert not endings.speaks_through(end, CUT_WORDS)


def test_snap_render_end_keeps_a_verified_cut_end():
    words = continuous(0.0, 20.5) + [W("done", 20.5, 20.8)]      # cut stage saw the gap after it
    assert render.snap_render_end("unused.mp4", 21.0, words, cfg={"cut": {}}, clean_cut_end=True) == 21.0
    with pytest.raises(endings.NoCleanEnding):                    # unverified: 0.2 s, no full stop
        render.snap_render_end("unused.mp4", 21.0, words, cfg={"cut": {}})


def test_snap_render_end_without_speech_is_unchanged():
    loud = [(t / 10.0, -20.0) for t in range(0, 220)]
    loud[190] = (19.0, -60.0)
    with patch.object(render, "extract_audio_loudness_profile", return_value=loud):
        end = render.snap_render_end("unused.mp4", 21.0, [], cfg={"cut": {}})
    assert end == pytest.approx(cut.snap_end_to_pause(0.0, 21.0, loudness=loud, min_dur=18.0,
                                                      max_dur=22.0, hard_max_end=21.0))


# ----------------------------------------------------------------------------- A4

def test_a4_never_moves_the_end_into_a_word():
    words = CUT_WORDS
    spans = endings.safe_spans(words, 18.0, 21.0, 21.0, ES)
    ref = 0
    hashes = [(round(i * 0.2, 1), 0xFFFF_FFFF_FFFF_FFFF) for i in range(106)]   # far from frame 1
    hashes[0] = (0.0, ref)
    hashes[95] = (19.0, ref)              # a perfect loop frame, but at 19.0 s someone is mid-word
    le = edits.choose_loop_end(hashes, 18.0, 21.0, words, SS, proposed_end=20.7, safe_spans=spans, es=ES)
    assert le is not None and le.end_s != pytest.approx(19.0)
    assert endings.span_at(le.end_s, spans) is not None and not endings.speaks_through(le.end_s, words)
    # without the restriction (the old behaviour) A4 would have picked the mid-word frame
    assert edits.choose_loop_end(hashes, 18.0, 21.0, words, SS, proposed_end=20.7).end_s == pytest.approx(19.0)


def test_a4_still_searches_freely_without_speech():
    hashes = [(round(i * 0.2, 1), 0xFF) for i in range(106)]
    hashes[0] = (0.0, 0)
    hashes[95] = (19.0, 0)
    assert edits.choose_loop_end(hashes, 18.0, 21.0, [], SS, proposed_end=20.0, safe_spans=None).end_s == \
        pytest.approx(19.0)


# ----------------------------------------------------------------------------- A3

def _silence(a: float, b: float, dur: float = 20.0):
    loud = [(t / 10.0, -70.0 if a <= t / 10.0 < b else -20.0) for t in range(0, int(dur * 10) + 1)]
    motion = [(t / 10.0, 0.0) for t in range(0, int(dur * 10) + 1)]
    return loud, motion


def test_a3_keeps_a_quiet_stretch_inside_a_sentence():
    loud, motion = _silence(5.0, 6.4)                             # the meter hears 1.4 s of nothing...
    words = [W("we", 4.6, 5.0), W("were", 5.3, 5.6), W("there", 6.0, 6.4)]   # ...whisper hears words
    ss = {**SS, "dead_air": {**SS["dead_air"], "loudness_lag_s": 0.0}}
    assert edits.plan_dead_air(20.0, loud, motion, ss).dropped                 # old: dropped
    assert edits.plan_dead_air(20.0, loud, motion, ss, words=words, es=ES).dropped == []


def test_a3_still_drops_silence_between_sentences():
    loud, motion = _silence(5.0, 6.4)
    words = [W("over.", 4.4, 4.9), W("Next", 6.5, 6.9)]
    ss = {**SS, "dead_air": {**SS["dead_air"], "loudness_lag_s": 0.0}}
    tl = edits.plan_dead_air(20.0, loud, motion, ss, words=words, es=ES)
    assert tl.dropped and all(endings.gap_is_sentence_break(s, e, words, ES) for s, e in tl.dropped)


def test_a3_never_drops_between_words_without_a_sentence_break():
    words = [W("so", 4.0, 4.4), W("then", 4.95, 5.3)]             # 0.55 s gap, no full stop
    assert endings.gap_is_sentence_break(4.5, 4.9, words, ES) is False
    assert endings.gap_is_sentence_break(4.5, 4.9, [W("so.", 4.0, 4.4), W("then", 4.95, 5.3)], ES) is True


# ----------------------------------------------------------------------------- end fade

def test_end_fade_never_reaches_back_into_the_last_word():
    words = continuous(0.0, 19.0) + [W("done.", 19.0, 19.4)]
    sp = planning.plan_from_words("HOOK", 19.5, words, preset="P1")
    assert sp.audio_fade_s <= 19.5 - 19.4 + 1e-6


# ----------------------------------------------------------------------------- whisper cache

def test_whisper_falls_back_to_the_default_cache_when_hf_home_is_on_a_missing_drive(monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))                 # exists (its drive does)
    assert render.whisper_download_root() is None
    monkeypatch.setenv("HF_HOME", "Q:/no_such_drive/hf" if Path("C:/").exists() else "/no_such_root/hf")
    root = render.whisper_download_root()
    if Path("C:/").exists():                                          # Windows: Q: is not mounted
        assert root and root.endswith(str(Path(".cache") / "huggingface" / "hub"))
    monkeypatch.delenv("HF_HOME")
    assert render.whisper_download_root() is None


# ----------------------------------------------------------------------------- audio-measured pauses

def _env(dur: float, dips: list[tuple[float, float]], speech_db: float = -20.0, dip_db: float = -42.0):
    hop = 0.01
    n = int(dur / hop) + 1
    db_vals = [dip_db if any(a <= i * hop < b for a, b in dips) else speech_db for i in range(n)]
    return endings.Envelope(0.0, hop, db_vals)


def test_a_full_stop_with_no_whisper_gap_counts_when_the_audio_pauses():
    # whisper: "so." ends 19.00 and "The" starts 19.00 (0.00 s); the audio dips 19.00-19.25
    words = continuous(0.0, 18.8) + [W("so.", 18.8, 19.0), W("The", 19.0, 19.3)] + continuous(19.3, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES) == []                       # whisper alone: none
    spans = endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(19.0, 19.25)]))
    assert len(spans) == 1 and spans[0].reason == "punct_audio"
    end = endings.choose_end(spans, 20.0, ES)
    assert 19.05 <= end <= 19.22                                                       # inside the measured pause


def test_a_measured_pause_after_a_word_without_a_full_stop_is_not_an_end():
    words = continuous(0.0, 18.8) + [W("so", 18.8, 19.0), W("the", 19.0, 19.3)] + continuous(19.3, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(19.0, 19.25)])) == []


def test_a_dip_shorter_than_audio_gap_min_s_is_a_consonant_not_a_pause():
    words = continuous(0.0, 18.8) + [W("so.", 18.8, 19.0), W("Take", 19.0, 19.3)] + continuous(19.3, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(19.10, 19.15)])) == []


def test_the_cut_stage_uses_the_measured_pause():
    words = continuous(0.0, 18.8) + [W("so.", 18.8, 19.0), W("The", 19.0, 19.3)] + continuous(19.3, 25.0)
    sc = scan([(0.0, 1.0)])
    assert cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES) is None
    choice = cut.clean_end_for_window(sc.windows[0], sc, words, 24.0, ES, env=_env(25.0, [(19.0, 19.25)]))
    assert choice is not None and 19.05 <= choice.end <= 19.22 and choice.words[-1]["word"] == "so."


def test_windows_snapped_to_the_same_start_are_tried_once():
    with patch.object(cut, "verify_video_file", return_value=(True, 120.0)), \
         patch.object(cut, "extract_scene_cuts", return_value=[30.5, 60.0, 90.0]), \
         patch.object(cut, "extract_audio_loudness_profile", return_value=[(t, -20.0) for t in range(120)]):
        sc = cut.scan_cut_windows("unused.mp4", cfg={"cut": {}})
    starts = [w.start for w in sc.windows]
    assert len(starts) == len(set(starts)) and 30.5 in starts


def test_a_whisper_gap_after_a_full_stop_without_an_audio_pause_is_not_an_end():
    # The item_1032 case: whisper puts 0.2 s between "online." and "So", the audio runs straight on.
    words = continuous(0.0, 18.6) + [W("online.", 18.6, 19.0), W("So", 19.2, 19.4)] + continuous(19.4, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES)                                  # whisper alone: yes
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [])) == []       # the audio: no
    spans = endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(19.0, 19.25)]))
    assert len(spans) == 1 and spans[0].reason == "punct_audio" and 19.0 < spans[0].start < spans[0].end < 19.25


def test_a_comma_length_pause_after_a_full_stop_is_not_enough():
    # item_5a67: "wanted level" + 0.15 s; whisper wrote "level." and "level," by turns.
    words = continuous(0.0, 18.6) + [W("level.", 18.6, 19.0), W("They're", 19.15, 19.4)] + continuous(19.4, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(18.98, 19.13)])) == []


def test_a_cut_verified_under_an_older_rule_is_not_trusted():
    payload = {"cut_start": 1.0, "cut_end": 21.0, "key": MODEL, "words": [], "ending": "sentence"}
    item = {"cut_start": 1.0, "cut_end": 21.0, "transcript_words": json.dumps(payload)}
    assert render.cut_end_verified(item, MODEL) is False                                     # rule 1
    item["transcript_words"] = json.dumps({**payload, "ending_rule": endings.RULE_VERSION})
    assert render.cut_end_verified(item, MODEL) is True


def test_the_end_fade_fits_inside_the_measured_quiet_tail():
    env = _env(21.0, [(20.9, 21.0)])
    assert endings.quiet_tail_s(20.98, env, ES) == pytest.approx(0.08, abs=0.011)
    assert endings.quiet_tail_s(20.5, env, ES) == 0.0
    assert endings.quiet_tail_s(20.98, None, ES) is None


def test_a_verified_cut_end_with_many_decimals_is_kept_and_found():
    # Real cut durations are not on a millisecond grid (e.g. 18.98567 s); rounding the chosen end
    # up must not push it out of the one-point verified span (P0 failed on two real items).
    dur = 18.98567
    words = continuous(0.0, 18.6) + [W("done", 18.6, 18.9)]
    end, spans = endings.content_end(words, dur, 18.0, 22.0, ES, trusted_end=dur)
    assert end <= dur and endings.span_at(end, spans) is not None
    assert end == pytest.approx(dur, abs=1e-3)


def test_a_long_whisper_pause_with_loud_audio_is_not_an_end():
    # Whisper: 0.7 s between "download," and "one"; the audio is loud through it (the item_1032 case).
    words = continuous(0.0, 18.3) + [W("download,", 18.3, 18.6), W("one", 19.3, 19.5)] + continuous(19.5, 25.0)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES)                                  # whisper alone: yes
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [])) == []       # loud: no
    # a 0.3 s real pause inside whisper's "0.7 s gap" is not a >= 0.6 s gap (the measured item_1032 case)
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(18.75, 19.05)], dip_db=-30.0)) == []
    spans = endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [(18.65, 19.3)], dip_db=-30.0))
    assert len(spans) == 1 and spans[0].reason == "pause" and 18.65 < spans[0].start < spans[0].end < 19.3


def test_a_long_whisper_silence_over_loud_audio_is_not_an_end_unless_explicitly_enabled():
    words = continuous(0.0, 10.0) + [W("over", 10.0, 10.3)]          # whisper hears nobody after 10.3 s
    # Off by default (rule 5): whisper once dropped 42 words and "heard nobody" over live speech.
    assert endings.safe_spans(words, 18.0, 22.0, 24.0, ES, env=_env(25.0, [])) == []
    on = {**ES, "no_speech_gap_s": 2.0}
    spans = endings.safe_spans(words, 18.0, 22.0, 24.0, on, env=_env(25.0, []))
    assert spans and spans[0].reason == "pause" and spans[0].start == 18.0


def test_the_verified_cut_end_only_reaches_back_over_its_measured_quiet_tail():
    words = continuous(0.0, 18.3) + [W("download,", 18.3, 18.6)]
    loud = endings.safe_spans(words, 18.0, 19.0, 19.0, ES, trusted_end=19.0, env=_env(19.0, []))
    assert [(s.reason, s.start, s.end) for s in loud] == [("verified", 19.0, 19.0)]       # just the point
    quiet = endings.safe_spans(words, 18.0, 19.0, 19.0, ES, trusted_end=19.0, env=_env(19.0, [(18.8, 19.01)]))
    v = [s for s in quiet if s.reason == "verified"][0]
    assert 18.8 < v.start < 18.85 and v.end == 19.0


def test_near_copies_of_a_window_without_a_sentence_end_are_skipped(tmp_db, tmp_path):
    _seed_item(tmp_db, tmp_path)
    sc = scan([(10.0, 0.9), (11.0, 0.88), (11.8, 0.87), (60.0, 0.5)])
    calls = []

    def transcriber(_p, a, b):
        calls.append(a)
        if a < 50:
            return continuous(a, b)
        return continuous(a, a + 19.0) + [W("finished.", a + 19.0, a + 19.4)] + continuous(a + 19.8, b)

    es = {**ES, "max_windows": 2}
    with patch.object(cut.endings, "ending_settings", return_value=es):
        ok, msg = _run_cut(tmp_db, tmp_path, sc, transcriber)
    assert ok, msg
    assert calls == [10.0, 60.0]                       # 11 and 11.8 s were near-copies of the failed 10 s
    assert db.get_item("item_c", db_path=tmp_db)["cut_start"] == 60.0
