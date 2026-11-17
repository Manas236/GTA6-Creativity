import json
from datetime import timedelta

import pytest

from dashboard import commands, queue_controls
from pipeline import clock, db, posting, runner
from test_dashboard import BASE, confirmed, result
from test_dashboard_controls import clip, local


def schedule(path, cfg, item, when):
    posting.ensure_post_rows(item, clock.iso(when), cfg, path)
    db.transition_stage(item["id"], "queued", db_path=path)


def queue_clips(path, cfg, clip):
    start = clock.utcnow() + timedelta(days=1)
    schedule(path, cfg, clip, start)
    ids = [clip["id"]]
    for index in range(1, 4):
        item_id = f"queue-ux-{index}"
        db.insert_discovered_item({"id": item_id, "source": "twitch", "source_id": item_id, "source_url": "https://example.com/queue"}, path)
        schedule(path, cfg, {**clip, "id": item_id}, start + timedelta(days=index))
        ids.append(item_id)
    return ids


def test_move_fourth_to_first_and_back(local, clip, tmp_db):
    _, _, cfg, _ = local
    ids = queue_clips(tmp_db, cfg, clip)
    before = queue_controls.scheduled(tmp_db)
    _, command_id = confirmed(local, "position", ids[3], {"target": ids[0]})
    assert result(tmp_db, command_id)["status"] == "applied"
    after = queue_controls.scheduled(tmp_db)
    assert [row["id"] for row in after] == [ids[3], *ids[:3]]
    assert [row["slot"] for row in after] == [row["slot"] for row in before]
    _, command_id = confirmed(local, "position", ids[3], {"target": ids[2]})
    assert result(tmp_db, command_id)["status"] == "applied"
    assert queue_controls.scheduled(tmp_db) == before


@pytest.mark.parametrize("blocker", ["started", "gap", "cap", "stale"])
def test_reorder_refuses_whole_change_at_runner_boundary(local, clip, tmp_db, blocker):
    _, _, cfg, _ = local
    ids = queue_clips(tmp_db, cfg, clip)
    assert runner.acquire_lock(tmp_db, 90, 10, "test-runner")
    try:
        _, command_id = confirmed(local, "position", ids[3], {"target": ids[0]})
        first, second, third, _ = queue_controls.scheduled(tmp_db)
        if blocker == "started":
            row = posting.get_posts_for_item(ids[1], tmp_db)[0]
            posting.set_status(row["id"], "publishing", tmp_db, publish_started_at=db.now())
        if blocker == "gap":
            cfg["posting"]["min_gap_hours"] = 25
        if blocker == "cap":
            cfg["posting"]["daily_post_target"] = 0
        if blocker == "stale":
            queue_controls.set_slot(second["id"], clock.iso(clock.parse(third["slot"]) - timedelta(hours=5)), tmp_db)
        before = queue_controls.scheduled(tmp_db)
        commands.runner_boundary(cfg, tmp_db, "discover")
        assert result(tmp_db, command_id)["status"] == "refused"
        assert queue_controls.scheduled(tmp_db) == before
    finally:
        runner.release_lock(tmp_db)


@pytest.mark.parametrize("slot_taken", [False, True])
def test_restore_unapproval_preserves_a_only_and_refuses_taken_slot(local, clip, tmp_db, slot_taken):
    _, _, cfg, _ = local
    item_id = clip["id"]
    confirmed(local, "aonly", item_id)
    slot = clock.utcnow() + timedelta(hours=8)
    schedule(tmp_db, cfg, db.get_item(item_id, tmp_db), slot)
    _, original = confirmed(local, "unapprove", item_id)
    if slot_taken:
        db.insert_discovered_item({"id": "occupied", "source": "twitch", "source_id": "occupied", "source_url": "https://example.com/occupied"}, tmp_db)
        schedule(tmp_db, cfg, {**clip, "id": "occupied"}, slot)
    _, command_id = confirmed(local, "restore_approval", item_id, {"command_id": original})
    item = db.get_item(item_id, tmp_db)
    posts = posting.get_posts_for_item(item_id, tmp_db)
    if slot_taken:
        assert result(tmp_db, command_id)["status"] == "refused"
        assert "no longer available" in result(tmp_db, command_id)["result"]
        assert item["stage"] == "awaiting_approval" and not posts
    else:
        assert result(tmp_db, command_id)["status"] == "applied"
        assert item["stage"] == "queued" and item["chosen_variant"] == "A"
        assert {row["slot_datetime"] for row in posts} == {clock.iso(slot)}


