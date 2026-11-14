"""Persistent controls, applied by the runner between stages. No network calls."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import (
    binding,
    clock,
    db,
    hooks,
    posting,
    rate_limit,
    scheduler,
    telegram_bot,
)
from .store import transaction

ACTIONS = {
    "approve": "Approve both variants",
    "aonly": "Approve A only",
    "reject": "Reject item",
    "hookok": "Hook is right / render anyway",
    "edit_hook": "Edit hook and re-render",
    "reslot": "Move to next available slot",
    "retry": "Retry failed stage",
    "pause": "Pause all posting",
    "resume": "Resume posting",
}
DECISIONS = {"approve": "ok", "aonly": "aonly", "reject": "no", "hookok": "hookok"}
EDITABLE = {
    "cut",
    "needs_review",
    "rendered",
    "awaiting_approval",
    "approved",
    "queued",
}
RETRY_STAGES = {"download": "discovered", "cut": "downloaded", "render": "cut"}


class ControlError(ValueError):
    pass


def display_settings(cfg):
    """Only non-secret settings needed to describe the running process."""
    keys = {
        "app": ("dry_run",),
        "posting": (
            "profile",
            "profiles",
            "min_gap_hours",
            "daily_post_target",
            "max_slot_search_days",
        ),
        "approval": ("mode", "switch_to_auto_after_days"),
        "buffer": ("target_ready_items",),
        "autopilot": (
            "week_one_days",
            "bandit_min_exploration_floor",
            "bandit_min_samples_per_variant",
        ),
    }
    return {
        section: {
            key: cfg.get(section, {}).get(key)
            for key in names
            if key in cfg.get(section, {})
        }
        for section, names in keys.items()
    }


def fingerprint(item, posts):
    fields = (
        "stage",
        "cut_start",
        "cut_end",
        "cut_path",
        "hook_text",
        "voiceover_script",
        "hook_binding",
        "voiceover_binding",
        "render_a_path",
        "render_b_path",
        "preview_only",
        "error_stage",
        "error_message",
        "hook_review",
        "approval_type",
    )
    state = {k: item.get(k) for k in fields} if item else {}
    # A card being sent changes rendered to awaiting_approval without changing its content.
    if state.get("stage") == "awaiting_approval":
        state["stage"] = "rendered"
    state["posts"] = [
        {
            k: p.get(k)
            for k in (
                "id",
                "status",
                "remote_container_id",
                "remote_id",
                "attempt_count",
                "publish_started_at",
                "slot_datetime",
            )
        }
        for p in posts
    ]
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def unstarted(posts):
    return all(
        p["status"] in ("pending", "dry_run")
        and not p.get("remote_container_id")
        and not p.get("remote_id")
        and not p.get("publish_started_at")
        and not p.get("attempt_count")
        for p in posts
    )


def validate(action, item, posts, payload):
    if action not in ACTIONS:
        raise ControlError("Unknown action.")
    if action in ("pause", "resume"):
        return
    if not item:
        raise ControlError("Item no longer exists.")
    if not db.is_releasable(item):
        raise ControlError(
            "Preview-only or synthetic items cannot be controlled or approved."
        )
    if not unstarted(posts):
        raise ControlError(
            "Publishing has started or failed. This item is display-only to prevent duplicate posts."
        )
    stage = item["stage"]
    if action in DECISIONS:
        expected = (
            {"needs_review"}
            if action == "hookok"
            else {"rendered", "awaiting_approval"}
        )
        if action == "reject":
            expected.add("needs_review")
        if stage not in expected:
            raise ControlError("This decision no longer applies. Refresh the item.")
        if action in ("approve", "aonly"):
            binding.assert_caption_binding(
                item, item.get("hook_text"), vo_path=item.get("voiceover_audio_path")
            )
            variants = ("a", "b") if action == "approve" else ("a",)
            for variant in variants:
                if not Path(item.get(f"render_{variant}_path") or "").is_file():
                    raise ControlError(
                        f"Render {variant.upper()} is missing. Re-render before approval."
                    )
        if action == "hookok" and not item.get("hook_binding"):
            raise ControlError(
                "No segment binding exists. Edit the hook to request a new check."
            )
    elif action == "edit_hook":
        if stage not in EDITABLE:
            raise ControlError("Hook editing requires an unposted cut or render.")
        raw_text = payload.get("hook_text")
        if not isinstance(raw_text, str):
            raise ControlError("Hook text must be a string.")
        text = raw_text.strip()
        if not text or len(text) > 220 or any(ord(c) < 32 for c in text):
            raise ControlError("Use a single hook line of 1–220 characters.")
        if not Path(item.get("cut_path") or "").is_file():
            raise ControlError(
                "The cut file is missing. Editing cannot request a re-render."
            )
    elif action == "reslot":
        if stage != "queued" or not posts:
            raise ControlError("Only an unstarted queued item can be moved.")
        try:
            when = datetime.fromisoformat(str(payload["after"]).replace("Z", "+00:00"))
            if when.tzinfo is None:
                raise ValueError()
            if when < clock.utcnow() - timedelta(minutes=1):
                raise ValueError()
        except (KeyError, ValueError):
            raise ControlError(
                "Choose a future time with an explicit timezone."
            ) from None
    elif action == "retry":
        if stage != "failed" or item.get("error_stage") not in RETRY_STAGES or posts:
            raise ControlError(
                "Retry is limited to download, cut and render failures with no posting history."
            )
        required = {"cut": "download_path", "render": "cut_path"}.get(
            item["error_stage"]
        )
        if required and not Path(item.get(required) or "").is_file():
            raise ControlError(
                f"Required {required} file is missing; automatic retry would fail again."
            )


def available_actions(item, posts):
    if not db.is_releasable(item) or not unstarted(posts):
        return []
    stage = item["stage"]
    actions = []
    if stage in ("rendered", "awaiting_approval"):
        actions += ["approve", "aonly", "reject"]
    if stage == "needs_review":
        actions += ["hookok", "reject"]
    if stage in EDITABLE:
        actions.append("edit_hook")
    if stage == "queued" and posts:
        actions.append("reslot")
    if stage == "failed" and item.get("error_stage") in RETRY_STAGES and not posts:
        actions.append("retry")
    return actions


def prepare(action, item_id, payload, path, cfg):
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload)
    detail = ACTIONS[action]
    if action == "reslot":
        bounded = copy.deepcopy(cfg)
        bounded.setdefault("posting", {})["daily_post_target"] = min(
            2, int(bounded["posting"].get("daily_post_target", 2))
        )
        slot = scheduler.next_free_slot(
            clock.parse(payload["after"]), bounded, path, item_id
        )
        detail = f"Next available slot: {slot.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}"
    return {
        "action": action,
        "item_id": item_id,
        "payload": payload,
        "expected": fingerprint(item, posts),
        "description": detail,
        "title": (item or {}).get("original_title") or "All platforms",
    }


def enqueue(plan, command_id, path):
    with transaction(path) as conn:
        existing = conn.execute(
            "SELECT * FROM dashboard_commands WHERE id = ?", (command_id,)
        ).fetchone()
        if existing:
            return dict(existing)
        item_id = plan["item_id"]
        item = db.get_item(item_id, path) if item_id else None
        posts = posting.get_posts_for_item(item_id, path) if item_id else []
        validate(plan["action"], item, posts, plan["payload"])
        if fingerprint(item, posts) != plan["expected"]:
            raise ControlError(
                "The item changed while you were confirming. Refresh and try again."
            )
        conn.execute(
            "INSERT INTO dashboard_commands (id, action, item_id, payload, expected, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                command_id,
                plan["action"],
                item_id,
                json.dumps(plan["payload"]),
                plan["expected"],
                db.now(),
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM dashboard_commands WHERE id = ?", (command_id,)
            ).fetchone()
        )


def release_card(item_id, path):
    state = telegram_bot.card_state(path)
    if state and state.get("item_id") == item_id:
        telegram_bot.set_card_state(None, path)


def apply(command, cfg, path):
    """Called only by the runner under its existing stage serialization."""
    action, item_id = command["action"], command["item_id"]
    payload = json.loads(command["payload"])
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload)
    if fingerprint(item, posts) != command["expected"]:
        raise ControlError(
            "State changed before the runner reached this command. Refresh and confirm again."
        )
    if action == "pause":
        # Existing Meta cooldown, held until explicit resume (maximum representable date).
        hours = (
            datetime(9999, 1, 1, tzinfo=timezone.utc) - clock.utcnow()
        ).total_seconds() / 3600
        rate_limit.record_error(
            posting.META_SOURCE,
            "Paused by dashboard; resume from the control panel",
            hours,
            path,
        )
        return "All Meta posting paused until resumed."
    if action == "resume":
        rate_limit.record_success(posting.META_SOURCE, path)
        return "Meta pause cleared. The runner will use the normal posting slots."
    if action in DECISIONS:
        outcome, message = telegram_bot.decide(item_id, DECISIONS[action], path)
        if not outcome or outcome == telegram_bot.NOT_APPLICABLE:
            raise ControlError(message)
        release_card(item_id, path)
        return message
    if action == "edit_hook":
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id = ?", (item_id,))
        fields = {
            "hook_text": payload["hook_text"].strip(),
            "hook_grounded": 0,
            "hook_binding": None,
            "voiceover_binding": None,
            "voiceover_audio_path": None,
            "render_a_path": None,
            "render_b_path": None,
            "caption_text": None,
            "keywords": None,
            "chosen_variant": None,
            "approval_type": None,
            "ab_excluded": 0,
            "hook_review": json.dumps(
                {
                    "kind": "dashboard_edit",
                    "at": db.now(),
                    "previous_hook": item.get("hook_text"),
                }
            ),
        }
        db.transition_stage(item_id, "cut", fields, path)
        release_card(item_id, path)
        return "Hook queued for the runner's claim, overlap and Gemini checks, then new renders."
    if action == "reslot":
        bounded = copy.deepcopy(cfg)
        bounded.setdefault("posting", {})["daily_post_target"] = min(
            2, int(bounded["posting"].get("daily_post_target", 2))
        )
        after = max(clock.utcnow(), clock.parse(payload["after"]))
        slot = clock.iso(scheduler.next_free_slot(after, bounded, path, item_id))
        with db.db(path) as conn:
            conn.execute(
                "UPDATE posts SET slot_datetime = ?, variant_decided_at = NULL, updated_at = ? "
                "WHERE item_id = ?",
                (slot, db.now(), item_id),
            )
        return f"Moved to {slot}."
    fields = {"error_stage": None, "error_message": None}
    if item["error_stage"] in ("download", "cut"):
        fields.update(binding.SEGMENT_CONTENT_FIELDS)
    if item["error_stage"] == "render":
        fields.update(render_a_path=None, render_b_path=None)
    db.transition_stage(item_id, RETRY_STAGES[item["error_stage"]], fields, path)
    return f"Returned to {RETRY_STAGES[item['error_stage']]} for the runner to retry."


def runner_boundary(cfg, path, stage):
    """No-op until the dashboard has initialized its mailbox. Atomic, replay-safe commands."""
    with db.db(path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dashboard_commands'"
        ).fetchone()
    if not exists:
        return
    remaining = max(0, (hooks._QUOTA_BLOCKED_UNTIL or 0) - hooks._now())
    db.kv_set(
        "dashboard:runtime",
        json.dumps(
            {
                "at": db.now(),
                "stage": stage,
                "dry_run": posting.is_dry_run(cfg),
                "profile": scheduler.active_profile(cfg)[0],
                "settings": display_settings(cfg),
                "gemini_blocked_until": (
                    clock.iso(clock.utcnow() + timedelta(seconds=remaining))
                    if remaining
                    else None
                ),
            }
        ),
        path,
    )
    with db.db(path) as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM dashboard_commands WHERE status='pending' ORDER BY created_at, rowid LIMIT 50"
            )
        ]
    for command_id in ids:
        with transaction(path) as conn:
            row = conn.execute(
                "SELECT * FROM dashboard_commands WHERE id=? AND status='pending'",
                (command_id,),
            ).fetchone()
            if row is None:
                continue
            conn.execute("SAVEPOINT control")
            try:
                message = apply(dict(row), cfg, path)
                if row["item_id"]:
                    db.log_stage(
                        row["item_id"],
                        "dashboard",
                        "success",
                        f"{row['action']}: {message}",
                        db_path=path,
                    )
                status = "applied"
                conn.execute("RELEASE control")
            except (ValueError, binding.CaptionMismatchError, RuntimeError) as exc:
                conn.execute("ROLLBACK TO control")
                conn.execute("RELEASE control")
                status, message = "refused", str(exc)[:500]
            conn.execute(
                "UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
                (status, message, db.now(), command_id),
            )
