"""Persistent controls, applied by the runner between stages. No network calls."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from pipeline import (
    binding,
    clock,
    cleanup,
    creation,
    db,
    hooks,
    posting,
    rate_limit,
    scheduler,
    telegram_bot,
)
from . import queue_controls
from .store import transaction

log = logging.getLogger(__name__)

ACTIONS = {
    "delete": "Delete item",
    "bulk_reject": "Reject item",
    "unapprove": "Return to review",
    "up": "Move up",
    "down": "Move down",
    "post_now": "Post now",
    "edit_caption": "Save caption",
    "make": "Make clips",
    "clip_url": "Clip from URL",
    "remake": "Remake next-best cut",
    "remake_hook": "Regenerate hook",
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
LONG = {"make", "clip_url", "remake", "remake_hook"}
ITEM_TERMINAL = {"posted", "measured", "cleaned"}
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
            "platforms",
            "min_gap_hours",
            "daily_post_target",
            "max_slot_search_days",
        ),
        "approval": ("mode", "switch_to_auto_after_days"),
        "cut": ("heatmap_weight",),
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
        "caption_body_override",
        "caption_tags_override",
        "rejection_reason",
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


def validate(action, item, posts, payload, path):
    if action not in ACTIONS:
        raise ControlError("Unknown action.")
    if action == "make":
        if type(payload.get("count")) is not int or not 1 <= payload["count"] <= 10:
            raise ControlError("Choose 1–10 clips.")
        return
    if action == "clip_url":
        check(creation.manual_details, payload, path)
        return
    if action in ("pause", "resume"):
        if action == "resume" and not rate_limit.dashboard_meta_paused(path):
            raise ControlError("No dashboard pause to resume. Meta cooldowns expire on their own.")
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
    if stage in ITEM_TERMINAL:
        raise ControlError("This item has posted. Remove it on Instagram yourself.")
    if action in ("delete", "bulk_reject") and stage == "rejected":
        if action == "bulk_reject" or item.get("rejection_reason") == "deleted from dashboard":
            raise ControlError("This item has already been rejected or deleted.")
    if action in ("unapprove", "post_now", "up", "down"):
        if stage not in ("approved", "queued") or (action in ("up", "down") and not posts):
            raise ControlError("This control requires an unstarted approved or queued item.")
    if action == "edit_caption":
        if stage not in {"rendered", "awaiting_approval", "approved", "queued"}:
            raise ControlError("Wait until the clip has rendered.")
        check(queue_controls.caption_fields, item, payload)
    if action in ("remake", "remake_hook"):
        if item.get("rejection_reason") == "deleted from dashboard" or item.get("cut_start") is None:
            raise ControlError("Remake requires an existing cut.")
        if action == "remake_hook" and not Path(item.get("cut_path") or "").is_file():
            raise ControlError("The cut file is missing.")
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
        if not item.get("hook_text") or not item.get("voiceover_script"):
            raise ControlError(
                "Wait for the runner to create the original hook and voiceover script before editing."
            )
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


def check(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise ControlError(str(exc)) from exc


def available_actions(item, posts):
    if not db.is_releasable(item) or not unstarted(posts):
        return []
    stage = item["stage"]
    if stage in ITEM_TERMINAL or item.get("rejection_reason") == "deleted from dashboard":
        return []
    actions = ["delete"]
    if stage != "rejected":
        actions.append("bulk_reject")
    if item.get("cut_start") is not None:
        actions.append("remake")
        if item.get("cut_path"):
            actions.append("remake_hook")
    if stage in {"rendered", "awaiting_approval", "approved", "queued"}:
        actions.append("edit_caption")
    if stage in {"approved", "queued"}:
        actions += ["unapprove", "post_now"]
    if stage == "queued" and posts:
        actions += ["up", "down"]
    if stage in ("rendered", "awaiting_approval"):
        actions += ["approve", "aonly", "reject"]
    if stage == "needs_review":
        actions += ["hookok", "reject"]
    if stage in EDITABLE and item.get("hook_text") and item.get("voiceover_script"):
        actions.append("edit_hook")
    if stage == "queued" and posts:
        actions.append("reslot")
    if stage == "failed" and item.get("error_stage") in RETRY_STAGES and not posts:
        actions.append("retry")
    return actions


def prepare(action, item_id, payload, path, cfg):
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload, path)
    detail = ACTIONS[action]
    payload = dict(payload)
    if action == "post_now":
        slot = check(queue_controls.next_post, item_id, cfg, path)
        payload["slot"] = clock.iso(slot)
        detail = f"{'Dry-run simulation' if posting.is_dry_run(cfg) else 'Post'} at {slot.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}"
    if action in ("up", "down"):
        payload["swap"] = check(queue_controls.swap, item_id, -1 if action == "up" else 1,
                                path, cfg, unstarted)
    if action == "edit_caption":
        detail = check(queue_controls.caption_fields, item, payload)[2]
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
        validate(plan["action"], item, posts, plan["payload"], path)
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
    """Apply a validated command while the caller holds the runner lock."""
    action, item_id = command["action"], command["item_id"]
    payload = json.loads(command["payload"])
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload, path)
    if fingerprint(item, posts) != command["expected"]:
        raise ControlError(
            "State changed before the runner reached this command. Refresh and confirm again."
        )
    if action in ("delete", "bulk_reject", "unapprove"):
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id=?", (item_id,))
        release_card(item_id, path)
        if action == "unapprove":
            db.transition_stage(item_id, "awaiting_approval",
                                {"approval_type": None, "chosen_variant": None, "ab_excluded": 0}, path)
            return "Returned to review. Posting slot released."
        reason = "deleted from dashboard" if action == "delete" else "user_rejected"
        if action == "bulk_reject" and item["stage"] in {"rendered", "awaiting_approval", "needs_review"}:
            return telegram_bot.decide(item_id, "no", path)[1]
        db.mark_rejected(item_id, reason, db_path=path)
        return "Item deleted." if action == "delete" else "Item rejected."
    if action == "edit_caption":
        body, tags, caption = check(queue_controls.caption_fields, item, payload)
        with db.db(path) as conn:
            conn.execute("UPDATE items SET caption_body_override=?, caption_tags_override=?, updated_at=? WHERE id=?",
                         (body, tags, db.now(), item_id))
            conn.execute("UPDATE posts SET caption_text=?, updated_at=? WHERE item_id=?",
                         (caption, db.now(), item_id))
        return "Caption saved."
    if action in ("up", "down"):
        other, here, there = check(queue_controls.swap, item_id, -1 if action == "up" else 1,
                                   path, cfg, unstarted, payload["swap"])
        with db.db(path) as conn:
            conn.executemany("UPDATE posts SET slot_datetime=?, updated_at=? WHERE item_id=?",
                             ((there, db.now(), item_id), (here, db.now(), other)))
        return f"Moved {'up' if action == 'up' else 'down'} in the queue."
    if action == "post_now":
        check(queue_controls.next_post, item_id, cfg, path)
        slot = clock.parse(payload["slot"])
        if not scheduler.slot_allowed(slot, scheduler.reserved_times(path, item_id), cfg):
            raise ControlError("The confirmed time is no longer available. Confirm a new time.")
        if slot < clock.utcnow() - timedelta(minutes=posting.missed_slot_grace(cfg)):
            raise ControlError("The confirmed time has passed. Confirm a new time.")
        posting.ensure_post_rows(item, payload["slot"], cfg, path)
        with db.db(path) as conn:
            conn.execute("UPDATE posts SET slot_datetime=?, status='pending', updated_at=? WHERE item_id=?",
                         (payload["slot"], db.now(), item_id))
        db.transition_stage(item_id, "queued", db_path=path)
        return f"Queued for {slot.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}."
    if action == "pause":
        rate_limit.set_dashboard_meta_pause(True, path)
        return "Posting paused by the dashboard until resumed."
    if action == "resume":
        rate_limit.set_dashboard_meta_pause(False, path)
        if posting.meta_paused(path)[0]:
            return "Dashboard pause lifted. Meta's cooldown is still active."
        return "Dashboard pause lifted. The runner will use the normal posting slots."
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
                "SELECT id FROM dashboard_commands WHERE status IN ('pending','running') ORDER BY created_at, rowid LIMIT 50"
            )
        ]
    for command_id in ids:
        with db.db(path) as conn:
            row = conn.execute("SELECT action FROM dashboard_commands WHERE id=?", (command_id,)).fetchone()
        if row and row["action"] in LONG:
            if stage == "discover":
                from . import jobs
                jobs.run(command_id, cfg, path)
        else:
            process(command_id, cfg, path)


def process(command_id, cfg, path):
    with transaction(path) as conn:
        row = conn.execute(
            "SELECT * FROM dashboard_commands WHERE id=? AND status='pending'",
            (command_id,),
        ).fetchone()
        if row is None:
            return
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
        except Exception as exc:
            conn.execute("ROLLBACK TO control")
            conn.execute("RELEASE control")
            if isinstance(exc, (ControlError, binding.CaptionMismatchError)):
                status, message = "refused", str(exc)[:500]
                log.info("Dashboard command %s refused: %s", command_id, message)
            else:
                status, message = "failed", f"{type(exc).__name__}: {exc}"[:500]
                log.exception("Dashboard command %s failed", command_id)
        conn.execute(
            "UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
            (status, message, db.now(), command_id),
        )
    if status == "applied" and row["action"] == "delete":
        item = db.get_item(row["item_id"], path)
        if item and item.get("rejection_reason") == "deleted from dashboard":
            try:
                cleanup.Cleaner(cfg, path).purge(item, cleanup.ALL_COLUMNS, subtitles=True)
            except Exception:
                log.exception("Dashboard media deletion incomplete for %s; cleanup can retry", item["id"])
