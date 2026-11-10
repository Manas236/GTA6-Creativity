"""Unit tests for the CP3 Telegram approval card. Zero network: the transport is a fake."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline import db, telegram_bot

CHAT_ID = "4242"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    """Records every Bot API call and answers from a scripted queue."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[list[dict[str, Any]]] = []
        self.next_message_id = 100
        self.raise_on: dict[str, Exception] = {}

    def __call__(self, method, params, files=None, http_timeout=30.0):
        self.calls.append((method, params))
        if method in self.raise_on:
            raise self.raise_on[method]
        if method == "getUpdates":
            return self.updates.pop(0) if self.updates else []
        if method in ("sendVideo", "sendMessage"):
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        return True

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def of(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def cfg() -> dict[str, Any]:
    return {
        "app": {"dry_run": True},
        "telegram": {"enabled": True, "poll_timeout_s": 1, "edit_throttle_gap_s": 2.5},
        "approval": {
            "mode": "telegram",
            "auto_quality_gate": {
                "require_both_renders": True,
                "require_ffprobe_pass": True,
                "require_grounded_hook": True,
                "max_duration_seconds": 22.0,
                "max_b_extra_seconds": 8.0,
            },
        },
    }


def make_item(
    tmp_db: Path,
    tmp_path: Path,
    item_id: str = "item-1",
    score: float = 0.5,
    stage: str = "rendered",
    grounded: int = 1,
    renders: bool = True,
) -> dict[str, Any]:
    db.insert_discovered_item(
        {
            "id": item_id,
            "source": "youtube",
            "source_id": item_id,
            "source_url": f"https://youtu.be/{item_id}",
            "author_name": "GamingBolt",
            "original_title": "GTA 6 detail nobody noticed",
            "discovery_score": score,
            "raw_velocity": 10.0,
            "percentile_rank": 0.9,
        },
        db_path=tmp_db,
    )
    a = tmp_path / f"{item_id}_A.mp4"
    b = tmp_path / f"{item_id}_B.mp4"
    if renders:
        a.write_bytes(b"A" * 2048)
        b.write_bytes(b"B" * 2048)
    db.transition_stage(
        item_id,
        stage,
        extra_fields={
            "hook_text": "WHAT IS HIDDEN IN THIS GTA 6 SHOT?",
            "hook_style": "question",
            "hook_grounded": grounded,
            "authenticity": "unverified",
            "render_a_path": str(a) if renders else None,
            "render_b_path": str(b) if renders else None,
            "caption_text": "WHAT IS HIDDEN IN THIS GTA 6 SHOT?\n\nCredit: GamingBolt\n.\n#gta6",
        },
        db_path=tmp_db,
    )
    return db.get_item(item_id, db_path=tmp_db)


def make_bot(tmp_db: Path, cfg: dict[str, Any], transport: FakeTransport, clock: Clock | None = None):
    return telegram_bot.ApprovalBot(
        transport=transport, chat_id=CHAT_ID, cfg=cfg, db_path=tmp_db, clock=clock or Clock()
    )


def tap(item_id: str, action: str, message_id: int) -> dict[str, Any]:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "data": f"a:{action}:{item_id}",
            "from": {"id": int(CHAT_ID)},
            "message": {"message_id": message_id, "chat": {"id": int(CHAT_ID), "type": "private"}},
        },
    }


# ---------------------------------------------------------------------------
# Card lifecycle
# ---------------------------------------------------------------------------

