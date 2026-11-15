"""Durable creation mailbox. Only an orchestrator holding the lease consumes it."""
import json
import logging
from pathlib import Path

from pipeline import binding, creation, db, discover

from . import commands
from .store import transaction

log = logging.getLogger(__name__)


def run(command_id, cfg, path, stage_enter=None):
    if stage_enter is None:
        from pipeline import runner

        stage_enter = lambda stage: runner.enter_stage(path, stage)
    with transaction(path) as conn:
        record = conn.execute(
            "SELECT * FROM dashboard_commands WHERE id=? AND status IN ('pending','running')",
            (command_id,),
        ).fetchone()
        if not record or record["action"] not in commands.LONG:
            return
        row = dict(record)
        conn.execute("UPDATE dashboard_commands SET status='running' WHERE id=?", (command_id,))
    payload = json.loads(row["payload"])
    try:
        with db.db(path) as conn:
            items = [r[0] for r in conn.execute("SELECT id FROM items WHERE dashboard_job=?", (command_id,))]
        if not items:
            if not stage_enter("discover"):
                return
            if row["action"] == "make":
                items = [r["id"] for r in discover.run_discovery(
                    cfg, path, limit=payload["count"], job_id=command_id)]
            elif row["action"] == "clip_url":
                with transaction(path):
                    items = [commands.check(creation.insert_manual, payload, command_id, path)]
            else:
                items = [prepare_remake(row, path)]
        for item_id in items:
            if not creation.process_item(item_id, cfg, path, stage_enter):
                return
        states = [db.get_item(item_id, path)["stage"] for item_id in items]
        ready = sum(stage in ("awaiting_approval", "rendered") for stage in states)
        status = "applied" if ready or not items else "failed"
        message = (f"{ready} of {len(items)} ready for approval. See item results below."
                   if items else "Discovery returned no new clips.")
    except (commands.ControlError, binding.CaptionMismatchError) as exc:
        status, message = "refused", str(exc)
    except Exception as exc:
        log.exception("Creation job %s failed", command_id)
        status, message = "failed", f"{type(exc).__name__}: {exc}"
    with transaction(path) as conn:
        conn.execute("UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
                     (status, message[:500], db.now(), command_id))


def prepare_remake(row, path):
    item_id = row["item_id"]
    with transaction(path) as conn:
        item = db.get_item(item_id, path)
        posts = commands.posting.get_posts_for_item(item_id, path)
        commands.validate(row["action"], item, posts, {}, path)
        if commands.fingerprint(item, posts) != row["expected"]:
            raise commands.ControlError("The item changed before the remake started.")
        fields = {**binding.SEGMENT_CONTENT_FIELDS, "dashboard_job": row["id"],
                  "error_stage": None, "error_message": None, "rejection_reason": None,
                  "dashboard_step": None, "approval_type": None, "chosen_variant": None, "ab_excluded": 0}
        if row["action"] == "remake_hook":
            # Keep the selected footage and its verified transcript.
            stage = "cut"
        else:
            fields["cut_exclude"] = json.dumps([item["cut_start"], item["cut_end"]])
            fields.update(caption_body_override=None, caption_tags_override=None)
            stage = "downloaded" if Path(item.get("download_path") or "").is_file() else "discovered"
        conn.execute("DELETE FROM posts WHERE item_id=?", (item_id,))
        db.transition_stage(item_id, stage, fields, path)
        commands.release_card(item_id, path)
    return item_id
