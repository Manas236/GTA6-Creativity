"""CP4 analytics: 24h/72h snapshots, config-driven metric names, and the ab_learning rebuild.
Zero network: Meta is the fake transport from test_posting."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import analytics, clock, db, posting
from test_posting import FakeResponse, FakeTransport, secrets  # noqa: F401  (fixture)


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "app": {"dry_run": False},
        "autopilot": {"week_one_days": 7, "bandit_min_exploration_floor": 0.2, "bandit_min_samples_per_variant": 3},
        "posting": {"platforms": {"instagram": True, "facebook": True}, "meta_min_gap_seconds": 0,
                    "graph_base": "https://graph.test", "api_version": "v25.0", "max_retries": 0,
                    "rate_limit_pause_hours": 3.0},
    }


def live_post(tmp_db: Path, item_id: str, posted_at: str, *, variant: str = "A", platforms=("instagram", "facebook"),
              hook_style: str = "question", source: str = "youtube", authenticity: str = "unverified",
              preset: str = "P0", ab_excluded: int = 0) -> dict[str, int]:
    """One posted item with a live row per platform. Returns {platform: post_id}."""
    db.insert_discovered_item({"id": item_id, "source": source, "source_id": item_id,
                               "source_url": f"https://x.test/{item_id}"}, db_path=tmp_db)
    db.transition_stage(item_id, "posted", extra_fields={"chosen_variant": variant, "ab_excluded": ab_excluded},
                        db_path=tmp_db)
    ids: dict[str, int] = {}
    with db.db(tmp_db) as conn:
        for platform in platforms:
            cur = conn.execute(
                """INSERT INTO posts (item_id, platform, variant, hook_style, source, authenticity, style_preset,
                                      slot_datetime, status, remote_id, ab_excluded, posted_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?, ?)""",
                (item_id, platform, variant, hook_style, source, authenticity, preset, posted_at,
                 f"{platform[:2]}-{item_id}", ab_excluded, posted_at, posted_at, posted_at),
            )
            ids[platform] = cur.lastrowid
    return ids


def snapshot(tmp_db: Path, post_id: int, hours: int, views: int) -> None:
    analytics.record_snapshot(post_id, hours, {"views": views, "likes": 0, "comments": 0, "shares": 0}, {}, tmp_db)


def learning(tmp_db: Path) -> dict[tuple[str, str], tuple[int, float]]:
    with db.db(tmp_db) as conn:
        return {(r["dimension"], r["dimension_value"]): (r["sample_count"], round(r["normalized_score"], 4))
                for r in conn.execute("SELECT * FROM ab_learning").fetchall()}


def ig_answer(views: int, likes: int = 0, comments: int = 0, shares: int = 0) -> FakeResponse:
    return FakeResponse({"data": [
        {"name": "views", "period": "lifetime", "values": [{"value": views}]},
        {"name": "likes", "period": "lifetime", "values": [{"value": likes}]},
        {"name": "comments", "period": "lifetime", "values": [{"value": comments}]},
        {"name": "shares", "period": "lifetime", "values": [{"value": shares}]},
        {"name": "reach", "period": "lifetime", "values": [{"value": views // 2}]},
    ]})


def fb_answer(plays: int, reactions: dict | None = None, actions: dict | None = None) -> FakeResponse:
    return FakeResponse({"data": [
        {"name": "fb_reels_total_plays", "values": [{"value": plays}]},
        {"name": "post_video_likes_by_reaction_type", "values": [{"value": reactions or {}}]},
        {"name": "post_video_social_actions", "values": [{"value": actions or {}}]},
    ]})


def run(tmp_db, cfg, transport, alerts=None):
    client = posting.MetaClient(transport=transport, cfg=cfg, db_path=tmp_db, sleep=lambda s: None)
    return analytics.run_analytics_stage(
        cfg=cfg, db_path=tmp_db, client=client,
        alert=(lambda text, key: alerts.append((text, key))) if alerts is not None else (lambda t, k: None))


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def test_24h_and_72h_snapshots_are_taken_once_each_for_both_platforms(tmp_db, cfg, secrets, fake_clock):
    ids = live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z")
    transport = FakeTransport({
        "/in-item-1/insights": ig_answer(1200, likes=80, comments=7, shares=11),
        "/fa-item-1/video_insights": fb_answer(300, {"LIKE": 20, "LOVE": 5}, {"COMMENT": 3, "SHARE": 4}),
    })

    fake_clock.set("2026-11-20T07:29:00Z")                       # 23 h 59 m: too early
    assert run(tmp_db, cfg, transport)["snapshots"] == [] and transport.calls == []

    fake_clock.set("2026-11-20T07:31:00Z")                       # 24 h
    assert sorted(run(tmp_db, cfg, transport)["snapshots"]) == sorted([(ids["instagram"], 24), (ids["facebook"], 24)])
    assert run(tmp_db, cfg, transport)["snapshots"] == []        # never a second 24h snapshot
    assert len(transport.calls) == 2

    fake_clock.set("2026-11-22T08:00:00Z")                       # 72 h
    assert sorted(run(tmp_db, cfg, transport)["snapshots"]) == sorted([(ids["instagram"], 72), (ids["facebook"], 72)])

    with db.db(tmp_db) as conn:
        rows = {(r["post_id"], r["snapshot_hours"]): dict(r) for r in conn.execute("SELECT * FROM metrics")}
    ig = rows[(ids["instagram"], 24)]
    assert (ig["views"], ig["likes"], ig["comments"], ig["shares"]) == (1200, 80, 7, 11)
    fb = rows[(ids["facebook"], 24)]
    assert (fb["views"], fb["likes"], fb["comments"], fb["shares"]) == (300, 25, 3, 4)   # breakdowns summed / keyed
    assert '"reach": 600' in ig["raw_json"]                      # everything Graph returned is kept
    # the requests were GETs for exactly the configured metric names, with the right token each
    method, url, kwargs = transport.sent("/in-item-1/insights")[0]
    assert method == "GET" and kwargs["params"]["access_token"] == "ig-token-secret"
    assert kwargs["params"]["metric"] == ",".join(analytics.DEFAULTS["instagram"]["metrics"])
    assert transport.sent("/fa-item-1/video_insights")[0][2]["params"]["access_token"] == "fb-page-token-secret"
    assert db.get_item("item-1", db_path=tmp_db)["stage"] == "measured"


def test_a_snapshot_that_is_too_late_is_skipped_not_recorded_as_if_it_were_on_time(tmp_db, cfg, secrets, fake_clock):
    ids = live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z", platforms=("instagram",))
    transport = FakeTransport({"/insights": ig_answer(9999)})

    fake_clock.set("2026-11-21T00:00:00Z")                       # 40.5 h: the 24h window (24-36 h) is gone
    assert run(tmp_db, cfg, transport)["snapshots"] == []
    fake_clock.set("2026-11-22T10:00:00Z")                       # 74.5 h: the 72h window is open
    assert run(tmp_db, cfg, transport)["snapshots"] == [(ids["instagram"], 72)]
    fake_clock.set("2026-11-30T00:00:00Z")                       # long after: nothing more
    assert run(tmp_db, cfg, transport)["snapshots"] == []
    assert len(transport.calls) == 1


def test_an_item_whose_window_lapsed_unmeasured_still_leaves_posted(tmp_db, cfg, secrets, fake_clock):
    live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z", platforms=("instagram",))
    fake_clock.set("2026-11-22T19:29:00Z")                       # 83 h 59 m: 72 + 12 not over yet
    assert analytics.mark_measured(fake_clock.now, cfg, tmp_db) == []
    fake_clock.set("2026-11-22T19:31:00Z")
    assert analytics.mark_measured(fake_clock.now, cfg, tmp_db) == ["item-1"]


def test_metric_names_come_from_config(tmp_db, cfg, secrets, fake_clock):
    ids = live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z", platforms=("instagram",))
    cfg["analytics"] = {"instagram": {
        "endpoint": "{remote_id}/insights",
        "metrics": ["reel_views_v2", "hearts"],
        "map": {"views": "reel_views_v2", "likes": "hearts"},
    }}
    transport = FakeTransport({"/insights": FakeResponse({"data": [
        {"name": "reel_views_v2", "total_value": {"value": 555}},          # the total_value shape
        {"name": "hearts", "values": [{"value": 4}, {"value": 44}]},        # the newest value wins
    ]})})
    fake_clock.set("2026-11-20T08:00:00Z")
    run(tmp_db, cfg, transport)
    assert transport.calls[0][2]["params"]["metric"] == "reel_views_v2,hearts"
    with db.db(tmp_db) as conn:
        row = conn.execute("SELECT views, likes FROM metrics WHERE post_id = ?", (ids["instagram"],)).fetchone()
    assert (row["views"], row["likes"]) == (555, 44)


def test_the_shipped_config_lists_the_metric_names_for_both_platforms(test_config):
    conf = analytics.settings(test_config)
    assert conf["snapshot_hours"] == [24, 72]
    assert "views" in conf["instagram"]["metrics"] and conf["instagram"]["endpoint"] == "{remote_id}/insights"
    assert "fb_reels_total_plays" in conf["facebook"]["metrics"]
    assert conf["facebook"]["endpoint"] == "{remote_id}/video_insights"
    assert set(conf["facebook"]["map"]) == {"views", "likes", "comments", "shares"}


def test_a_metric_graph_no_longer_knows_is_dropped_alerted_once_and_the_rest_is_recorded(tmp_db, cfg, secrets, fake_clock):
    ids = live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z", platforms=("instagram",))
    cfg["analytics"] = {"instagram": {"endpoint": "{remote_id}/insights", "metrics": ["views", "plays_old"],
                                      "map": {"views": ["views", "plays_old"]}}}

    def answer(method, url, kwargs):
        asked = kwargs["params"]["metric"].split(",")
        if "plays_old" in asked:
            return FakeResponse({"error": {"message": "(#100) metric[1] must be one of ...", "code": 100}}, 400)
        return FakeResponse({"data": [{"name": "views", "values": [{"value": 77}]}]})

    transport = FakeTransport({"/insights": answer})
    alerts: list = []
    fake_clock.set("2026-11-20T08:00:00Z")
    out = run(tmp_db, cfg, transport, alerts)

    assert out["snapshots"] == [(ids["instagram"], 24)] and out["failed"] == []
    assert len(alerts) == 1 and "plays_old" in alerts[0][0] and "analytics.instagram.metrics" in alerts[0][0]
    with db.db(tmp_db) as conn:
        assert conn.execute("SELECT views FROM metrics").fetchone()["views"] == 77
    assert posting.meta_paused(tmp_db)[0] is False               # a bad metric name is not a rate limit


def test_a_failed_fetch_is_retried_next_run_and_a_429_stops_the_pass(tmp_db, cfg, secrets, fake_clock):
    live_post(tmp_db, "item-1", "2026-11-19T07:30:00Z", platforms=("instagram",))
    live_post(tmp_db, "item-2", "2026-11-19T07:40:00Z", platforms=("instagram",))
    routes: dict[str, Any] = {"/insights": FakeResponse({"error": {"message": "boom", "code": 1}}, 500)}
    transport = FakeTransport(routes)
    fake_clock.set("2026-11-20T08:00:00Z")
    out = run(tmp_db, cfg, transport)
    assert out["snapshots"] == [] and len(out["failed"]) == 2    # both tried: one failure does not stop the next

    routes["/insights"] = FakeResponse({"error": {"message": "limit", "code": 4}}, 429)
    before = len(transport.calls)
    run(tmp_db, cfg, transport)
    assert len(transport.calls) == before + 1                    # stopped at the first 429
    assert posting.meta_paused(tmp_db)[0] is True

    routes["/insights"] = ig_answer(10)
    assert run(tmp_db, cfg, transport)["snapshots"] == []        # paused: nothing is sent
    with db.db(tmp_db) as conn:
        conn.execute("DELETE FROM rate_limits")
    assert len(run(tmp_db, cfg, transport)["snapshots"]) == 2    # still inside the window: caught up


def test_no_live_posts_means_no_requests(tmp_db, cfg, secrets, fake_clock):
    transport = FakeTransport({})
    out = run(tmp_db, cfg, transport)
    assert out["snapshots"] == [] and transport.calls == [] and out["learning_rows"] == 0


# ---------------------------------------------------------------------------
# ab_learning
# ---------------------------------------------------------------------------

def test_percentile_ranks_are_mid_ranks():
    assert analytics.percentile_ranks({"a": 10, "b": 20, "c": 30, "d": 40}) == {
        "a": 0.125, "b": 0.375, "c": 0.625, "d": 0.875}
    assert analytics.percentile_ranks({"only": 5}) == {"only": 0.5}
    assert analytics.percentile_ranks({"a": 7, "b": 7}) == {"a": 0.5, "b": 0.5}


def test_posts_are_ranked_within_each_platform_before_combining(tmp_db, cfg, fake_clock):
    # Instagram numbers are 100x Facebook's. Item B wins on Facebook, item A wins on Instagram.
    a = live_post(tmp_db, "item-a", "2026-11-19T07:30:00Z", variant="A")
    b = live_post(tmp_db, "item-b", "2026-11-19T12:30:00Z", variant="B")
    snapshot(tmp_db, a["instagram"], 24, 100_000)
    snapshot(tmp_db, b["instagram"], 24, 90_000)
    snapshot(tmp_db, a["facebook"], 24, 100)
    snapshot(tmp_db, b["facebook"], 24, 900)

    analytics.rebuild_ab_learning(cfg, tmp_db)

    got = learning(tmp_db)
    # Each wins one platform, so they tie at 0.5. Adding raw views instead would have made A
    # the clear winner (100,100 vs 90,900) purely because Instagram's numbers are bigger.
    assert got[("variant", "A")] == (1, 0.5) and got[("variant", "B")] == (1, 0.5)


def test_a_72h_snapshot_replaces_the_24h_one_and_never_competes_with_24h_numbers(tmp_db, cfg, fake_clock):
    old = live_post(tmp_db, "item-old", "2026-11-19T07:30:00Z", variant="A", platforms=("instagram",))
    new = live_post(tmp_db, "item-new", "2026-11-21T07:30:00Z", variant="B", platforms=("instagram",))
    new2 = live_post(tmp_db, "item-new2", "2026-11-21T12:30:00Z", variant="B", platforms=("instagram",))
    snapshot(tmp_db, old["instagram"], 24, 100)        # the weakest of the three at 24 h
    snapshot(tmp_db, new["instagram"], 24, 500)
    snapshot(tmp_db, new2["instagram"], 24, 900)
    snapshot(tmp_db, old["instagram"], 72, 5000)       # its 72h number is bigger than anyone's 24h number
    analytics.rebuild_ab_learning(cfg, tmp_db)
    got = learning(tmp_db)
    # item-old is alone in the 72h cohort -> 0.5; it is NOT ranked top for having "5000 views".
    assert got[("variant", "A")] == (1, 0.5)
    # the two B items keep their 24h ranks within the full 24h cohort of three: 0.5 and 0.8333
    assert got[("variant", "B")] == (2, round((0.5 + 5 / 6) / 2, 4))


def test_all_five_dimensions_are_built_and_ab_excluded_is_left_out_of_variant_only(tmp_db, cfg, fake_clock):
    specs = [
        ("i1", "A", "question", "youtube", "official", "P0", 0, 100),
        ("i2", "B", "curiosity", "youtube", "leak", "P1", 0, 200),
        ("i3", "B", "curiosity", "twitch", "unverified", "P1", 0, 300),
        ("i4", "A", "hype_analysis", "twitch", "fan_made", "P2", 1, 400),     # "Approve A only"
    ]
    for item_id, variant, hook, source, auth, preset, excluded, views in specs:
        ids = live_post(tmp_db, item_id, "2026-11-19T07:30:00Z", variant=variant, hook_style=hook, source=source,
                        authenticity=auth, preset=preset, ab_excluded=excluded, platforms=("instagram",))
        snapshot(tmp_db, ids["instagram"], 24, views)

    out = analytics.rebuild_ab_learning(cfg, tmp_db)
    got = learning(tmp_db)

    assert {dim for dim, _ in got} == {"variant", "hook_style", "source", "authenticity", "style_preset"}
    # ranks: i1 .125, i2 .375, i3 .625, i4 .875
    assert got[("variant", "A")] == (1, 0.125)                    # i4 (ab_excluded) is NOT in variant...
    assert got[("variant", "B")] == (2, 0.5)
    assert got[("hook_style", "hype_analysis")] == (1, 0.875)     # ...but it IS in every other dimension
    assert got[("source", "twitch")] == (2, 0.75)
    assert got[("source", "youtube")] == (2, 0.25)
    assert got[("authenticity", "official")] == (1, 0.125)        # official vs unverified only
    assert got[("authenticity", "unverified")] == (3, 0.625)
    assert got[("style_preset", "P1")] == (2, 0.5) and got[("style_preset", "P2")] == (1, 0.875)
    assert sum(n for (dim, _), (n, _s) in got.items() if dim == "variant") == 3
    assert out["variant"]["B"]["n"] == 2


def test_ab_learning_is_rebuilt_from_metrics_every_run_never_incremented(tmp_db, cfg, fake_clock):
    a = live_post(tmp_db, "item-a", "2026-11-19T07:30:00Z", variant="A", platforms=("instagram",))
    b = live_post(tmp_db, "item-b", "2026-11-19T12:30:00Z", variant="B", platforms=("instagram",))
    snapshot(tmp_db, a["instagram"], 24, 100)
    snapshot(tmp_db, b["instagram"], 24, 200)
    with db.db(tmp_db) as conn:                                  # junk a rebuild must wipe
        conn.execute("INSERT INTO ab_learning VALUES ('variant', 'Z', 99, 0.9, 1.0, 'x')")

    for _ in range(3):                                           # three runs: counts must not grow
        analytics.rebuild_ab_learning(cfg, tmp_db)
    got = learning(tmp_db)
    assert got[("variant", "A")] == (1, 0.25) and got[("variant", "B")] == (1, 0.75)
    assert ("variant", "Z") not in got

    with db.db(tmp_db) as conn:                                  # a metric disappears -> so does its sample
        conn.execute("DELETE FROM metrics WHERE post_id = ?", (b["instagram"],))
    analytics.rebuild_ab_learning(cfg, tmp_db)
    got = learning(tmp_db)
    assert ("variant", "B") not in got and got[("variant", "A")] == (1, 0.5)


def test_variant_weights_are_the_allocation_the_scheduler_will_use(tmp_db, cfg, fake_clock):
    for i in range(3):
        a = live_post(tmp_db, f"a{i}", "2026-11-01T07:30:00Z", variant="A", platforms=("instagram",))
        b = live_post(tmp_db, f"b{i}", "2026-11-01T12:30:00Z", variant="B", platforms=("instagram",))
        snapshot(tmp_db, a["instagram"], 24, 100 + i)
        snapshot(tmp_db, b["instagram"], 24, 900 + i)
    out = analytics.rebuild_ab_learning(cfg, tmp_db)             # 9 days after the first live post
    assert (out["variant"]["B"]["weight"], out["variant"]["A"]["weight"]) == (0.8, 0.2)

    db.kv_set(db.K_FIRST_LIVE, "2026-11-08T00:00:00Z", db_path=tmp_db)   # pretend it is still week 1
    out = analytics.rebuild_ab_learning(cfg, tmp_db)
    assert (out["variant"]["B"]["weight"], out["variant"]["A"]["weight"]) == (0.5, 0.5)
