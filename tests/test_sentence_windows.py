"""Sentence-aligned cut windows (pipeline/sentence_windows.py + cut.run_cut_item): synthetic word
lists only - no whisper, no ffmpeg, no network."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import clock, config, cut, db, endings, runner, sentence_windows as sw_mod, telegram_bot, transcribe

ES = endings.ending_settings({"cut": {}})
SW = sw_mod.settings({"cut": {}})
CFG = config.get_config()


def W(word, start, end):
    return {"word": word, "start": round(float(start), 3), "end": round(float(end), 3), "prob": 0.9}


def talker(total: float, sentence_ends: list[float], word_s: float = 0.25, gap_s: float = 0.3) -> list[dict]:
    """A fast talker: back-to-back words, no pause anywhere except `gap_s` after each sentence end
    (a word ending in a full stop at each time in `sentence_ends`)."""
    words, t = [], 0.0
    ends = sorted(sentence_ends)
    while t < total - word_s:
        nxt = next((e for e in ends if t < e <= t + word_s + 1e-9), None)
        if nxt is not None:
            words.append(W("done.", t, nxt))
            t = nxt + gap_s
            continue
        words.append(W("word", t, t + word_s))
        t += word_s
    return words


def region(words, total, env=None):
    return sw_mod.Region(0.0, total, words, env)


def flat_scorer(total, loud=None, cuts=()):
    return sw_mod.Scorer(total, loud or [], list(cuts), {"cut": {}})


# ----------------------------------------------------------------------------- selection beats fixed windows

def test_sentence_aligned_selection_finds_ends_that_fixed_windows_miss():
    total = 120.0
    ends = [31.0, 41.0, 51.0, 69.5, 79.5, 95.0]          # nothing in 58-69 s
    words = talker(total, ends)
    # The loudest stretch is 40-62 s, so the old scorer's best fixed windows start at ~40-47 s and
    # look for an end in 58-69 s - where this speaker never pauses.
    loud = [(t / 10, -15.0 if 400 <= t <= 620 else -35.0) for t in range(0, 1200)]
    sc = cut.CutScan(total, loud, [], [cut.CutWindow(s, s + 20.0, 1.0 - s / 1000) for s in range(40, 48)],
                     18.0, 22.0, 20.0)
    old = [cut.clean_end_for_window(w, sc, words, min(total, w.start + 24.0), ES) for w in sc.windows]
    assert old == [None] * 8                              # the old loop: no_clean_ending

    cands, n_ends = sw_mod.candidates([region(words, total)], flat_scorer(total, loud), 18.0, 22.0, ES, SW)
    assert n_ends == len(ends) and cands
    best = cands[0]
    assert 18.0 - 1e-6 <= best.end - best.start <= 22.0 + 1e-6
    assert any(abs(best.end - (e + 0.15)) <= 0.16 for e in ends)          # it ends in a sentence pause
    assert not endings.speaks_through(best.end, words)
    assert best.start >= 40.0 - 22.0                       # and still prefers the loud part


def test_every_candidate_ends_in_a_clean_sentence_end_and_respects_the_length_bounds():
    total = 90.0
    words = talker(total, [12.0, 25.0, 33.0, 47.5, 60.0, 71.0, 84.0])
    cands, _ = sw_mod.candidates([region(words, total)], flat_scorer(total), 18.0, 22.0, ES, SW)
    assert cands
    spans = endings.safe_spans(words, 0.0, total, total, ES)
    for c in cands:
        assert 18.0 - 1e-6 <= c.end - c.start <= 22.0 + 1e-6 and c.start >= 0.0
        assert endings.span_at(c.end, spans) is not None


# ----------------------------------------------------------------------------- start bonus

def test_a_window_starting_at_a_sentence_start_gets_the_soft_bonus():
    total = 60.0
    words = talker(total, [5.5, 27.0, 45.0])
    plain = {**SW, "start_bonus": 0.0}
    no_bonus, _ = sw_mod.candidates([region(words, total)], flat_scorer(total), 18.0, 22.0, ES, plain)
    with_bonus, _ = sw_mod.candidates([region(words, total)], flat_scorer(total), 18.0, 22.0, ES, SW)
    pick = next(c for c in with_bonus if abs(c.end - 27.15) < 0.2)       # the window ending after 27 s
    assert pick.at_sentence_start and abs(pick.start - (5.8 - SW["start_lead_s"])) < 0.06
    plain_pick = next(c for c in no_bonus if abs(c.end - 27.15) < 0.2)
    assert pick.score == pytest.approx(plain_pick.score + SW["start_bonus"], abs=0.02)


def test_the_bonus_is_soft_a_much_better_window_still_wins():
    total = 60.0
    words = talker(total, [5.0, 27.0, 45.0])
    # loud only from 7 s on: starting at the sentence start (5.15 s) is quieter than starting later
    loud = [(t / 10, -45.0 if t < 70 else -12.0) for t in range(0, 600)]
    cands, _ = sw_mod.candidates([region(words, total)], flat_scorer(total, loud), 18.0, 22.0, ES,
                                 {**SW, "start_bonus": 0.001})
    pick = next(c for c in cands if abs(c.end - 27.15) < 0.2)
    assert not pick.at_sentence_start and pick.start > 7.0


# ----------------------------------------------------------------------------- run_cut_item end to end

def _seed(tmp_db, tmp_path, item_id="item_s"):
    src = tmp_path / f"{item_id}.mp4"
    src.write_bytes(b"0" * 64)
    db.insert_discovered_item({"id": item_id, "source": "twitch", "source_id": item_id,
                               "source_url": f"https://clips.twitch.tv/{item_id}", "author_name": "Streamer",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage(item_id, "downloaded", extra_fields={"download_path": str(src)}, db_path=tmp_db)
    return src


def _short_scan(total):
    """What scan_cut_windows returns for a source <= target_duration: the whole clip."""
    return cut.CutScan(total, [], [], [cut.CutWindow(0.0, total, 1.0)], 18.0, 22.0, 20.0)


def _run(tmp_db, tmp_path, sc, transcriber, item_id="item_s", dup=None, cfg=None):
    def fake_extract(_src, a, b, out):
        Path(out).write_bytes(b"cut")
    stored = {"prior": [(0, 1)]} if dup else {}
    with patch.object(cut, "scan_cut_windows", return_value=sc), \
         patch.object(cut, "_extract_cut", side_effect=fake_extract), \
         patch.object(cut, "verify_video_file", return_value=(True, 19.5)), \
         patch.object(cut.dedupe, "extract_1fps_hashes", return_value=[(0, 1), (1, 2)]), \
         patch.object(cut.db, "get_stored_fingerprints_by_item", return_value=stored), \
         patch.object(cut.dedupe, "check_consecutive_match", side_effect=dup or (lambda *a, **k: False)), \
         patch.object(telegram_bot, "send_alert", return_value=True):
        return cut.run_cut_item(item_id, cfg=cfg, db_path=tmp_db, transcriber=transcriber,
                                envelope=lambda *a: None)


@pytest.mark.parametrize("total,ends", [(25.0, [8.0, 19.2]), (45.0, [12.0, 24.5, 38.0])])
def test_short_streamer_clips_use_the_same_sentence_aligned_logic(tmp_db, tmp_path, total, ends):
    _seed(tmp_db, tmp_path)
    calls = []

    def transcriber(_p, a, b):
        calls.append((a, b))
        return talker(total, ends)

    ok, msg = _run(tmp_db, tmp_path, _short_scan(total) if total <= 20 else
                   cut.CutScan(total, [], [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 22.0, 20.0), transcriber)
    assert ok, msg
    assert calls == [(0.0, total)]                        # the whole clip, transcribed once
    item = db.get_item("item_s", db_path=tmp_db)
    length = item["cut_end"] - item["cut_start"]
    assert 18.0 - 0.01 <= length <= 22.0 + 0.01 and item["cut_end"] <= total
    assert any(abs(item["cut_end"] - (e + 0.15)) <= 0.16 for e in ends)
    sel = json.loads(item["transcript_words"])["selection"]
    assert sel["candidates"] >= 1 and sel["clean_ends"] == len(ends) and sel["mode"] == "sentence"


@pytest.mark.parametrize("total", [25.0, 45.0])
def test_short_clips_of_continuous_speech_fail_no_clean_ending(tmp_db, tmp_path, total):
    _seed(tmp_db, tmp_path)
    sc = cut.CutScan(total, [], [], [cut.CutWindow(0.0, min(total, 20.0), 1.0)], 18.0, 22.0, 20.0)
    ok, msg = _run(tmp_db, tmp_path, sc, lambda _p, a, b: talker(total, []))
    assert not ok and msg.startswith("no_clean_ending: 0 clean sentence end(s)")
    item = db.get_item("item_s", db_path=tmp_db)
    assert item["stage"] == "failed" and item["error_message"].startswith("no_clean_ending")


def test_cut_level_dedupe_still_applies_and_skips_overlapping_candidates(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 120.0
    words = talker(total, [20.0, 40.5, 41.5, 90.0])     # 40.5 and 41.5 give heavily overlapping windows
    loud = [(t / 10, -12.0 if 200 <= t <= 420 else -30.0) for t in range(0, 1200)]
    sc = cut.CutScan(total, loud, [], [cut.CutWindow(20.0, 40.0, 1.0)], 18.0, 22.0, 20.0)
    verdicts = iter([True, False])                       # the first cut duplicates a prior item
    ok, msg = _run(tmp_db, tmp_path, sc, lambda _p, a, b: words, dup=lambda *a, **k: next(verdicts))
    assert ok, msg
    item = db.get_item("item_s", db_path=tmp_db)
    # the best window (ending after 40.5 or 41.5 s) duplicated a prior cut; its overlapping
    # neighbour was skipped without being cut, and the next distinct window was taken
    assert not (40.0 < item["cut_end"] < 42.0)
    with db.db(tmp_db) as conn:
        msg = conn.execute("SELECT message FROM stage_log WHERE item_id = 'item_s' AND status = 'success'").fetchone()[0]
    assert msg.startswith("Cut candidate #") and not msg.startswith("Cut candidate #1 ")


def test_every_candidate_a_duplicate_is_rejected_as_before(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 60.0
    ok, msg = _run(tmp_db, tmp_path, cut.CutScan(total, [], [], [cut.CutWindow(0, 20, 1)], 18.0, 22.0, 20.0),
                   lambda _p, a, b: talker(total, [20.0, 45.0]), dup=lambda *a, **k: True)
    assert not ok and msg.startswith("Rejected: duplicate_cut")
    assert db.get_item("item_s", db_path=tmp_db)["stage"] == "rejected"


def test_no_speech_keeps_the_old_loudness_windows(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 60.0
    loud = [(t / 10, -20.0) for t in range(0, 600)]
    loud[300] = (30.0, -60.0)
    sc = cut.CutScan(total, loud, [], [cut.CutWindow(10.0, 30.0, 1.0)], 18.0, 22.0, 20.0)
    ok, msg = _run(tmp_db, tmp_path, sc, lambda _p, a, b: [])
    assert ok, msg
    item = db.get_item("item_s", db_path=tmp_db)
    assert item["cut_start"] == 10.0 and item["cut_end"] == pytest.approx(30.0, abs=0.05)
    assert json.loads(item["transcript_words"])["selection"]["mode"] == "no_speech"


# ----------------------------------------------------------------------------- transcript cache

def test_the_source_is_transcribed_once_and_again_only_when_model_or_glossary_change(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 60.0
    calls = []

    def transcriber(_p, a, b):
        calls.append((a, b))
        return talker(total, [20.0, 45.0])

    sc = cut.CutScan(total, [], [], [cut.CutWindow(0, 20, 1)], 18.0, 22.0, 20.0)
    assert _run(tmp_db, tmp_path, sc, transcriber, cfg=CFG)[0]
    assert _run(tmp_db, tmp_path, sc, transcriber, cfg=CFG)[0]           # a re-cut: cached
    assert len(calls) == 1
    sel = json.loads(db.get_item("item_s", db_path=tmp_db)["transcript_words"])["selection"]
    assert sel["transcript_cached"] is True
    other = copy.deepcopy(CFG)
    other["transcribe"]["glossary"] = other["transcribe"]["glossary"] + ["GTA VI"]
    assert _run(tmp_db, tmp_path, sc, transcriber, cfg=other)[0]         # glossary changed: again
    assert len(calls) == 2
    other_model = copy.deepcopy(CFG)
    other_model["transcribe"]["model"] = "base.en"
    assert _run(tmp_db, tmp_path, sc, transcriber, cfg=other_model)[0]   # model changed: again
    assert len(calls) == 3


def test_long_sources_transcribe_only_the_top_regions():
    wins = [cut.CutWindow(s, s + 20, 1.0) for s in (300.0, 301.0, 900.0, 40.0, 1500.0)]
    regions = sw_mod.transcribe_regions(1800.0, wins, 18.0, 22.0, 2.0, {**SW, "region_count": 3})
    # 301 s is skipped (it overlaps 300 s); 40 s is the third region
    assert regions == [(30.0, 74.0), (290.0, 334.0), (890.0, 934.0)]
    assert sw_mod.transcribe_regions(600.0, wins, 18.0, 22.0, 2.0, SW) == [(0.0, 600.0)]


# ----------------------------------------------------------------------------- fail-rate visibility

def _log(tmp_db, status, message, when):
    clock.set_clock(clock.FakeClock(when))
    db.log_stage("x", "cut", status, message=message, db_path=tmp_db)


def test_status_counts_no_clean_ending_over_24h_and_all_time(tmp_db, capsys, fake_clock):
    fake_clock.set("2026-11-01T00:00:00Z")
    db.log_stage("a", "cut", "failure", message="no_clean_ending: 0 clean", db_path=tmp_db)
    db.log_stage("b", "cut", "success", message="Cut candidate #1", db_path=tmp_db)
    fake_clock.set("2026-11-10T00:00:00Z")
    for i in range(3):
        db.log_stage(f"c{i}", "cut", "failure", message="no_clean_ending: none", db_path=tmp_db)
    db.log_stage("d", "cut", "rejected", message="duplicate_cut of y", db_path=tmp_db)
    db.log_stage("e", "cut", "start", message="ignored", db_path=tmp_db)
    db.log_stage("f", "render", "failure", message="no_clean_ending: render", db_path=tmp_db)   # not a cut
    fake_clock.set("2026-11-10T12:00:00Z")
    day = db.cut_outcome_stats(tmp_db, hours=24)
    assert (day["no_clean_ending"], day["attempts"]) == (3, 4) and day["rate"] == pytest.approx(0.75)
    allt = db.cut_outcome_stats(tmp_db)
    assert (allt["no_clean_ending"], allt["attempts"]) == (4, 6)
    runner.print_status(db_path=tmp_db)
    out = capsys.readouterr().out
    assert "=== Cut: no_clean_ending ===" in out
    assert "last 24 h : 3 of 4 cut attempts (75%)" in out and "all time  : 4 of 6 cut attempts (67%)" in out


def test_failure_alerts_carry_the_rate_line_only_past_the_threshold(tmp_db, fake_clock):
    fake_clock.set("2026-11-10T00:00:00Z")
    cfg = {**CFG, "cut": {**CFG["cut"], "no_clean_ending_alert": {"rate": 0.4, "min_attempts": 5}}}
    sent = []

    class Bot:
        def alert(self, text, key=None, repeat_gap_s=None):
            sent.append(text)
            return True

    for i in range(4):
        db.log_stage(f"s{i}", "cut", "success", message="ok", db_path=tmp_db)
    db.log_stage("f0", "cut", "failure", message="no_clean_ending: x", db_path=tmp_db)
    assert telegram_bot.no_clean_ending_line(cfg, tmp_db) is None            # 1 of 5 = 20 %
    telegram_bot.send_alert("posting failed", cfg=cfg, db_path=tmp_db, bot=Bot())
    assert sent[-1] == "posting failed"
    for i in range(3):
        db.log_stage(f"f{i + 1}", "cut", "failure", message="no_clean_ending: x", db_path=tmp_db)
    line = telegram_bot.no_clean_ending_line(cfg, tmp_db)                    # 4 of 8 = 50 %
    assert line and "4 of 8" in line and "50%" in line and "40%" in line
    telegram_bot.send_alert("posting failed", cfg=cfg, db_path=tmp_db, bot=Bot())
    assert sent[-1] == f"posting failed\n{line}"


def test_a_no_clean_ending_cut_alerts_once_the_rate_is_high(tmp_db, tmp_path, fake_clock):
    fake_clock.set("2026-11-10T00:00:00Z")
    for i in range(5):
        db.log_stage(f"f{i}", "cut", "failure", message="no_clean_ending: x", db_path=tmp_db)
    _seed(tmp_db, tmp_path)
    sc = cut.CutScan(30.0, [], [], [cut.CutWindow(0, 20, 1)], 18.0, 22.0, 20.0)
    with patch.object(cut, "scan_cut_windows", return_value=sc), \
         patch.object(telegram_bot, "send_alert", return_value=True) as alert:
        ok, _ = cut.run_cut_item("item_s", db_path=tmp_db, transcriber=lambda _p, a, b: talker(30.0, []),
                                 envelope=lambda *a: None)
    assert not ok
    alert.assert_called_once()
    assert alert.call_args.kwargs["key"] == "cut_no_clean_ending_rate"


def test_windows_stay_under_max_duration_and_a_verified_end_tolerates_encode_slack():
    total = 60.0
    words = talker(total, [22.5, 45.0])
    cands, _ = sw_mod.candidates([region(words, total)], flat_scorer(total), 18.0, 22.0, ES, SW)
    assert cands and all(c.end - c.start <= 22.0 - SW["length_margin_s"] + 1e-6 for c in cands)
    # item_1032: a 22.00 s window encoded to 22.005 s; render must still accept the verified end
    cut_words = talker(22.005, [])[:-2]
    end, spans = endings.content_end(cut_words, 22.005, 18.0, 22.0, ES, trusted_end=22.005)
    assert end == pytest.approx(22.005) and spans
