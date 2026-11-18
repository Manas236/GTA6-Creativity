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
from .presentation import slot_time
from . import queue_controls

log = logging.getLogger(__name__)

ACTIONS = {
    "approve": "Approve both variants",
    "aonly": "Approve A only",
    "bonly": "Approve B only",
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
    "position": "Move to queue position",
    "restore_approval": "Restore previous approval",
    "up": "Move up",
    "down": "Move down",
    "post_now": "Post at the earliest allowed time",
    "edit_caption": "Save caption",
    "approve_versions": "Approve preset versions",
    "edit_caption_words": "Edit burned-in captions",
    "edit_voiceover": "Edit voiceover",
    "move_to_slot": "Move to exact slot",
    "make_from_discovered": "Make clip from discovered source",
    "snapshot_now": "Take analytics snapshot",
    "run_stage": "Run a pipeline stage",
    "go_live": "Go live",
    "go_dry": "Enable dry run",
}
GLOBAL_ACTIONS = {"go_live", "go_dry", "run_stage"}
DECISIONS = {"approve": "ok", "aonly": "aonly", "bonly": "bonly", "reject": "no", "hookok": "hookok"}
#: Approval decision -> the renders that must exist on disk before it is accepted.
APPROVAL_RENDERS = {"approve": ("a", "b"), "aonly": ("a",), "bonly": ("b",)}
#: approval_type -> the decision that restores it after "Return to review" (default: approve).
RESTORE_DECISIONS = {"approved_a_only": "aonly", "approved_b_only": "bonly"}
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
        "caption_words_override",
        "voiceover_override",
        "approved_versions",
        "creation_job",
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


def publishing_block_reason(posts):
    if any(post["status"] == "publishing" for post in posts):
        return "It is already publishing."
    if any(post["status"] == "live" for post in posts):
        return "It has already been published."
    if any(post["status"] == "failed" for post in posts):
        return "A previous publishing attempt failed."
    return "Publishing has already started."


def validate(action, item, posts, payload, path):
    if action not in ACTIONS:
        raise ControlError("Unknown action.")
    if action in GLOBAL_ACTIONS:
        if action == "go_live" and payload.get("dry_run_mode") is not None:
            raise ControlError("Going live requires a null dry_run_mode.")
        if action == "run_stage" and payload.get("stage") not in ("analytics", "cleanup"):
            raise ControlError("Choose analytics or cleanup.")
        return
    if action == "snapshot_now":
        if not item or not any(post["status"] == "live" and post["remote_id"] for post in posts):
            raise ControlError("This item has no live posts.")
        return
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
        if action in ("position", "up", "down"):
            raise ControlError(publishing_block_reason(posts))
        raise ControlError(
            "Publishing has started or failed. This item is display-only to prevent duplicate posts."
        )
    stage = item["stage"]
    if action == "make_from_discovered" and (stage != "discovered" or item.get("creation_job")):
        raise ControlError("Choose an unclaimed discovered item.")
    if action == "approve_versions":
        variants = payload.get("variants")
        if (stage not in ("rendered", "awaiting_approval") or not isinstance(variants, list)
                or not variants or any(v not in ("p1", "p2", "p3") for v in variants)
                or len(set(variants)) != len(variants)):
            raise ControlError("Choose one or more available preset versions.")
        available = {row["variant"] for row in db.renders_for_item(item["id"], path)
                     if Path(row["path"]).is_file()}
        if not set(variants) <= available:
            raise ControlError("A selected render is missing. Re-render before approval.")
        binding.assert_caption_binding(item, item.get("hook_text"), vo_path=item.get("voiceover_audio_path"))
    if action in ("edit_caption_words", "edit_voiceover"):
        if stage not in EDITABLE or not Path(item.get("cut_path") or "").is_file():
            raise ControlError("Editing requires an unposted item with a cut file.")
        if action == "edit_caption_words":
            words = payload.get("words")
            if (not isinstance(words, list) or not words or any(
                not isinstance(w, dict) or type(w.get("i")) is not int or not 0 <= w["i"] < 2000
                or not isinstance(w.get("text"), str) or not 0 < len(w["text"].strip()) <= 80
                or any(ord(c) < 32 for c in w["text"]) for w in words
            )):
                raise ControlError("Enter indexed caption words with 1–80 characters each.")
        else:
            script = payload.get("script")
            speed = payload.get("speed", 1.1)
            voice = payload.get("voice", "am_adam")
            if (not isinstance(script, str) or not 0 < len(script.strip()) <= 300
                    or type(speed) not in (int, float) or not 0.5 <= speed <= 2
                    or not isinstance(voice, str) or not re.fullmatch(r"[a-z]{2}_[a-z0-9]+", voice)):
                raise ControlError("Use a 1–300 character script, a Kokoro voice and speed between 0.5 and 2.")
    if action == "move_to_slot":
        if stage not in ("approved", "queued"):
            raise ControlError("Approve the item before moving it.")
        try:
            when = datetime.fromisoformat(payload["at"])
            if when.tzinfo is None or when <= clock.utcnow():
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ControlError("Choose a future slot with an explicit timezone.") from None
    if stage in ("posted", "measured", "cleaned"):
        raise ControlError("Remove published media on Instagram yourself.")
    if action == "delete" and stage == "rejected" and item.get("rejection_reason") == "deleted from dashboard":
        raise ControlError("This item is already deleted.")
    if action == "unapprove" and stage not in ("approved", "queued"):
        raise ControlError("Only approved or queued items can return to review.")
    if action in ("up", "down", "position") and (stage != "queued" or not posts):
        raise ControlError("Only scheduled items can move in the queue.")
    if action == "position":
        order = [row["id"] for row in queue_controls.scheduled(path)]
        if item["id"] not in order or payload.get("target") not in order or payload["target"] == item["id"]:
            raise ControlError("Choose another scheduled clip as the target.")
    if action == "restore_approval" and (stage != "awaiting_approval" or posts):
        raise ControlError("The clip is no longer available for approval.")
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
        if action in APPROVAL_RENDERS:
            if db.renders_for_item(item["id"], path):
                raise ControlError("Choose a preset version.")
            binding.assert_caption_binding(
                item, item.get("hook_text"), vo_path=item.get("voiceover_audio_path")
            )
            for variant in APPROVAL_RENDERS[action]:
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


