"""CP4 scheduler: timezone profiles, UTC slots, missed slots, variant allocation, and the
slot-driven posting stage. Zero network: Meta is the fake transport from test_posting."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from pipeline import clock, db, posting, scheduler
from test_posting import FakeTransport, happy_routes, make_publisher, secrets  # noqa: F401  (fixture)

UTC = timezone.utc


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "app": {"dry_run": False},
        "autopilot": {"week_one_days": 7, "bandit_min_exploration_floor": 0.20,
                      "bandit_min_samples_per_variant": 3},
        "posting": {
            "profile": "local",
            "profiles": {
                "local": {"timezone": "Asia/Kolkata", "slots": ["13:00", "18:00"]},
                "server": {"timezone": "America/New_York", "slots": ["18:00", "21:00"]},
            },
            "min_gap_hours": 3.0,
            "daily_post_target": 2,
            "missed_slot_grace_minutes": 90,
            "platforms": {"instagram": True, "facebook": False},
            "meta_min_gap_seconds": 0,
            "graph_base": "https://graph.test",
            "rupload_base": "https://rupload.test",
            "api_version": "v25.0",
            "dry_run_mode": "plan_only",
            "max_retries": 0,
            "max_post_attempts": 3,
            "container_poll_seconds": 0.0,
            "container_poll_max_minutes": 1.0,
        },
    }


def approved_item(tmp_db: Path, tmp_path: Path, item_id: str, score: float = 0.5, a_only: bool = False) -> dict:
    db.insert_discovered_item(
        {"id": item_id, "source": "youtube", "source_id": item_id, "source_url": f"https://youtu.be/{item_id}",
         "author_name": "GamingBolt", "discovery_score": score, "raw_velocity": 5.0, "percentile_rank": 0.8},
        db_path=tmp_db,
    )
    a, b = tmp_path / f"{item_id}_A.mp4", tmp_path / f"{item_id}_B.mp4"
    a.write_bytes(b"A" * 1024)
    b.write_bytes(b"B" * 2048)
    db.transition_stage(
        item_id, "approved",
        extra_fields={
            "hook_text": f"HOOK {item_id}", "hook_style": "question", "authenticity": "unverified",
            "render_a_path": str(a), "render_b_path": str(b), "style_preset": "P0",
            "approval_type": "approved_a_only" if a_only else "approved",
            "chosen_variant": "A" if a_only else None, "ab_excluded": 1 if a_only else 0,
        },
        db_path=tmp_db,
    )
    return db.get_item(item_id, db_path=tmp_db)


def slot_of(tmp_db: Path, item_id: str) -> str:
    return posting.item_slot(posting.get_posts_for_item(item_id, db_path=tmp_db))


def routes_for_many() -> dict[str, Any]:
    """Happy Instagram routes that hand out a fresh container / media id per post."""
    from test_posting import FakeResponse, IG_USER

    counter = {"n": 0}

    def new_container(method, url, kwargs):
        counter["n"] += 1
        return FakeResponse({"id": f"container-{counter['n']}"})

    def publish(method, url, kwargs):
        return FakeResponse({"id": f"media-{kwargs['data']['creation_id'].split('-')[1]}"})

    return {
        f"/{IG_USER}/media_publish": publish,
        f"/{IG_USER}/media": new_container,
        "/ig-api-upload/": FakeResponse({"success": True}),
        "/container-": FakeResponse({"status_code": "FINISHED"}),
        "/media-": FakeResponse({}),          # no timestamp -> posted_at is the pipeline clock
    }


# ---------------------------------------------------------------------------
# Profiles and slots
# ---------------------------------------------------------------------------

def test_local_profile_slots_are_kolkata_1300_and_1800_as_utc(cfg):
    after = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)          # 11:30 in Kolkata
    slots = [clock.iso(s) for _, s in zip(range(4), scheduler.slot_candidates(after, cfg))]
    assert slots == [
        "2026-11-10T07:30:00Z",   # 13:00 IST
        "2026-11-10T12:30:00Z",   # 18:00 IST
        "2026-11-11T07:30:00Z",
        "2026-11-11T12:30:00Z",
    ]


def test_server_profile_slots_are_new_york_1800_and_2100_and_follow_dst(cfg):
    cfg["posting"]["profile"] = "server"
    winter = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)        # EST, UTC-5
    assert [clock.iso(s) for _, s in zip(range(2), scheduler.slot_candidates(winter, cfg))] == [
        "2026-11-19T23:00:00Z", "2026-11-20T02:00:00Z",
    ]
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)          # EDT, UTC-4
    assert [clock.iso(s) for _, s in zip(range(2), scheduler.slot_candidates(summer, cfg))] == [
        "2026-07-01T22:00:00Z", "2026-07-02T01:00:00Z",
    ]


def test_an_unknown_profile_is_an_error_not_a_silent_default(cfg):
    cfg["posting"]["profile"] = "moon"
    with pytest.raises(ValueError, match="moon"):
        scheduler.active_profile(cfg)


def test_the_shipped_config_has_both_profiles_and_two_posts_a_day(test_config):
    assert test_config["posting"]["profile"] == "local"
    name, tz, slots = scheduler.active_profile(test_config)
    assert (name, str(tz), [s.strftime("%H:%M") for s in slots]) == ("local", "Asia/Kolkata", ["13:00", "18:00"])
    test_config["posting"]["profile"] = "server"
    name, tz, slots = scheduler.active_profile(test_config)
    assert (str(tz), [s.strftime("%H:%M") for s in slots]) == ("America/New_York", ["18:00", "21:00"])
    assert test_config["posting"]["daily_post_target"] == 2
    assert test_config["approval"]["switch_to_auto_after_days"] == 7


# ---------------------------------------------------------------------------
# Queueing approved items
# ---------------------------------------------------------------------------

def test_approved_items_are_queued_into_the_next_free_slots_two_a_day_as_utc_iso(tmp_db, tmp_path, cfg, fake_clock):
    for i, score in enumerate([0.9, 0.7, 0.5, 0.3, 0.1]):
        approved_item(tmp_db, tmp_path, f"item-{i}", score)

    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))

    assert [s["item_id"] for s in out["scheduled"]] == [f"item-{i}" for i in range(5)]   # best score first
    assert [s["slot"] for s in out["scheduled"]] == [
        "2026-11-10T07:30:00Z", "2026-11-10T12:30:00Z",
        "2026-11-11T07:30:00Z", "2026-11-11T12:30:00Z",
        "2026-11-12T07:30:00Z",
    ]
    for i in range(5):
        item = db.get_item(f"item-{i}", db_path=tmp_db)
        # the variant is NOT drawn at queue time: it is drawn when the slot comes due
        assert item["stage"] == "queued" and item["chosen_variant"] is None
        rows = posting.get_posts_for_item(item["id"], db_path=tmp_db)
        assert [r["status"] for r in rows] == ["pending"]
        assert rows[0]["variant"] == posting.UNDECIDED_VARIANT and rows[0]["variant_decided_at"] is None
        assert rows[0]["style_preset"] == "P0"
        assert rows[0]["slot_datetime"].endswith("Z")


def test_scheduling_twice_changes_nothing(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    first = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))["scheduled"][0]
    # a crash between "rows created" and "stage moved" leaves the item approved with rows
    db.transition_stage("item-0", "approved", db_path=tmp_db)
    again = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(99))["scheduled"][0]
    assert (again["slot"], again["variant"]) == (first["slot"], first["variant"])
    assert first["variant"] == posting.UNDECIDED_VARIANT
    assert len(posting.get_posts_for_item("item-0", db_path=tmp_db)) == 1
    assert db.get_item("item-0", db_path=tmp_db)["chosen_variant"] is None


def test_the_min_gap_is_respected_between_slots_and_against_real_live_times(tmp_db, tmp_path, cfg, fake_clock):
    cfg["posting"]["profiles"]["local"]["slots"] = ["13:00", "14:00", "18:00"]   # 14:00 is 1 h after 13:00
    cfg["posting"]["daily_post_target"] = 3
    approved_item(tmp_db, tmp_path, "item-0", 0.9)
    approved_item(tmp_db, tmp_path, "item-1", 0.8)
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    assert [s["slot"] for s in out["scheduled"]] == ["2026-11-10T07:30:00Z", "2026-11-10T12:30:00Z"]

    # a post that really went live at 05:30Z the next day blocks the 07:30Z slot (2 h away)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status = 'live', posted_at = '2026-11-11T05:30:00Z' WHERE item_id = 'item-0'")
    db.transition_stage("item-0", "posted", db_path=tmp_db)
    fake_clock.set("2026-11-11T05:31:00Z")
    nxt = scheduler.next_free_slot(fake_clock.now, cfg, tmp_db, exclude_item_id="item-1")
    assert clock.iso(nxt) == "2026-11-11T08:30:00Z"     # 14:00 IST: the first slot >= 3 h after 05:30Z


def test_the_daily_target_caps_a_local_day(tmp_db, tmp_path, cfg, fake_clock):
    cfg["posting"]["profiles"]["local"]["slots"] = ["09:00", "13:00", "18:00", "22:00"]
    fake_clock.set("2026-11-10T00:00:00Z")
    for i in range(3):
        approved_item(tmp_db, tmp_path, f"item-{i}", 0.9 - i / 10)
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    days = [scheduler.local_day(clock.parse(s["slot"]), cfg).isoformat() for s in out["scheduled"]]
    assert days == ["2026-11-10", "2026-11-10", "2026-11-11"]     # only 2 on the first local day


def test_switching_the_profile_keeps_queued_slots_and_uses_the_new_zone_for_new_ones(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0", 0.9)
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    assert slot_of(tmp_db, "item-0") == "2026-11-10T07:30:00Z"

    cfg["posting"]["profile"] = "server"
    approved_item(tmp_db, tmp_path, "item-1", 0.8)
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))

    assert slot_of(tmp_db, "item-0") == "2026-11-10T07:30:00Z"            # untouched
    assert out["scheduled"][0]["slot"] == "2026-11-10T23:00:00Z"          # 18:00 New York
    assert out["profile"] == "server"


def test_a_synthetic_dry_run_item_is_never_scheduled(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "synthetic-cp3-1")
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    assert out["scheduled"] == []
    item = db.get_item("synthetic-cp3-1", db_path=tmp_db)
    assert item["stage"] == "rejected" and "not_schedulable" in item["rejection_reason"]
    assert posting.get_posts_for_item("synthetic-cp3-1", db_path=tmp_db) == []


def test_a_trigger_refusal_while_scheduling_fails_that_item_and_the_rest_are_queued(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-bad", 0.9)
    approved_item(tmp_db, tmp_path, "item-good", 0.1)
    with db.db(tmp_db) as conn:
        conn.execute(
            "CREATE TRIGGER refuse_bad BEFORE INSERT ON posts WHEN NEW.item_id = 'item-bad' "
            "BEGIN SELECT RAISE(ABORT, 'preview_only item cannot be posted'); END"
        )
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    assert [s["item_id"] for s in out["scheduled"]] == ["item-good"]
    assert out["scheduled"][0]["slot"] == "2026-11-10T07:30:00Z"          # the bad item took no slot
    assert db.get_item("item-bad", db_path=tmp_db)["stage"] == "failed"


# ---------------------------------------------------------------------------
# Variant allocation
# ---------------------------------------------------------------------------

def set_learning(tmp_db: Path, a: tuple[int, float], b: tuple[int, float]) -> None:
    with db.db(tmp_db) as conn:
        conn.execute("DELETE FROM ab_learning")
        for value, (n, score) in (("A", a), ("B", b)):
            conn.execute(
                "INSERT INTO ab_learning (dimension, dimension_value, sample_count, normalized_score, weight, rebuilt_at) "
                "VALUES ('variant', ?, ?, ?, 1.0, ?)", (value, n, score, db.now()))


def first_live(tmp_db: Path, when: str) -> None:
    db.kv_set(db.K_FIRST_LIVE, when, db_path=tmp_db)


def share_of_b(tmp_db, cfg, item, draws: int = 4000) -> float:
    rng = random.Random(7)
    return sum(scheduler.choose_variant(item, cfg, tmp_db, rng)[0] == "B" for _ in range(draws)) / draws


def test_approve_a_only_stays_a_and_stays_ab_excluded(tmp_db, tmp_path, cfg, fake_clock):
    item = approved_item(tmp_db, tmp_path, "item-a", a_only=True)
    set_learning(tmp_db, (10, 0.1), (10, 0.9))                    # even with B far ahead
    first_live(tmp_db, "2026-10-01T00:00:00Z")
    assert all(scheduler.choose_variant(item, cfg, tmp_db, random.Random(s))[0] == "A" for s in range(50))

    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    row = posting.get_posts_for_item("item-a", db_path=tmp_db)[0]
    assert (row["variant"], row["ab_excluded"]) == ("A", 1)
    assert db.get_item("item-a", db_path=tmp_db)["ab_excluded"] == 1


def test_week_one_is_fifty_fifty_and_is_counted_from_the_first_live_post(tmp_db, tmp_path, cfg, fake_clock):
    item = approved_item(tmp_db, tmp_path, "item-0")
    set_learning(tmp_db, (10, 0.1), (10, 0.9))                    # learning must be ignored in week 1

    assert scheduler.in_week_one(cfg, tmp_db) is True             # nothing live yet: still "week 1"
    assert 0.47 < share_of_b(tmp_db, cfg, item) < 0.53

    fake_clock.set("2026-12-25T00:00:00Z")                        # weeks later, but still no live post
    assert scheduler.in_week_one(cfg, tmp_db) is True

    first_live(tmp_db, "2026-12-24T12:00:00Z")                    # the first post goes live
    fake_clock.set("2026-12-31T11:59:00Z")                        # 6 d 23 h 59 m later
    assert scheduler.in_week_one(cfg, tmp_db) is True
    assert "week 1" in scheduler.choose_variant(item, cfg, tmp_db, random.Random(1))[1]
    fake_clock.set("2026-12-31T12:00:00Z")                        # exactly 7 days
    assert scheduler.in_week_one(cfg, tmp_db) is False


def test_after_week_one_the_leader_gets_eighty_and_the_other_keeps_its_twenty_percent_floor(tmp_db, tmp_path, cfg, fake_clock):
    item = approved_item(tmp_db, tmp_path, "item-0")
    first_live(tmp_db, "2026-11-01T00:00:00Z")                    # 9 days before the fake "now"

    set_learning(tmp_db, (6, 0.30), (6, 0.70))
    probs, why = scheduler.variant_probabilities(cfg, tmp_db)
    assert probs == {"B": 0.8, "A": 0.2} and "bandit" in why
    assert 0.77 < share_of_b(tmp_db, cfg, item) < 0.83

    set_learning(tmp_db, (6, 0.99), (6, 0.01))                    # however lopsided, the floor holds
    assert 0.17 < share_of_b(tmp_db, cfg, item) < 0.23


def test_the_bandit_stays_fifty_fifty_until_both_variants_have_enough_samples(tmp_db, tmp_path, cfg, fake_clock):
    first_live(tmp_db, "2026-11-01T00:00:00Z")
    assert scheduler.variant_probabilities(cfg, tmp_db)[0] == {"A": 0.5, "B": 0.5}        # no learning rows
    set_learning(tmp_db, (2, 0.1), (9, 0.9))
    probs, why = scheduler.variant_probabilities(cfg, tmp_db)
    assert probs == {"A": 0.5, "B": 0.5} and "fewer than 3" in why
    set_learning(tmp_db, (5, 0.5), (5, 0.5))
    assert scheduler.variant_probabilities(cfg, tmp_db)[0] == {"A": 0.5, "B": 0.5}        # a tie


def test_first_live_at_is_pinned_once_read_from_posts(tmp_db, tmp_path, cfg, fake_clock):
    assert db.first_live_at(tmp_db) is None
    item = approved_item(tmp_db, tmp_path, "item-0")
    posting.ensure_post_rows(item, slot_datetime="2026-11-10T07:30:00Z", cfg=cfg, db_path=tmp_db)
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status = 'live', posted_at = '2026-11-10T07:31:00Z'")
    assert db.first_live_at(tmp_db) == "2026-11-10T07:31:00Z"
    with db.db(tmp_db) as conn:
        conn.execute("DELETE FROM posts")                        # cleanup can never move the launch date
    assert db.first_live_at(tmp_db) == "2026-11-10T07:31:00Z"


# ---------------------------------------------------------------------------
# The slot-driven posting stage, and missed slots
# ---------------------------------------------------------------------------

def test_nothing_posts_before_its_slot_and_it_posts_at_the_slot(tmp_db, tmp_path, cfg, secrets, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    transport = FakeTransport(routes_for_many())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    fake_clock.set("2026-11-10T07:29:00Z")
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}
    assert transport.calls == []

    fake_clock.set("2026-11-10T07:30:30Z")
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {"item-0": {"instagram": "live"}}
    assert db.get_item("item-0", db_path=tmp_db)["stage"] == "posted"
    assert db.first_live_at(tmp_db) == "2026-11-10T07:30:30Z"


def test_an_approved_item_is_not_posted_until_the_scheduler_has_queued_it(tmp_db, tmp_path, cfg, secrets, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    transport = FakeTransport(routes_for_many())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    fake_clock.set("2026-11-20T00:00:00Z")
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}
    assert transport.calls == []


def test_missed_slots_after_days_asleep_are_re_slotted_one_per_slot_never_burst_posted(tmp_db, tmp_path, cfg, secrets, fake_clock):
    for i in range(4):
        approved_item(tmp_db, tmp_path, f"item-{i}", 0.9 - i / 10)
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))   # 10th and 11th, 2 a day
    transport = FakeTransport(routes_for_many())
    publisher, _ = make_publisher(tmp_db, cfg, transport)

    fake_clock.set("2026-11-13T09:00:00Z")          # the laptop wakes 3 days later: all 4 slots are gone

    # Even if posting runs BEFORE the scheduler, nothing stale goes out.
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}
    assert transport.calls == []

    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    assert [(m[0], m[2]) for m in out["rescheduled"]] == [
        ("item-0", "2026-11-13T12:30:00Z"), ("item-1", "2026-11-14T07:30:00Z"),
        ("item-2", "2026-11-14T12:30:00Z"), ("item-3", "2026-11-15T07:30:00Z"),
    ]                                                # same order, one per slot, two a day
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}

    # Walk forward minute by minute through the next two days: never two posts inside the gap.
    live_times: list[datetime] = []
    t = fake_clock.now
    while t < clock.parse("2026-11-15T09:00:00Z"):
        t += timedelta(minutes=10)
        fake_clock.set(t)
        scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
        for _item_id, result in posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher).items():
            assert result == {"instagram": "live"}
            live_times.append(t)
    assert [clock.iso(x) for x in live_times] == [
        "2026-11-13T12:30:00Z", "2026-11-14T07:30:00Z", "2026-11-14T12:30:00Z", "2026-11-15T07:30:00Z",
    ]
    assert len(transport.sent("media_publish")) == 4          # each item published exactly once


def test_a_post_a_little_late_but_inside_the_grace_still_goes_out(tmp_db, tmp_path, cfg, secrets, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes_for_many()))
    fake_clock.set("2026-11-10T08:45:00Z")           # 75 minutes late, grace is 90
    assert scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)["rescheduled"] == []
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {"item-0": {"instagram": "live"}}


def test_a_post_that_is_already_underway_is_never_re_slotted(tmp_db, tmp_path, cfg, secrets, fake_clock):
    cfg["posting"]["platforms"]["facebook"] = True
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    with db.db(tmp_db) as conn:                      # Instagram went out (as B), Facebook did not, 3 days asleep
        conn.execute("UPDATE posts SET status = 'live', posted_at = '2026-11-10T07:30:10Z', remote_id = 'm1', "
                     "variant = 'B', variant_decided_at = '2026-11-10T07:30:00Z' WHERE platform = 'instagram'")
    fake_clock.set("2026-11-13T09:00:00Z")

    assert scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)["rescheduled"] == []
    assert slot_of(tmp_db, "item-0") == "2026-11-10T07:30:00Z"

    transport = FakeTransport(happy_routes())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    out = posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)
    assert out == {"item-0": {"instagram": "live", "facebook": "live"}}       # Facebook alone catches up
    assert transport.sent("media_publish") == []                              # Instagram is not published twice


def test_only_one_new_post_is_begun_per_pass_and_never_inside_the_min_gap(tmp_db, tmp_path, cfg, secrets, fake_clock):
    a = approved_item(tmp_db, tmp_path, "item-0", 0.9)
    b = approved_item(tmp_db, tmp_path, "item-1", 0.8)
    for item, slot in ((a, "2026-11-10T07:30:00Z"), (b, "2026-11-10T07:40:00Z")):   # forced too close together
        posting.ensure_post_rows(item, slot_datetime=slot, cfg=cfg, db_path=tmp_db)
        db.transition_stage(item["id"], "queued", db_path=tmp_db)
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes_for_many()))

    fake_clock.set("2026-11-10T07:45:00Z")           # both are due
    assert list(posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)) == ["item-0"]
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}   # gap holds item-1 back
    fake_clock.set("2026-11-10T09:30:00Z")           # past item-1's grace: it is re-slotted, not sent late
    moved = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)["rescheduled"]
    assert moved == [("item-1", "2026-11-10T07:40:00Z", "2026-11-10T12:30:00Z")]


def test_going_live_after_a_dry_run_re_slots_the_dry_run_items_instead_of_skipping_or_bursting(tmp_db, tmp_path, cfg, secrets, fake_clock):
    cfg["app"]["dry_run"] = True
    for i in range(3):
        approved_item(tmp_db, tmp_path, f"item-{i}", 0.9 - i / 10)
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    transport = FakeTransport(routes_for_many())
    publisher, _ = make_publisher(tmp_db, cfg, transport)
    for when in ("2026-11-10T07:31:00Z", "2026-11-10T12:31:00Z", "2026-11-11T07:31:00Z"):
        fake_clock.set(when)
        scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
        posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)
    assert transport.calls == []                                             # plan_only: nothing was sent
    assert [posting.get_posts_for_item(f"item-{i}", db_path=tmp_db)[0]["status"] for i in range(3)] == ["dry_run"] * 3
    assert all(db.get_item(f"item-{i}", db_path=tmp_db)["stage"] == "queued" for i in range(3))
    assert scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)["rescheduled"] == []   # dry-run rows stay put

    cfg["app"]["dry_run"] = False                                            # 19 Nov: go live
    fake_clock.set("2026-11-19T06:00:00Z")
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    assert out["reset_after_dry_run"] == 3
    assert [m[2] for m in out["rescheduled"]] == [
        "2026-11-19T07:30:00Z", "2026-11-19T12:30:00Z", "2026-11-20T07:30:00Z",
    ]
    assert posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher) == {}   # not due yet
    fake_clock.set("2026-11-19T07:30:05Z")
    assert list(posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)) == ["item-0"]


# ---------------------------------------------------------------------------
# Rows that failed for good
# ---------------------------------------------------------------------------

def test_an_unknown_remote_state_is_never_retried_on_a_later_pass(tmp_db, tmp_path, cfg, secrets, fake_clock):
    from test_posting import FakeResponse, IG_USER

    item = approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    row = posting.get_posts_for_item("item-0", db_path=tmp_db)[0]
    posting.update_post(row["id"], db_path=tmp_db, status="publishing", remote_container_id="container-1",
                        publish_started_at="2026-11-10T07:30:00Z")
    routes = routes_for_many()
    routes[f"/{IG_USER}/media"] = FakeResponse({"data": []})                 # nothing in recent media
    transport = FakeTransport(routes)
    alerts: list = []
    publisher, _ = make_publisher(tmp_db, cfg, transport, alerts=alerts)

    fake_clock.set("2026-11-10T07:35:00Z")
    for _ in range(4):                                                       # four more cycles
        posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)
        fake_clock.advance(minutes=1)

    row = posting.get_posts_for_item("item-0", db_path=tmp_db)[0]
    assert row["status"] == "failed" and row["no_retry"] == 1
    assert transport.sent("media_publish") == [] and transport.sent("ig-api-upload") == []
    assert len(alerts) == 1                                                  # one alert, not one per cycle
    failed = db.get_item(item["id"], db_path=tmp_db)
    assert failed["stage"] == "failed" and failed["error_stage"] == "posting"   # out of the queue, slot freed


def test_live_on_one_platform_and_given_up_on_the_other_counts_as_posted(tmp_db, tmp_path, cfg, secrets, fake_clock):
    from test_posting import FB_PAGE, FakeResponse

    cfg["posting"]["platforms"]["facebook"] = True
    cfg["posting"]["max_post_attempts"] = 2
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=random.Random(1))
    routes = routes_for_many()
    routes[f"/{FB_PAGE}/video_reels"] = FakeResponse({"error": {"message": "nope", "code": 100}}, 400)
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes))

    fake_clock.set("2026-11-10T07:30:30Z")
    stages = []
    for _ in range(3):
        posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher)
        stages.append(db.get_item("item-0", db_path=tmp_db)["stage"])
        fake_clock.advance(minutes=1)

    assert stages == ["queued", "posted", "posted"]          # 2 Facebook attempts, then it is let go
    rows = {r["platform"]: r["status"] for r in posting.get_posts_for_item("item-0", db_path=tmp_db)}
    assert rows == {"instagram": "live", "facebook": "failed"}
    assert posting.item_has_open_posts("item-0", cfg, tmp_db) is False



# ---------------------------------------------------------------------------
# The variant is drawn when the slot comes due (latest learning), not at queue time
# ---------------------------------------------------------------------------

class FixedRoll:
    """An rng whose every draw is `value`: 0.3 means A at 50/50 but B when B leads 80/20."""

    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


def test_the_variant_is_drawn_and_logged_when_the_slot_comes_due(tmp_db, tmp_path, cfg, secrets, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.3))
    assert out["scheduled"][0]["variant"] == posting.UNDECIDED_VARIANT          # nothing drawn yet

    fake_clock.set(slot_of(tmp_db, "item-0"))
    publisher, _ = make_publisher(tmp_db, cfg, FakeTransport(routes_for_many()))
    posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher, rng=FixedRoll(0.3))
    rows = posting.get_posts_for_item("item-0", db_path=tmp_db)
    assert {r["variant"] for r in rows} == {"A"}                                # week 1: 0.3 < 0.5 -> A
    assert all(r["variant_decided_at"] == clock.iso(clock.utcnow()) for r in rows)
    assert db.get_item("item-0", db_path=tmp_db)["chosen_variant"] == "A"
    with db.db(tmp_db) as conn:
        log = [r["message"] for r in conn.execute(
            "SELECT message FROM stage_log WHERE item_id = 'item-0' AND stage = 'scheduler'")]
    assert any("drawn at the slot" in m and "week 1" in m for m in log)


def test_after_week_one_the_draw_uses_the_bandit_even_for_an_item_queued_in_week_one(tmp_db, tmp_path, cfg, secrets, fake_clock):
    first_live(tmp_db, "2026-11-03T07:30:00Z")                     # week 1 ends 2026-11-10T07:30Z
    fake_clock.set("2026-11-10T06:00:00Z")
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.3))   # queued in week 1
    assert slot_of(tmp_db, "item-0") == "2026-11-10T07:30:00Z"
    set_learning(tmp_db, (6, 0.2), (6, 0.8))

    fake_clock.set("2026-11-10T07:30:00Z")                         # the slot: exactly 7 days -> not week 1
    assert scheduler.in_week_one(cfg, tmp_db) is False
    publisher, transport = make_publisher(tmp_db, cfg, FakeTransport(routes_for_many()))
    posting.run_posting_stage(cfg=cfg, db_path=tmp_db, publisher=publisher, rng=FixedRoll(0.3))
    row = posting.get_posts_for_item("item-0", db_path=tmp_db)[0]
    assert (row["variant"], row["status"]) == ("B", "live")        # 0.3 is B under 80/20; it was A at 50/50


def test_a_drawn_variant_is_frozen_once_a_row_has_begun(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    assert posting.decide_variant("item-0", cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.9)) == "B"
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status = 'uploading', remote_container_id = 'c1'")
    set_learning(tmp_db, (9, 0.9), (9, 0.1))
    first_live(tmp_db, "2026-10-01T00:00:00Z")                     # long after week 1, A now leads
    assert posting.decide_variant("item-0", cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.0)) == "B"
    assert posting.get_posts_for_item("item-0", db_path=tmp_db)[0]["variant"] == "B"


def test_an_undecided_row_can_never_be_uploaded(tmp_db, tmp_path, cfg, fake_clock):
    item = approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    row = posting.get_posts_for_item("item-0", db_path=tmp_db)[0]
    with pytest.raises(posting.PublishError, match="not been decided"):
        posting.video_for_post(item, row)


def test_a_missed_slot_and_a_dry_run_both_leave_the_variant_to_be_drawn_again(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-0")
    scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    posting.decide_variant("item-0", cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.9))   # drawn, but never begun
    fake_clock.set("2026-11-11T00:00:00Z")                         # the slot was missed
    moved = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)["rescheduled"]
    assert moved and moved[0][0] == "item-0"
    assert posting.get_posts_for_item("item-0", db_path=tmp_db)[0]["variant_decided_at"] is None

    posting.decide_variant("item-0", cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.9))
    with db.db(tmp_db) as conn:
        conn.execute("UPDATE posts SET status = 'dry_run'")
    live_cfg = {**cfg, "app": {**cfg.get("app", {}), "dry_run": False}}
    assert posting.reset_dry_run_rows(cfg=live_cfg, db_path=tmp_db) == 1
    row = posting.get_posts_for_item("item-0", db_path=tmp_db)[0]
    assert row["status"] == "pending" and row["variant_decided_at"] is None


def test_a_only_is_fixed_at_queue_time(tmp_db, tmp_path, cfg, fake_clock):
    approved_item(tmp_db, tmp_path, "item-a", a_only=True)
    out = scheduler.run_scheduler_stage(cfg=cfg, db_path=tmp_db)
    assert out["scheduled"][0]["variant"] == "A"
    row = posting.get_posts_for_item("item-a", db_path=tmp_db)[0]
    assert row["variant"] == "A" and row["variant_decided_at"]
    assert posting.decide_variant("item-a", cfg=cfg, db_path=tmp_db, rng=FixedRoll(0.99)) == "A"