def test_card_send_carries_video_hook_authenticity_source_and_score(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)

    sent = bot.ensure_card()

    assert sent == "item-1"
    # Variant B is the card's video; Variant A follows as a second message.
    videos = transport.of("sendVideo")
    assert len(videos) == 2
    caption = videos[0]["caption"]
    assert "WHAT IS HIDDEN IN THIS GTA 6 SHOT?" in caption   # hook
    assert "UNVERIFIED" in caption                            # authenticity tag
    assert "youtube" in caption and "youtu.be/item-1" in caption   # source
    assert "GamingBolt" in caption                            # creator
    assert "0.500" in caption                                 # discovery score
    assert "🅰 Variant A" in videos[1]["caption"]
    # Three buttons, only on the card itself.
    buttons = [b["callback_data"] for row in videos[0]["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["a:ok:item-1", "a:no:item-1", "a:aonly:item-1"]
    assert "reply_markup" not in videos[1]
    # The item moved to awaiting_approval and the card is recorded.
    assert db.get_item("item-1", db_path=tmp_db)["stage"] == "awaiting_approval"
    state = telegram_bot.card_state(tmp_db)
    assert state["item_id"] == "item-1" and state["mid"] == 101


def test_approve_edits_the_same_card_clears_buttons_and_leaves_variant_to_the_scheduler(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    clock = Clock()
    bot = make_bot(tmp_db, cfg, transport, clock)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]

    bot.handle_update(tap("item-1", "ok", mid))

    item = db.get_item("item-1", db_path=tmp_db)
    assert item["stage"] == "approved"
    assert item["approval_type"] == "approved"
    assert item["chosen_variant"] is None       # the scheduler assigns A/B later, not the reviewer
    assert item["ab_excluded"] == 0
    # The one card was edited in place (not a new message) and its buttons were removed.
    edits = transport.of("editMessageCaption")
    assert len(edits) == 1
    assert edits[0]["message_id"] == mid
    assert edits[0]["reply_markup"] == {"inline_keyboard": []}
    assert "✅ Approved" in edits[0]["caption"]
    assert "✅ Approved" in [p for m, p in transport.calls if m == "answerCallbackQuery"][0]["text"]
    assert telegram_bot.card_state(tmp_db) is None   # the slot is free for the next item


def test_reject_marks_the_item_rejected_with_user_rejected(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]

    bot.handle_update(tap("item-1", "no", mid))

    item = db.get_item("item-1", db_path=tmp_db)
    assert item["stage"] == "rejected"
    assert item["rejection_reason"] == "user_rejected"
    assert "❌ Rejected" in transport.of("editMessageCaption")[0]["caption"]
    assert telegram_bot.card_state(tmp_db) is None


def test_approve_a_only_pins_variant_a_and_excludes_it_from_the_ab_comparison(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]

    bot.handle_update(tap("item-1", "aonly", mid))

    item = db.get_item("item-1", db_path=tmp_db)
    assert item["stage"] == "approved"
    assert item["approval_type"] == "approved_a_only"
    assert item["chosen_variant"] == "A"
    assert item["ab_excluded"] == 1
    with db.db(tmp_db) as conn:
        logged = [r["message"] for r in conn.execute(
            "SELECT message FROM stage_log WHERE item_id = 'item-1' AND stage = 'approval'"
        ).fetchall()]
    assert any("excluded from the A/B comparison" in m for m in logged)


def test_a_click_on_an_old_card_does_nothing_and_says_so(tmp_db, tmp_path, cfg):
    """Stale-button protection: a tap on a superseded message changes no state."""
    make_item(tmp_db, tmp_path, "item-1", score=0.9)
    make_item(tmp_db, tmp_path, "item-2", score=0.1)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    old_mid = telegram_bot.card_state(tmp_db)["mid"]
    bot.handle_update(tap("item-1", "ok", old_mid))         # decide the first one
    bot.ensure_card()                                        # the second card is now the live one
    new_mid = telegram_bot.card_state(tmp_db)["mid"]
    assert new_mid != old_mid

    before = db.get_item("item-2", db_path=tmp_db)
    bot.handle_update(tap("item-2", "ok", old_mid))           # a tap on the OLD message

    assert db.get_item("item-2", db_path=tmp_db) == before     # nothing changed
    answers = [p["text"] for m, p in transport.calls if m == "answerCallbackQuery"]
    assert answers[-1] == telegram_bot.STALE
    assert telegram_bot.card_state(tmp_db)["item_id"] == "item-2"   # the live card is untouched


def test_a_click_for_a_different_item_on_the_live_message_is_stale(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "item-1")
    make_item(tmp_db, tmp_path, "item-2")
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]

    assert bot.on_button("a:ok:item-2", mid) == telegram_bot.STALE
    assert db.get_item("item-2", db_path=tmp_db)["stage"] == "rendered"


def test_a_click_from_another_chat_is_ignored_entirely(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]
    intruder = tap("item-1", "ok", mid)
    intruder["callback_query"]["message"]["chat"]["id"] = 999999

    bot.handle_update(intruder)

    assert db.get_item("item-1", db_path=tmp_db)["stage"] == "awaiting_approval"
    assert "answerCallbackQuery" not in transport.methods()


# ---------------------------------------------------------------------------
# One at a time
# ---------------------------------------------------------------------------

def test_strictly_one_item_at_a_time_in_score_order(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "low", score=0.10)
    make_item(tmp_db, tmp_path, "high", score=0.90)
    make_item(tmp_db, tmp_path, "mid", score=0.50)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)

    assert bot.ensure_card() == "high"          # highest score first
    assert bot.ensure_card() is None            # a card is open: nothing else is sent
    assert bot.ensure_card() is None
    assert len(transport.of("sendVideo")) == 2  # still only the first item's two messages

    bot.handle_update(tap("high", "ok", telegram_bot.card_state(tmp_db)["mid"]))
    assert bot.ensure_card() == "mid"           # only now does the next one go out
    assert bot.ensure_card() is None
    bot.handle_update(tap("mid", "no", telegram_bot.card_state(tmp_db)["mid"]))
    assert bot.ensure_card() == "low"

    assert db.get_item("low", db_path=tmp_db)["stage"] == "awaiting_approval"