def available_actions(item, posts, path=None):
    if not db.is_releasable(item) or not unstarted(posts):
        return ["snapshot_now"] if any(p["status"] == "live" and p["remote_id"] for p in posts) else []
    stage = item["stage"]
    if stage in ("posted", "measured", "cleaned"):
        return []
    actions = [] if item.get("rejection_reason") == "deleted from dashboard" else ["delete"]
    if stage in ("approved", "queued"):
        actions += ["unapprove", "post_now", "move_to_slot"]
    if stage == "queued" and posts:
        actions += ["up", "down"]
    if stage in EDITABLE:
        actions.append("edit_caption")
    if actions and item.get("source_url") and item.get("cut_start") is not None:
        actions.append("remake_cut")
    if actions and item.get("cut_path") and Path(item["cut_path"]).is_file():
        actions.append("remake_hook")
    if stage in ("rendered", "awaiting_approval"):
        versions = path is not None and db.renders_for_item(item["id"], path)
        actions += (["approve_versions", "edit_caption_words", "edit_voiceover"] if versions
                    else ["approve", "aonly", "bonly"])
        actions.append("reject")
    if stage == "discovered" and not item.get("creation_job"):
        actions.append("make_from_discovered")
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
    if item_id and action in GLOBAL_ACTIONS | {"pause", "resume", "make", "clip_url"}:
        raise ControlError("This control does not select an item.")
    item = db.get_item(item_id, path) if item_id else None
    posts = posting.get_posts_for_item(item_id, path) if item_id else []
    validate(action, item, posts, payload, path)
    detail = ACTIONS[action]
    if action in ("move_to_slot", "reslot"):
        payload = {**payload, "previous_slot": posting.item_slot(posts)}
    if action == "edit_hook":
        payload = {**payload, "previous": {"hook_text": item["hook_text"]}}
    if action == "edit_caption":
        payload = {**payload, "previous": {"body": item["caption_body"] or "",
                                         "hashtags": item["caption_hashtags"] or ""},
                   "restore_default": item.get("caption_body") is None}
    if action in ("position", "up", "down"):
        rows = queue_controls.scheduled(path)
        order = [row["id"] for row in rows]
        if item_id not in order:
            raise ControlError("The clip has no scheduled slot.")
        old = order.index(item_id)
        new = order.index(payload["target"]) if action == "position" else old + (-1 if action == "up" else 1)
        if not 0 <= new < len(order):
            raise ControlError("The item is already at that end of the queue.")
        payload = {"target": order[new], "undo_target": order[old + (1 if new > old else -1)],
                   "schedule": rows}
    if action == "unapprove":
        payload = {"previous_slot": posting.item_slot(posts), "stage": item["stage"],
                   "decision": RESTORE_DECISIONS.get(item["approval_type"], "approve"),
                   "versions": json.loads(item.get("approved_versions") or "[]")}
    if action == "restore_approval":
        payload = {"command_id": str(payload.get("command_id", ""))}
    if action == "delete":
        detail = ("Deletes this clip's video files for good. The item stays in history so discovery "
                  "never picks this clip again. This can't be undone.")
    if action == "post_now":
        if posting.meta_paused(path)[0] or not posting.enabled_platforms(cfg):
            raise ControlError("Posting is paused or no platforms are enabled.")
        try:
            slot = queue_controls.earliest(cfg, path, item_id)
        except ValueError as exc:
            raise ControlError(str(exc)) from None
        payload = {**payload, "slot": clock.iso(slot)}
        detail = f"Earliest allowed slot: {slot_time(slot, cfg)}"
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
        detail = f"Next available slot: {slot_time(slot, cfg)}"
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
    if action == "approve_versions":
        message = telegram_bot.approve_versions(item_id, payload["variants"], path)
        release_card(item_id, path)
        return message
    if action == "move_to_slot":
        when = clock.parse(payload["at"])
        _, zone, slots = scheduler.active_profile(cfg)
        reserved = scheduler.reserved_times(path, item_id)
        if when.astimezone(zone).time() not in slots or not scheduler.slot_available(when, reserved, cfg):
            raise ControlError("That slot is unavailable under the posting gap or daily target.")
        if not posting.enabled_platforms(cfg):
            raise ControlError("No publishing platforms are enabled.")
        posting.ensure_post_rows(item, clock.iso(when), cfg, path)
        queue_controls.set_slot(item_id, clock.iso(when), path)
        db.transition_stage(item_id, "queued", db_path=path)
        return f"Scheduled for {slot_time(when, cfg)}."
    if action == "make_from_discovered":
        db.transition_stage(item_id, "discovered", {"creation_job": command["id"]}, path)
        return "Discovered item claimed."
    if action in ("edit_caption_words", "edit_voiceover"):
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id=?", (item_id,))
        fields = {"creation_job": command["id"], "approved_versions": None, "chosen_variant": None,
                  "approval_type": None, "ab_excluded": 0}
        if action == "edit_caption_words":
            words = json.loads(item.get("caption_words_override") or "{}")
            words.update({str(w["i"]): w["text"].strip() for w in payload["words"]})
            fields["caption_words_override"] = json.dumps(words)
        else:
            voiceover = {**cfg.get("voiceover", {}), **payload}
            override = {key: voiceover[key] for key in ("script", "voice", "speed") if key in voiceover}
            fields.update(voiceover_override=json.dumps(override), voiceover_script=payload["script"].strip(),
                          voiceover_binding=None, voiceover_audio_path=None)
        release_card(item_id, path)
        db.transition_stage(item_id, "cut", fields, path)
        return "Re-render requested."
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
    if action == "restore_approval":
        with db.db(path) as conn:
            prior = conn.execute(
                "SELECT payload FROM dashboard_commands WHERE id=? AND item_id=? "
                "AND action='unapprove' AND status='applied'",
                (payload["command_id"], item_id),
            ).fetchone()
        if prior is None:
            raise ControlError("The original unapproval did not apply.")
        original = json.loads(prior["payload"])
        slot = original.get("previous_slot")
        if original.get("stage") == "queued":
            if not slot or clock.parse(slot) < clock.utcnow() - timedelta(seconds=posting.missed_slot_grace(cfg)):
                raise ControlError("The previous slot has expired.")
            if not queue_controls.allowed(clock.parse(slot), scheduler.reserved_times(path, item_id), cfg):
                raise ControlError("The previous slot is no longer available.")
            if not posting.enabled_platforms(cfg):
                raise ControlError("No publishing platforms are enabled.")
        # "a_only" is the flag unapprovals recorded before B-only existed.
        decision = original.get("decision") or ("aonly" if original.get("a_only") else "approve")
        if original.get("versions"):
            outcome, message = "approved_versions", telegram_bot.approve_versions(item_id, original["versions"], path)
        elif decision not in APPROVAL_RENDERS:
            raise ControlError("The original approval cannot be restored.")
        else:
            validate(decision, item, posts, {}, path)
            outcome, message = telegram_bot.decide(item_id, DECISIONS[decision], path)
        if not outcome or outcome == telegram_bot.NOT_APPLICABLE:
            raise ControlError(message)
        if slot and original.get("stage") == "queued":
            restored = db.get_item(item_id, path)
            posting.ensure_post_rows(restored, slot, cfg, path)
            db.transition_stage(item_id, "queued", db_path=path)
        release_card(item_id, path)
        return "Previous approval restored."
    if action in ("delete", "unapprove", "remake_cut", "remake_hook"):
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id=?", (item_id,))
        release_card(item_id, path)
        if action == "delete":
            db.mark_rejected(item_id, "deleted from dashboard", db_path=path)
            return "Deleted from dashboard."
        fields = {"chosen_variant": None, "approval_type": None, "ab_excluded": 0, "approved_versions": None}
        if action == "unapprove":
            db.transition_stage(item_id, "awaiting_approval", fields, path)
            return "Returned to review."
        stage = "cut" if action == "remake_hook" else "downloaded"
        if stage == "downloaded" and not Path(item.get("download_path") or "").is_file():
            stage = "discovered"
        fields.update(binding.SEGMENT_CONTENT_FIELDS, creation_job=command["id"], error_message=None,
                      error_stage=None, caption_body=None, caption_hashtags=None,
                      caption_words_override=None, voiceover_override=None,
                      legacy_renders=int(bool(item.get("legacy_renders") or item.get("render_a_path")
                                              or item.get("render_b_path"))))
        db.transition_stage(item_id, stage, fields, path)
        return "Remake started."
    if action == "edit_caption":
        body, tags = caption_values(payload)
        fields = {"caption_body": body, "caption_hashtags": tags}
        if payload.get("restore_default_caption") is True:
            fields = {"caption_body": None, "caption_hashtags": None}
        db.transition_stage(item_id, item["stage"], fields, path)
        with db.db(path) as conn:
            conn.execute("UPDATE posts SET caption_text=?, updated_at=? WHERE item_id=?",
                         (posting.caption_for_item({**item, **fields}, cfg), db.now(), item_id))
        return "Caption saved."
    if action in ("up", "down", "position"):
        if action != "position" and "schedule" not in payload:
            return queue_controls.move(item_id, -1 if action == "up" else 1, cfg, path)
        return queue_controls.reposition(item_id, payload["target"], payload["schedule"], cfg, path)
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
        return f"Scheduled for {slot_time(slot, cfg)}."
    if action == "edit_hook":
        with db.db(path) as conn:
            conn.execute("DELETE FROM posts WHERE item_id = ?", (item_id,))
        fields = {
            "hook_text": payload["hook_text"].strip(),
            "hook_grounded": 0,
            "hook_binding": None,
            "voiceover_binding": None,
            "voiceover_audio_path": None,
            "legacy_renders": int(bool(item.get("legacy_renders") or item.get("render_a_path")
                                       or item.get("render_b_path"))),
            "approved_versions": None,
            "caption_words_override": None,
            "voiceover_override": None,
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
        return f"Moved to {slot_time(clock.parse(slot), cfg)}."
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
                "('make','clip_url','remake_cut','remake_hook','make_from_discovered',"
                "'edit_caption_words','edit_voiceover','snapshot_now','go_live','go_dry','run_stage') "
                "ORDER BY created_at, rowid LIMIT 50"
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


def undo_for(plan, command_id):
    action, item_id, payload = plan["action"], plan["item_id"], plan["payload"]
    undo = None
    if action in APPROVAL_RENDERS or action == "approve_versions":
        undo = ("unapprove", {})
    elif action == "unapprove":
        undo = ("restore_approval", {"command_id": command_id})
    elif action in ("up", "down", "position") and payload.get("undo_target"):
        undo = ("position", {"target": payload["undo_target"]})
    elif action in ("move_to_slot", "reslot") and payload.get("previous_slot"):
        undo = ("move_to_slot", {"at": payload["previous_slot"]})
    elif action in ("edit_hook", "edit_caption") and payload.get("previous") is not None:
        undo = (action, {**payload["previous"], **(
            {"restore_default_caption": True} if action == "edit_caption" and payload.get("restore_default") else {}
        )})
    return {"action": undo[0], "item_id": item_id, "payload": undo[1]} if undo else None