@pytest.mark.parametrize("queued", [False, True])
def test_restore_unapproval_preserves_b_only(local, clip, tmp_db, queued):
    _, _, cfg, _ = local
    item_id = clip["id"]
    confirmed(local, "bonly", item_id)
    slot = clock.utcnow() + timedelta(hours=8)
    if queued:
        schedule(tmp_db, cfg, db.get_item(item_id, tmp_db), slot)
        assert {row["variant"] for row in posting.get_posts_for_item(item_id, tmp_db)} == {"B"}
    _, original = confirmed(local, "unapprove", item_id)
    returned = db.get_item(item_id, tmp_db)
    assert (returned["stage"], returned["approval_type"], returned["chosen_variant"]) == ("awaiting_approval", None, None)
    _, command_id = confirmed(local, "restore_approval", item_id, {"command_id": original})
    assert result(tmp_db, command_id)["status"] == "applied"
    item = db.get_item(item_id, tmp_db)
    assert item["stage"] == ("queued" if queued else "approved")
    assert (item["approval_type"], item["chosen_variant"], item["ab_excluded"]) == ("approved_b_only", "B", 1)
    posts = posting.get_posts_for_item(item_id, tmp_db)
    if queued:
        assert {(row["variant"], row["ab_excluded"], row["slot_datetime"]) for row in posts} == {("B", 1, clock.iso(slot))}
    else:
        assert not posts


def test_restore_reads_the_a_only_flag_of_an_unapproval_recorded_before_b_only(local, clip, tmp_db):
    item_id = clip["id"]
    confirmed(local, "aonly", item_id)
    _, original = confirmed(local, "unapprove", item_id)
    with db.db(tmp_db) as conn:   # the payload shape older dashboards wrote
        conn.execute("UPDATE dashboard_commands SET payload=? WHERE id=?",
                     (json.dumps({"previous_slot": None, "stage": "approved", "a_only": True}), original))
    _, command_id = confirmed(local, "restore_approval", item_id, {"command_id": original})
    assert result(tmp_db, command_id)["status"] == "applied"
    item = db.get_item(item_id, tmp_db)
    assert (item["stage"], item["approval_type"], item["chosen_variant"]) == ("approved", "approved_a_only", "A")


def test_library_filtered_pages_and_manual_platform(local, tmp_db):
    _, client, _, _ = local
    for index in range(27):
        item_id = f"manual-{index:02d}"
        db.insert_discovered_item(
            {"id": item_id, "source": "manual", "source_id": item_id, "source_url": "https://example.com/manual", "original_title": "Ocean Drive"}, tmp_db
        )
        db.transition_stage(item_id, "rejected", {"source_platform": "twitch", "authenticity": "unverified",
                                                "cut_signal": "chat", "created_at": "2026-11-10T12:00:00Z"}, tmp_db)
    filters = "?stage=rejected&platform=twitch&authenticity=unverified&signal=chat&posted=no&from=2026-11-09&to=2026-11-15&q=ocean"
    one = client.get("/api/library" + filters, base_url=BASE).json
    two = client.get("/api/library" + filters + "&page=2", base_url=BASE).json
    assert one["total"] == 27 and one["pages"] == 2 and len(one["ids"]) == 25
    assert two["ids"] == ["manual-25", "manual-26"]
    assert client.get("/api/library" + filters + "&size=50", base_url=BASE).json["pages"] == 1


def test_another_make_worker_is_started_for_jobs_queued_during_a_one_shot_make(local, tmp_db, monkeypatch):
    from dashboard import workers

    started = []
    monkeypatch.setattr(workers, "spawn", lambda *args: started.append(args))
    assert runner.acquire_lock(tmp_db, kind="make")
    try:
        _, command_id = confirmed(local, "make", payload={"count": 2})
        assert result(tmp_db, command_id)["status"] == "pending"
        assert len(started) == 1
        assert started[0][2:] == ("make", "--job", command_id)
    finally:
        runner.release_lock(tmp_db)