def test_a_card_sent_before_a_crash_is_shown_again_not_skipped(tmp_db, tmp_path, cfg):
    """The bot died between the send and the tap: the open card's item is re-sent, not passed over."""
    make_item(tmp_db, tmp_path, "crashed", score=0.9, stage="awaiting_approval")
    make_item(tmp_db, tmp_path, "fresh", score=0.5)
    telegram_bot.set_card_state(None, tmp_db)   # the card state was lost with the process
    transport = FakeTransport()

    assert make_bot(tmp_db, cfg, transport).ensure_card() == "crashed"


def test_the_poll_offset_is_persisted_so_a_tap_is_never_re_delivered(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.ensure_card()
    mid = telegram_bot.card_state(tmp_db)["mid"]
    transport.updates = [[tap("item-1", "ok", mid)], []]

    assert bot.poll_once(1) == 1
    assert db.kv_get(telegram_bot.K_OFFSET, db_path=tmp_db) == "2"
    assert bot.poll_once(1) == 0
    assert transport.of("getUpdates")[-1]["offset"] == 2


# ---------------------------------------------------------------------------
# Throttled edits
# ---------------------------------------------------------------------------

def test_edits_are_throttled_to_one_per_gap_and_the_latest_text_wins(tmp_db, cfg):
    transport = FakeTransport()
    clock = Clock()
    editor = telegram_bot.Editor(transport, CHAT_ID, clock=clock, gap=2.5)

    assert editor.edit(1, "text", "first", None) is True
    assert editor.edit(1, "text", "second", None) is False   # inside the gap
    assert editor.edit(1, "text", "third", None) is False
    assert len(transport.of("editMessageText")) == 1

    clock.now += 2.5
    editor.flush()                                            # the newest pending text goes out
    texts = [p["text"] for p in transport.of("editMessageText")]
    assert texts == ["first", "third"]


def test_a_button_tap_skips_the_gap_but_never_a_429_wait(tmp_db, cfg):
    transport = FakeTransport()
    clock = Clock()
    editor = telegram_bot.Editor(transport, CHAT_ID, clock=clock, gap=2.5)
    editor.edit(1, "text", "first", None)

    assert editor.edit(1, "text", "tapped", None, now=True) is True   # a tap is answered at once

    transport.raise_on["editMessageText"] = telegram_bot.TelegramError(
        "editMessageText", 429, "Too Many Requests", retry_after=30
    )
    assert editor.edit(1, "text", "limited", None, now=True) is False
    assert editor.blocked_until == clock.now + 30
    transport.raise_on.clear()
    assert editor.edit(1, "text", "still held", None, now=True) is False   # held even for a tap
    clock.now += 30
    assert editor.edit(1, "text", "after the wait", None, now=True) is True
    assert transport.of("editMessageText")[-1]["text"] == "after the wait"


# ---------------------------------------------------------------------------
# Auto mode
# ---------------------------------------------------------------------------

def test_auto_mode_approves_a_good_item_without_touching_telegram(tmp_db, tmp_path, cfg):
    cfg["approval"]["mode"] = "auto"
    make_item(tmp_db, tmp_path, "good")
    transport = FakeTransport()

    result = telegram_bot.run_approval_stage(
        cfg=cfg, db_path=tmp_db, bot=make_bot(tmp_db, cfg, transport), probe=lambda p: 20.0
    )

    assert result["mode"] == "auto"
    assert result["approved"] == ["good"]
    item = db.get_item("good", db_path=tmp_db)
    assert item["stage"] == "approved"
    assert item["approval_type"] == "auto_approved"
    assert item["chosen_variant"] is None
    assert transport.calls == []   # no Telegram call at all in auto mode


def test_auto_mode_rejects_an_ungrounded_hook(tmp_db, tmp_path, cfg):
    cfg["approval"]["mode"] = "auto"
    make_item(tmp_db, tmp_path, "fallback", grounded=0)

    telegram_bot.run_auto_approval(cfg=cfg, db_path=tmp_db, probe=lambda p: 20.0)

    item = db.get_item("fallback", db_path=tmp_db)
    assert item["stage"] == "rejected"
    assert "not grounded" in item["rejection_reason"]


def test_auto_mode_rejects_a_missing_render(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "norender", renders=False)
    passed, why = telegram_bot.quality_gate(
        db.get_item("norender", db_path=tmp_db), cfg=cfg, probe=lambda p: 20.0
    )
    assert passed is False
    assert "render missing" in why


def test_the_gate_caps_variant_a_but_lets_variant_b_be_longer_by_design(tmp_db, tmp_path, cfg):
    """Variant B is a freeze-frame plus the cut (PLAN.md 7), so it is checked against A's headroom."""
    item = make_item(tmp_db, tmp_path, "durations")
    lengths = {item["render_a_path"]: 20.4, item["render_b_path"]: 23.1}

    passed, why = telegram_bot.quality_gate(item, cfg=cfg, probe=lambda p: lengths[str(p)])
    assert passed is True, why

    lengths[item["render_a_path"]] = 25.0           # A itself over the 22s cap
    passed, why = telegram_bot.quality_gate(item, cfg=cfg, probe=lambda p: lengths[str(p)])
    assert passed is False and "variant A is 25.0s" in why

    lengths[item["render_a_path"]] = 20.0
    lengths[item["render_b_path"]] = 40.0           # B far beyond the freeze headroom
    passed, why = telegram_bot.quality_gate(item, cfg=cfg, probe=lambda p: lengths[str(p)])
    assert passed is False and "longer than A" in why


def test_the_gate_fails_when_ffprobe_cannot_read_a_render(tmp_db, tmp_path, cfg):
    item = make_item(tmp_db, tmp_path, "unreadable")
    passed, why = telegram_bot.quality_gate(item, cfg=cfg, probe=lambda p: None)
    assert passed is False and "ffprobe" in why


# ---------------------------------------------------------------------------
# Failure alerts
# ---------------------------------------------------------------------------

def test_failure_alerts_go_to_the_same_chat_and_the_same_failure_is_not_repeated(tmp_db, cfg):
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)

    assert bot.alert("render failed for item-9", key="render:item-9") is True
    assert bot.alert("render failed for item-9", key="render:item-9") is False   # inside the hour
    assert bot.alert("a different failure", key="other") is True

    sent = transport.of("sendMessage")
    assert len(sent) == 2
    assert all(p["chat_id"] == CHAT_ID for p in sent)
    assert sent[0]["text"].startswith("🚨")


