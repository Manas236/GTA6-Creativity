"""Persistent controls, applied by the runner between stages. No network calls."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import sqlite3
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
    runner,
    telegram_bot,
)
from .store import transaction
from . import queue_controls

log = logging.getLogger(__name__)

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
    "make": "Make clips now",
    "clip_url": "Clip from URL",
    "remake_cut": "Remake with another moment",
    "remake_hook": "Regenerate hook and render",
    "delete": "Delete item and its local media",
    "unapprove": "Return to review",
    "up": "Move up",
    "down": "Move down",
    "post_now": "Post at the earliest allowed time",
    "edit_caption": "Save caption",
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
        "caption_body",
        "caption_hashtags",
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
    if action in ("pause", "resume"):
        if action == "resume" and not rate_limit.dashboard_meta_paused(path):
            raise ControlError("No dashboard pause to resume. Meta cooldowns expire on their own.")
        return
    if action == "make":
        if type(payload.get("count")) is not int or not 1 <= payload["count"] <= 10:
            raise ControlError("Choose 1–10 clips.")
        return
    if action == "clip_url":
        try:
            platform, source_id, _ = creation.source_from_url(payload["url"])
            start, end = (creation.hint(payload.get(key)) for key in ("start", "end"))
            if end is not None and end <= (start or 0):
                raise ValueError("The end hint must be after the start.")
            prior = creation.existing(platform, source_id, path)
            if prior:
                raise ValueError(f"Source already seen: /items/{prior}")
        except (KeyError, ValueError, TypeError) as exc:
            raise ControlError(str(exc)) from None
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
    if stage in ("posted", "measured", "cleaned"):
        raise ControlError("Remove published media on Instagram yourself.")
    if action == "delete" and stage == "rejected" and item.get("rejection_reason") == "deleted from dashboard":
        raise ControlError("This item is already deleted.")
    if action == "unapprove" and stage not in ("approved", "queued"):
        raise ControlError("Only approved or queued items can return to review.")
    if action in ("up", "down") and (stage != "queued" or not posts):
        raise ControlError("Only scheduled items can move in the queue.")
    if action == "post_now" and stage not in ("approved", "queued"):
        raise ControlError("Approve the item before posting.")
    if action in ("remake_cut", "remake_hook") and action not in available_actions(item, posts):
        raise ControlError("This item has no usable source or cut for that remake.")
    if action == "edit_caption":
        if stage not in EDITABLE:
            raise ControlError("Caption editing requires an unposted cut or render.")
        body, tags = caption_values(payload)
        caption = posting.caption_for_item({**item, "caption_body": body, "caption_hashtags": tags})
        if len(caption) > posting.IG_CAPTION_LIMIT:
            raise ControlError("Keep the full caption, including credit, within 2,200 characters.")
        if len(re.findall(r"(?<!\w)#[\w]+", caption)) > 30:
            raise ControlError("Use no more than 30 hashtags across the caption.")
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


def caption_values(payload):
    body, hashtags = payload.get("body", ""), payload.get("hashtags", "")
    if not isinstance(body, str) or not isinstance(hashtags, str):
        raise ControlError("Caption and hashtags must be text.")
    tags = hashtags.split()
    if any(not re.fullmatch(r"#[\w]+", tag) for tag in tags):
        raise ControlError("Separate hashtags with spaces and start each with #.")
    return body.strip(), " ".join(tags)


def available_actions(item, posts):
    if not db.is_releasable(item) or not unstarted(posts):
        return []
    stage = item["stage"]
    if stage in ("posted", "measured", "cleaned"):
        return []
    actions = [] if item.get("rejection_reason") == "deleted from dashboard" else ["delete"]
    if stage in ("approved", "queued"):
        actions += ["unapprove", "post_now"]
    if stage == "queued" and posts:
        actions += ["up", "down"]
    if stage in EDITABLE:
        actions.append("edit_caption")
    if actions and item.get("source_url") and item.get("cut_start") is not None:
        actions.append("remake_cut")
    if actions and item.get("cut_path") and Path(item["cut_path"]).is_file():
        actions.append("remake_hook")
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
    if item_id and action in ("pause", "resume", "make", "clip_url"):
        raise ControlError("This control does not select an item.")
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload, path)
    detail = ACTIONS[action]
    if action == "post_now":
        if posting.meta_paused(path)[0] or not posting.enabled_platforms(cfg):
            raise ControlError("Posting is paused or no platforms are enabled.")
        try:
            slot = queue_controls.earliest(cfg, path, item_id)
        except ValueError as exc:
            raise ControlError(str(exc)) from None
        payload = {**payload, "slot": clock.iso(slot)}
        detail = f"Earliest allowed slot: {slot.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}"
        if posting.is_dry_run(cfg):
            detail += " (dry run)"
        if not runner.active_lock(path, runner._lock_settings(cfg)[0]):
            detail += ". Start runner and post."
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
    """Apply a command while holding the runner lock."""
    action, item_id = command["action"], command["item_id"]
    payload = json.loads(command["payload"])
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload, path)
    if fingerprint(item, posts) != command["expected"]:
        raise ControlError(
            "State changed before the runner reached this command. Refresh and confirm again."
        )
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
    if action in ("delete", "unapprove", "remake_cut", "remake_hook"):
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id=?", (item_id,))
        release_card(item_id, path)
        if action == "delete":
            db.mark_rejected(item_id, "deleted from dashboard", db_path=path)
            return "Deleted from dashboard."
        fields = {"chosen_variant": None, "approval_type": None, "ab_excluded": 0}
        if action == "unapprove":
            db.transition_stage(item_id, "awaiting_approval", fields, path)
            return "Returned to review."
        stage = "cut" if action == "remake_hook" else "downloaded"
        if stage == "downloaded" and not Path(item.get("download_path") or "").is_file():
            stage = "discovered"
        fields.update(binding.SEGMENT_CONTENT_FIELDS, creation_job=command["id"], error_message=None,
                      error_stage=None, caption_body=None, caption_hashtags=None)
        db.transition_stage(item_id, stage, fields, path)
        return "Remake started."
    if action == "edit_caption":
        body, tags = caption_values(payload)
        fields = {"caption_body": body, "caption_hashtags": tags}
        db.transition_stage(item_id, item["stage"], fields, path)
        with db.db(path) as conn:
            conn.execute("UPDATE posts SET caption_text=?, updated_at=? WHERE item_id=?",
                         (posting.caption_for_item({**item, **fields}, cfg), db.now(), item_id))
        return "Caption saved."
    if action in ("up", "down"):
        return queue_controls.move(item_id, -1 if action == "up" else 1, cfg, path)
    if action == "post_now":
        if posting.meta_paused(path)[0] or not posting.enabled_platforms(cfg):
            raise ControlError("Posting is paused or no platforms are enabled.")
        slot = clock.parse(payload["slot"])
        if (not queue_controls.allowed(slot, scheduler.reserved_times(path, item_id), cfg)
                or not queue_controls.allowed(max(slot, clock.utcnow()),
                                              scheduler.reserved_times(path, item_id), cfg)):
            raise ControlError("The earliest slot changed. Open the confirmation again.")
        if (clock.utcnow() - slot).total_seconds() > posting.missed_slot_grace(cfg):
            raise ControlError("The confirmed slot expired. Open the confirmation again.")
        reason = posting.refusal_reason(item, cfg)
        if reason:
            raise ControlError(reason)
        posting.ensure_post_rows(item, clock.iso(slot), cfg, path)
        queue_controls.set_slot(item_id, clock.iso(slot), path)
        db.transition_stage(item_id, "queued", db_path=path)
        return f"Scheduled for {slot.astimezone(scheduler.active_profile(cfg)[1]).isoformat()}."
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
                "SELECT id FROM dashboard_commands WHERE status='pending' AND action NOT IN "
                "('make','clip_url','remake_cut','remake_hook') ORDER BY created_at, rowid LIMIT 50"
            )
        ]
    for command_id in ids:
        apply_pending(command_id, cfg, path)


def apply_pending(command_id, cfg, path, media_root=None):
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

        result = dict(conn.execute("SELECT * FROM dashboard_commands WHERE id=?", (command_id,)).fetchone())
    if status == "applied" and row["action"] == "delete":
        item = db.get_item(row["item_id"], path)
        try:
            cleanup.Cleaner(cfg, path, media_root=media_root).purge(
                item, cleanup.ALL_COLUMNS, subtitles=True)
        except (OSError, sqlite3.OperationalError):
            log.exception("Media cleanup could not finish for deleted item %s", row["item_id"])
    return result


def submit(plan, command_id, path, cfg, media_root=None):
    command = enqueue(plan, command_id, path)
    if command["status"] != "pending":
        return command
    stale, heartbeat = runner._lock_settings(cfg)
    if runner.active_lock(path, stale):
        return command
    if plan["action"] in creation.ACTIONS or not runner.acquire_lock(path, stale, heartbeat, "dashboard"):
        return command
    try:
        return apply_pending(command_id, cfg, path, media_root) or command
    finally:
        runner.release_lock(path)
