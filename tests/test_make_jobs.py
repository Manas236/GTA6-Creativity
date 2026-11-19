"""19 Nov fix pass: the content gate in the cut stage (per window), Make's "N asked = N delivered"
loop with its source budget and discovery widening, Twitch display titles, and archiving old
Review items. No network, no CLIP: timelines and discovery are injected."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline import archive, config, content_gate as cg, creation, cut, db, discover, telegram_bot, twitch
from tests.test_sentence_windows import talker

CFG = config.get_config()


# ----------------------------------------------------------------------------- cut stage + gate

def _seed(tmp_db, tmp_path, item_id="item_g"):
    src = tmp_path / f"{item_id}.mp4"
    src.write_bytes(b"0" * 64)
    db.insert_discovered_item({"id": item_id, "source": "twitch", "source_id": item_id,
                               "source_url": f"https://clips.twitch.tv/{item_id}", "author_name": "Streamer",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage(item_id, "downloaded", extra_fields={"download_path": str(src)}, db_path=tmp_db)


def _timeline(total, game_until=None, game_from=None, static=False):
    """2 samples/s; gameplay where the window says so, tweets elsewhere."""
    samples = []
    for i in range(int(total * 2)):
        t = i / 2
        game = (game_until is None or t < game_until) and (game_from is None or t >= game_from)
        samples.append(cg.Sample(t, None if i == 0 else (0.0 if static else 0.3),
                                 (0.9 if game else 0.05) if i % 2 == 0 else None,
                                 "gameplay" if game else "tweet / social post"))
    return cg.Timeline(0.0, total, samples)


def _run(tmp_db, sc, words, timeline, item_id="item_g"):
    def fake_extract(_src, a, b, out):
        Path(out).write_bytes(b"cut")
    with patch.object(cut, "scan_cut_windows", return_value=sc), \
         patch.object(cut, "_extract_cut", side_effect=fake_extract), \
         patch.object(cut, "verify_video_file", return_value=(True, 19.5)), \
         patch.object(cut.dedupe, "extract_1fps_hashes", return_value=[(0, 1), (1, 2)]), \
         patch.object(cut.db, "get_stored_fingerprints_by_item", return_value={}), \
         patch.object(telegram_bot, "send_alert", return_value=True):
        return cut.run_cut_item(item_id, cfg=CFG, db_path=tmp_db, transcriber=lambda _p, a, b: words,
                                envelope=lambda *a: None, gate=lambda _p: timeline)


@pytest.mark.content_gate
def test_a_window_that_fails_the_gate_moves_on_to_the_next_window_in_the_same_source(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 120.0
    words = talker(total, [20.0, 40.0, 60.0, 80.0, 100.0])
    # Loudest early on, so the best-scored windows are in the first minute - which is all tweets.
    loud = [(t / 10, -12.0 if t <= 600 else -30.0) for t in range(0, 1200)]
    sc = cut.CutScan(total, loud, [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 30.0, 20.0)
    ok, msg = _run(tmp_db, sc, words, _timeline(total, game_from=62.0))
    assert ok, msg
    item = db.get_item("item_g", db_path=tmp_db)
    # Not one of the louder all-tweet windows: at least 60 % of the chosen window is the gameplay part.
    length = item["cut_end"] - item["cut_start"]
    assert item["cut_end"] - max(62.0, item["cut_start"]) >= 0.6 * length - 1.0
    sel = json.loads(item["transcript_words"])["selection"]
    assert sel["gate"]["game_share"] >= 0.6 and sel["gate_rejected"]
    assert all("not_gameplay" in g["reason"] for g in sel["gate_rejected"])
    assert "gate: motion" in msg


@pytest.mark.content_gate
def test_every_window_failing_the_gate_rejects_the_source_as_not_gameplay(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    total = 60.0
    words = talker(total, [20.0, 40.0, 59.0])
    sc = cut.CutScan(total, [], [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 30.0, 20.0)
    ok, msg = _run(tmp_db, sc, words, _timeline(total, game_until=12.0))   # 20 % gameplay: source floor passes
    assert not ok and "not_gameplay" in msg
    item = db.get_item("item_g", db_path=tmp_db)
    assert item["stage"] == "rejected" and item["rejection_reason"].startswith("rejected: not_gameplay: all")


@pytest.mark.content_gate
def test_a_source_with_no_moving_gameplay_is_rejected_before_transcription(tmp_db, tmp_path):
    _seed(tmp_db, tmp_path)
    sc = cut.CutScan(60.0, [], [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 30.0, 20.0)
    calls = []
    with patch.object(cut, "scan_cut_windows", return_value=sc):
        ok, msg = cut.run_cut_item("item_g", cfg=CFG, db_path=tmp_db,
                                   transcriber=lambda *a: calls.append(a) or [],
                                   gate=lambda _p: _timeline(60.0, static=True))
    assert not ok and "not_gameplay_source" in msg and "static" in msg
    assert calls == []
    assert db.get_item("item_g", db_path=tmp_db)["stage"] == "rejected"


@pytest.mark.content_gate
def test_a_streamer_with_no_hard_cuts_is_not_rejected_as_static(tmp_db, tmp_path):
    """The 19 Nov ohnePixel case: one continuous camera, 0 scene cuts, all moving gameplay."""
    _seed(tmp_db, tmp_path)
    total = 60.0
    sc = cut.CutScan(total, [], [], [cut.CutWindow(0.0, 20.0, 1.0)], 18.0, 30.0, 20.0)   # no scene cuts
    ok, msg = _run(tmp_db, sc, talker(total, [21.0, 42.0]), _timeline(total))
    assert ok, msg


# ----------------------------------------------------------------------------- Make: N asked = N delivered

def _job(tmp_db, count):
    from dashboard.store import initialize
    initialize(tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("INSERT INTO dashboard_commands (id, action, item_id, payload, expected, status, created_at) "
                     "VALUES ('job1', 'make', NULL, ?, '', 'running', ?)", (json.dumps({"count": count}), db.now()))
    return "job1"


def _cands(prefix, n):
    return [{"id": f"item_{prefix}{i}", "source": "youtube", "source_id": f"{prefix}{i}",
             "source_url": f"https://youtu.be/{prefix}{i}", "original_title": f"{prefix} video {i}",
             "author_name": "Creator", "original_duration": 60.0, "raw_velocity": 1.0} for i in range(n)]


def _producer(tmp_db, good):
    """Render the items whose id is in `good`; reject the rest with a gate reason."""
    def produce(item_id):
        if item_id in good:
            db.transition_stage(item_id, "awaiting_approval", db_path=tmp_db)
        else:
            db.mark_rejected(item_id, "rejected: not_gameplay: all 3 candidate window(s) failed", db_path=tmp_db)
    return produce


def test_make_keeps_going_until_n_clips_are_delivered(tmp_db):
    job = _job(tmp_db, 2)
    pool = _cands("a", 6)
    good = {"item_a2", "item_a4"}
    rounds = []
    done, msg = creation.make_clips(job, 2, CFG, tmp_db, produce=_producer(tmp_db, good),
                                    discover_round=lambda level: rounds.append(level) or pool)
    assert done == 2 and msg.startswith("2 of 2 done in 5 source attempt(s)")
    assert rounds == [0]
    with db.db(tmp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items WHERE creation_job=?", (job,)).fetchone()[0] == 5


def test_make_widens_discovery_when_candidates_run_out(tmp_db):
    job = _job(tmp_db, 1)
    pools = {0: _cands("a", 2), 1: _cands("b", 2)}
    seen = []

    def discover_round(level):
        seen.append(level)
        return pools.get(level, [])
    done, msg = creation.make_clips(job, 1, CFG, tmp_db, produce=_producer(tmp_db, {"item_b1"}),
                                    discover_round=discover_round)
    assert done == 1 and seen == [0, 1]


def test_make_stops_at_the_budget_and_says_why(tmp_db):
    job = _job(tmp_db, 1)
    cfg = {**CFG, "make": {"sources_per_clip": 3}}
    done, msg = creation.make_clips(job, 1, cfg, tmp_db, produce=_producer(tmp_db, set()),
                                    discover_round=lambda level: _cands(f"l{level}x", 10))
    assert done == 0 and "after 3 source attempt(s)" in msg and "budget of 3 candidate sources" in msg
    items = creation._job_items(job, tmp_db)
    assert len(items) == 3 and all("not_gameplay" in creation.attempt_outcome(i) for i in items)


def test_make_reports_dry_discovery(tmp_db):
    job = _job(tmp_db, 2)
    done, msg = creation.make_clips(job, 2, CFG, tmp_db, produce=_producer(tmp_db, {"item_a0"}),
                                    discover_round=lambda level: _cands("a", 1) if level == 0 else [])
    assert done == 1 and "discovery ran dry" in msg


def test_make_progress_is_written_while_it_runs(tmp_db):
    job = _job(tmp_db, 2)
    seen = []

    def produce(item_id):
        with db.db(tmp_db) as conn:
            seen.append(conn.execute("SELECT result FROM dashboard_commands WHERE id=?", (job,)).fetchone()[0])
        db.transition_stage(item_id, "awaiting_approval", db_path=tmp_db)
    creation.make_clips(job, 2, CFG, tmp_db, produce=produce, discover_round=lambda level: _cands("a", 5))
    assert seen[0].startswith("0 of 2 done · 1 of 10 source attempts · attempt 1:")
    assert seen[1].startswith("1 of 2 done · 2 of 10 source attempts · attempt 2:")


def test_needs_review_is_not_delivered_and_its_reason_is_shown(tmp_db):
    job = _job(tmp_db, 1)

    def produce(item_id):
        db.transition_stage(item_id, "needs_review", {"hook_review": json.dumps({"reason": "FAIL: GTA 5"})},
                            db_path=tmp_db)
    done, msg = creation.make_clips(job, 1, {**CFG, "make": {"sources_per_clip": 2}}, tmp_db, produce=produce,
                                    discover_round=lambda level: _cands("a", 5))
    assert done == 0
    items = creation._job_items(job, tmp_db)
    assert creation.attempt_outcome(items[0]).startswith("hook failed its check twice") and "GTA 5" in \
        creation.attempt_outcome(items[0])


# ----------------------------------------------------------------------------- discovery

def test_widened_config_adds_queries_and_a_longer_twitch_window():
    cfg = {"sources": {"youtube": {"queries": ["a"], "max_results_per_query": 10},
                       "twitch": {"lookback_hours": 48, "limit": 20}},
           "discovery": {"widen": [{"youtube_queries": ["b"], "twitch_lookback_hours": 168, "twitch_limit": 100},
                                   {"youtube_queries": ["c"], "youtube_max_results_per_query": 20,
                                    "twitch_lookback_hours": 720}]}}
    assert discover.widened_config(cfg, 0) is cfg
    w1, w2 = discover.widened_config(cfg, 1), discover.widened_config(cfg, 2)
    assert w1["sources"]["youtube"]["queries"] == ["a", "b"] and w1["sources"]["twitch"]["lookback_hours"] == 168
    assert w2["sources"]["youtube"]["queries"] == ["a", "b", "c"] and w2["sources"]["twitch"]["lookback_hours"] == 720
    assert w2["sources"]["youtube"]["max_results_per_query"] == 20 and cfg["sources"]["youtube"]["queries"] == ["a"]


def test_sources_shorter_than_a_clip_are_skipped_at_discovery():
    short = {"source": "twitch", "original_duration": 9.0, "source_id": "s"}
    long_ = {"source": "twitch", "original_duration": 45.0, "source_id": "l"}
    with patch.object(discover, "discover_youtube", return_value=[]), \
         patch.object(twitch, "discover_twitch", return_value=[short, long_]):
        assert discover.gather_candidates({"cut": {"min_duration": 18.0}}) == [long_]


@pytest.mark.parametrize("title,expected", [
    ("Reset", 'jasontheween GTA VI clip: "Reset"'),
    ("W", 'jasontheween GTA VI clip: "W"'),
    ("", "jasontheween GTA VI Twitch clip"),
    ("ohnePixel tries out the strip club", "ohnePixel tries out the strip club"),
])
def test_twitch_one_word_titles_get_the_streamer_and_game(title, expected):
    assert twitch.clip_title({"title": title, "broadcaster_name": "jasontheween"}) == expected


# ----------------------------------------------------------------------------- archive

def test_archive_moves_files_and_takes_items_out_of_review(tmp_db, tmp_path, isolated_media):
    db.init_db(tmp_db)
    renders = isolated_media / "renders"
    renders.mkdir(parents=True)
    old_a = renders / "item_old_A.mp4"
    old_a.write_bytes(b"a")
    (isolated_media / "subtitles").mkdir()
    (isolated_media / "subtitles" / "item_old_A.ass").write_text("x")
    for iid, created, stage in (("item_old", "2026-11-09T09:00:00Z", "rendered"),
                                ("item_new", "2026-11-19T06:00:00Z", "rendered"),
                                ("item_live", "2026-11-09T09:00:00Z", "rendered")):
        db.insert_discovered_item({"id": iid, "source": "youtube", "source_id": iid, "source_url": "u",
                                   "original_title": iid}, db_path=tmp_db)
        with db.db(tmp_db) as conn:
            conn.execute("UPDATE items SET created_at=?, stage=? WHERE id=?", (created, stage, iid))
    db.transition_stage("item_old", "rendered", {"render_a_path": str(old_a)}, tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("INSERT INTO posts (item_id, platform, variant, hook_style, source, slot_datetime, status, "
                     "created_at, updated_at) VALUES ('item_live', 'instagram', 'A', 'question', 'youtube', "
                     "'2026-11-14T12:47:08Z', 'live', '2026-11-14T12:47:08Z', '2026-11-14T12:47:08Z')")

    done = archive.archive_before("2026-11-19", db_path=tmp_db, media=isolated_media)
    assert [d["id"] for d in done] == ["item_old"]          # today's and the posted one are untouched
    item = db.get_item("item_old", db_path=tmp_db)
    moved = isolated_media / "archive" / "2026-11-19" / "renders" / "item_old_A.mp4"
    assert item["stage"] == "archived" and item["render_a_path"] == str(moved.resolve())
    assert moved.read_bytes() == b"a" and not old_a.exists()
    assert (isolated_media / "archive" / "2026-11-19" / "subtitles" / "item_old_A.ass").exists()
    assert db.get_item("item_new", db_path=tmp_db)["stage"] == "rendered"
    assert db.get_item("item_live", db_path=tmp_db)["stage"] == "rendered"