def test_the_approval_stage_is_skipped_when_telegram_is_switched_off(tmp_db, cfg):
    cfg["telegram"]["enabled"] = False
    transport = FakeTransport()
    result = telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=make_bot(tmp_db, cfg, transport))
    assert result["skipped"] == "telegram.enabled is false"
    assert transport.calls == []


def test_one_runner_pass_sends_the_card_and_polls_once(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    transport.updates = [[]]

    result = telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot, poll_timeout=1)

    assert result == {"mode": "telegram", "card_sent": "item-1", "updates_handled": 0}
    assert transport.methods().count("getUpdates") == 1


# ---------------------------------------------------------------------------
# preview_only and the style preset
# ---------------------------------------------------------------------------

def _drop_preview_triggers(tmp_db) -> None:
    with db.db(tmp_db) as conn:
        for name in ("items_preview_only_never_released", "posts_block_preview_only_insert",
                     "posts_block_preview_only_update"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")


def test_a_preview_only_item_never_gets_a_card_even_without_the_trigger(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "item-preview", score=0.9)
    make_item(tmp_db, tmp_path, "item-real", score=0.1)
    _drop_preview_triggers(tmp_db)                      # the filter must hold on its own
    db.set_preview_only("item-preview", True, db_path=tmp_db)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)

    assert bot.ensure_card() == "item-real"             # the higher-scoring preview item is skipped
    assert db.get_item("item-preview", db_path=tmp_db)["stage"] == "rendered"
    # sending it directly is refused too, and so is the auto gate
    telegram_bot.set_card_state(None, tmp_db)
    assert bot.send_card(db.get_item("item-preview", db_path=tmp_db)) is None
    result = telegram_bot.run_auto_approval(cfg=cfg, db_path=tmp_db, probe=lambda p: 20.0)
    assert "item-preview" not in result["approved"] + result["rejected"]


def test_a_trigger_refusal_on_the_card_fails_that_item_and_the_next_one_is_offered(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "item-bad", score=0.9)
    make_item(tmp_db, tmp_path, "item-good", score=0.1)
    with db.db(tmp_db) as conn:   # the database refuses item-bad exactly as the real trigger would
        conn.execute(
            "CREATE TRIGGER refuse_bad BEFORE UPDATE OF stage ON items "
            "WHEN NEW.id = 'item-bad' AND NEW.stage IN ('awaiting_approval', 'approved') "
            "BEGIN SELECT RAISE(ABORT, 'preview_only item cannot enter an approval/posting stage'); END"
        )
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)

    assert bot.ensure_card() is None                    # refused, cleanly: nothing was sent
    assert transport.of("sendVideo") == []
    bad = db.get_item("item-bad", db_path=tmp_db)
    assert bad["stage"] == "failed" and bad["error_stage"] == "approval"
    assert telegram_bot.card_state(tmp_db) is None      # the slot is free
    assert bot.ensure_card() == "item-good"             # the runner carries on with the next item


def test_a_refused_decision_fails_the_item_instead_of_raising(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "item-1", stage="awaiting_approval")
    with db.db(tmp_db) as conn:
        conn.execute(
            "CREATE TRIGGER refuse_approve BEFORE UPDATE OF stage ON items WHEN NEW.stage = 'approved' "
            "BEGIN SELECT RAISE(ABORT, 'preview_only item cannot enter an approval/posting stage'); END"
        )
    outcome, answer = telegram_bot.decide("item-1", "ok", db_path=tmp_db)
    assert outcome == "" and "refused" in answer
    assert db.get_item("item-1", db_path=tmp_db)["stage"] == "failed"


def test_the_card_sends_the_renders_of_the_items_own_style_preset(tmp_db, tmp_path, cfg):
    item = make_item(tmp_db, tmp_path)
    # a preview render of another preset sits right next to the real ones and must be ignored
    (tmp_path / "P3_B.mp4").write_bytes(b"P" * 2048)
    db.transition_stage("item-1", "rendered", extra_fields={"style_preset": "P2"}, db_path=tmp_db)
    sent_files: list[str] = []

    class FileTransport(FakeTransport):
        def __call__(self, method, params, files=None, http_timeout=30.0):
            if files:
                sent_files.append(files["video"][0])
            return super().__call__(method, params, files, http_timeout)

    transport = FileTransport()
    make_bot(tmp_db, cfg, transport).ensure_card()

    assert sent_files == [Path(item["render_b_path"]).name, Path(item["render_a_path"]).name]
    videos = transport.of("sendVideo")
    assert "Style preset: P2" in videos[0]["caption"]
    assert "P2" in videos[1]["caption"]


# ---------------------------------------------------------------------------
# CP4: Telegram approval for week 1, then auto, with one notice
# ---------------------------------------------------------------------------

def test_approval_stays_on_telegram_until_seven_days_after_the_first_live_post(tmp_db, tmp_path, cfg, fake_clock):
    cfg["approval"]["switch_to_auto_after_days"] = 7
    assert telegram_bot.effective_mode(cfg, tmp_db) == "telegram"            # nothing live yet

    fake_clock.set("2027-01-01T00:00:00Z")                                   # months pass with no live post
    assert telegram_bot.effective_mode(cfg, tmp_db) == "telegram"

    db.kv_set(db.K_FIRST_LIVE, "2027-01-01T00:00:00Z", db_path=tmp_db)       # first live post
    fake_clock.set("2027-01-07T23:59:00Z")
    assert telegram_bot.effective_mode(cfg, tmp_db) == "telegram"
    fake_clock.set("2027-01-08T00:00:00Z")
    assert telegram_bot.effective_mode(cfg, tmp_db) == "auto"
    assert db.kv_get(telegram_bot.K_AUTO_SINCE, db_path=tmp_db) == "2027-01-08T00:00:00Z"


def test_the_switch_sends_exactly_one_notice_closes_the_open_card_and_uses_the_quality_gate(tmp_db, tmp_path, cfg, fake_clock):
    cfg["approval"]["switch_to_auto_after_days"] = 7
    make_item(tmp_db, tmp_path, "item-good", score=0.9)
    make_item(tmp_db, tmp_path, "item-ungrounded", score=0.5, grounded=0)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot, poll_timeout=1, probe=lambda p: 20.0)
    assert telegram_bot.card_state(tmp_db)["item_id"] == "item-good"         # week 1: a card is open

    db.kv_set(db.K_FIRST_LIVE, "2026-11-01T00:00:00Z", db_path=tmp_db)
    fake_clock.set("2026-11-08T00:00:01Z")
    results = [
        telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot, poll_timeout=1, probe=lambda p: 20.0)
        for _ in range(3)
    ]

    assert [r["mode"] for r in results] == ["auto"] * 3
    assert [r["switch_notice_sent"] for r in results] == [True, False, False]
    notices = [p["text"] for p in transport.of("sendMessage") if "Approvals are now automatic" in p["text"]]
    assert len(notices) == 1 and "7 days" in notices[0]
    assert telegram_bot.card_state(tmp_db) is None                           # the open card was retired
    assert any("Auto approval took over" in p.get("caption", "") for p in transport.of("editMessageCaption"))
    # auto mode is the existing quality gate
    assert db.get_item("item-good", db_path=tmp_db)["approval_type"] == "auto_approved"
    bad = db.get_item("item-ungrounded", db_path=tmp_db)
    assert bad["stage"] == "rejected" and "auto_quality_gate" in bad["rejection_reason"]
    assert transport.methods().count("getUpdates") == 1                      # no more polling once auto


def test_a_notice_that_could_not_be_delivered_is_retried_and_still_sent_only_once(tmp_db, tmp_path, cfg, fake_clock):
    cfg["approval"]["switch_to_auto_after_days"] = 7
    db.kv_set(db.K_FIRST_LIVE, "2026-11-01T00:00:00Z", db_path=tmp_db)
    fake_clock.set("2026-11-09T00:00:00Z")
    transport = FakeTransport()
    transport.raise_on["sendMessage"] = telegram_bot.TelegramError("sendMessage", 502, "bad gateway")
    bot = make_bot(tmp_db, cfg, transport)

    first = telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot)
    assert first["mode"] == "auto" and first["switch_notice_sent"] is False  # auto regardless of the notice
    transport.raise_on.clear()
    assert telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot)["switch_notice_sent"] is True
    assert telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot)["switch_notice_sent"] is False


def test_switch_to_auto_can_be_turned_off_and_that_undoes_a_switch(tmp_db, tmp_path, cfg, fake_clock):
    db.kv_set(db.K_FIRST_LIVE, "2026-10-01T00:00:00Z", db_path=tmp_db)
    cfg["approval"]["switch_to_auto_after_days"] = 7
    assert telegram_bot.effective_mode(cfg, tmp_db) == "auto"
    cfg["approval"]["switch_to_auto_after_days"] = None
    assert telegram_bot.effective_mode(cfg, tmp_db) == "telegram"
    cfg["approval"]["mode"] = "auto"
    assert telegram_bot.effective_mode(cfg, tmp_db) == "auto"


def test_an_untapped_synthetic_dry_run_card_does_not_block_real_cards(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "synthetic-cp3-1", score=0.1)
    make_item(tmp_db, tmp_path, "item-real", score=0.9)
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.send_card(db.get_item("synthetic-cp3-1", db_path=tmp_db))            # what scripts/cp3_dry_run.py does
    assert telegram_bot.card_state(tmp_db)["item_id"] == "synthetic-cp3-1"

    result = telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot, poll_timeout=1)

    assert result["card_sent"] == "item-real"
    assert db.get_item("synthetic-cp3-1", db_path=tmp_db)["stage"] == "rejected"


def test_a_tap_on_the_synthetic_card_is_still_honoured_before_it_is_released(tmp_db, tmp_path, cfg):
    make_item(tmp_db, tmp_path, "synthetic-cp3-1")
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    bot.send_card(db.get_item("synthetic-cp3-1", db_path=tmp_db))
    transport.updates.append([tap("synthetic-cp3-1", "ok", telegram_bot.card_state(tmp_db)["mid"])])

    telegram_bot.run_approval_stage(cfg=cfg, db_path=tmp_db, bot=bot, poll_timeout=1)

    assert db.get_item("synthetic-cp3-1", db_path=tmp_db)["stage"] == "approved"   # the tap counted
    assert telegram_bot.card_state(tmp_db) is None


def test_two_different_alerts_alternating_are_each_still_rate_limited(tmp_db, cfg, fake_clock):
    transport = FakeTransport()
    bot = make_bot(tmp_db, cfg, transport)
    sent = [bot.alert(f"stage {k} failed", key=k) for k in ("a", "b", "a", "b")]
    assert sent == [True, True, False, False]
    fake_clock.advance(hours=1, seconds=1)
    assert bot.alert("stage a failed", key="a") is True
    assert bot.alert("once only", key="once", repeat_gap_s=None) is True
    fake_clock.advance(days=3)
    assert bot.alert("once only", key="once", repeat_gap_s=None) is False
