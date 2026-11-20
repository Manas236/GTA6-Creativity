"""Posting progress: every step of a publish, recorded as it happens, for the dashboard panel.

A Post now and a scheduled post record the same events into `post_events` (created by
`db.init_db`). A run starts with a `requested` event and ends with an `outcome` event
(`live`, `dry_run` or `failed`). `panel()` folds the latest run together with the real state
around it (the dashboard_commands row, the runner lock and the posts rows) into the eight
stages the dashboard shows. Nothing here is driven by a timer.

Recording never raises: a progress row must never be the reason a post fails.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable

from . import clock, db

log = logging.getLogger(__name__)

STEPS = (
    ("requested", "Request recorded"),
    ("waiting", "Waiting for runner"),
    ("checks", "Checks passed"),
    ("container", "Creating Instagram container"),
    ("upload", "Uploading video"),
    ("processing", "Meta processing the video"),
    ("publishing", "Publishing"),
    ("live", "Live"),
)
STEP_KEYS = [key for key, _ in STEPS]
OUTCOME = "outcome"
OUTCOMES = ("live", "dry_run", "failed")
KIND_LABELS = {
    "post_now": "Post now",
    "scheduled": "Scheduled post",
    "retry": "Retry",
    "settle": "Crash recovery check",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS post_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    post_id INTEGER,
    command_id TEXT,
    step TEXT NOT NULL,
    state TEXT NOT NULL,
    message TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_post_events_item ON post_events(item_id, id);
"""


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def record(
    item_id: str | None,
    step: str,
    state: str,
    message: str = "",
    *,
    post_id: int | None = None,
    command_id: str | None = None,
    db_path: Any = None,
    dedupe: bool = False,
    **detail: Any,
) -> int | None:
    """Append one event. `dedupe` skips it when the item's latest event says the same thing
    (a held post is checked every few seconds and must not flood the table)."""
    if not item_id:
        return None
    try:
        with db.db(db_path) as conn:
            if dedupe:
                last = conn.execute(
                    "SELECT step, state, message FROM post_events WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                    (item_id,),
                ).fetchone()
                if last and (last["step"], last["state"], last["message"] or "") == (step, state, message or ""):
                    return None
            cur = conn.execute(
                "INSERT INTO post_events (item_id, post_id, command_id, step, state, message, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, post_id, command_id, step, state, str(message or "")[:1000],
                 json.dumps(detail, default=str)[:4000] if detail else None, db.now()),
            )
            return cur.lastrowid
    except (sqlite3.Error, TypeError, ValueError) as e:
        log.warning("posting progress not recorded for %s (%s %s): %s", item_id, step, state, e)
        return None


def latest_run(item_id: str, db_path: Any = None) -> list[dict[str, Any]]:
    """The events of the item's latest run, oldest first ([] when it has none)."""
    try:
        with db.db(db_path) as conn:
            start = conn.execute(
                "SELECT MAX(id) FROM post_events WHERE item_id = ? AND step = 'requested'", (item_id,)
            ).fetchone()[0]
            if start is None:
                return []
            rows = conn.execute(
                "SELECT * FROM post_events WHERE item_id = ? AND id >= ? ORDER BY id", (item_id, start)
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def run_is_open(item_id: str, db_path: Any = None) -> bool:
    return latest_state(item_id, db_path) == "open"


def latest_state(item_id: str, db_path: Any = None) -> str | None:
    """None (no run yet), 'open', or the latest run's outcome ('live', 'dry_run', 'failed')."""
    events = latest_run(item_id, db_path)
    if not events:
        return None
    outcomes = [e["state"] for e in events if e["step"] == OUTCOME]
    return outcomes[-1] if outcomes else "open"


def open_run(item_id: str, kind: str, message: str, *, command_id: str | None = None,
             db_path: Any = None, **detail: Any) -> int | None:
    return record(item_id, "requested", "done", message, command_id=command_id, db_path=db_path,
                  kind=kind, **detail)


def ensure_run(item_id: str, kind: str, message: str, db_path: Any = None, **detail: Any) -> bool:
    """Open a run unless one is already open. Returns True when it opened one."""
    if run_is_open(item_id, db_path):
        return False
    open_run(item_id, kind, message, db_path=db_path, **detail)
    return True


def finish(item_id: str, outcome: str, message: str = "", *, post_id: int | None = None,
           db_path: Any = None, **detail: Any) -> None:
    record(item_id, OUTCOME, outcome, message, post_id=post_id, db_path=db_path, **detail)


def fail(item_id: str, step: str, message: str, *, error: dict | None = None,
         retry: dict | None = None, post_id: int | None = None, db_path: Any = None,
         **detail: Any) -> None:
    """A step failed: mark it, and end the run as failed with the error and the retry plan."""
    extra = {k: v for k, v in (("error", error), ("retry", retry)) if v}
    record(item_id, step, "failed", message, post_id=post_id, db_path=db_path, **extra, **detail)
    finish(item_id, "failed", message, post_id=post_id, db_path=db_path, failed_step=step, **extra, **detail)


# ---------------------------------------------------------------------------
# Error formatting
# ---------------------------------------------------------------------------

def meta_error_from(error: Any, http_status: int | None = None) -> dict[str, Any] | None:
    """Meta's error object as the fields the panel shows."""
    if not isinstance(error, dict) or not error:
        return None
    out = {
        "http_status": http_status,
        "code": error.get("code"),
        "subcode": error.get("error_subcode"),
        "type": error.get("type"),
        "message": error.get("message"),
        "user_message": error.get("error_user_msg") or error.get("error_user_title"),
        "fbtrace_id": error.get("fbtrace_id"),
    }
    return {k: v for k, v in out.items() if v not in (None, "")}


def error_text(error: dict[str, Any] | None) -> str:
    if not error:
        return ""
    if error.get("exception"):
        return str(error["exception"])
    parts = []
    if error.get("code") is not None:
        parts.append(f"Meta error code {error['code']}")
    if error.get("subcode") is not None:
        parts.append(f"subcode {error['subcode']}")
    head = ", ".join(parts)
    if error.get("type"):
        head += f" ({error['type']})" if head else str(error["type"])
    if error.get("http_status"):
        head += f", HTTP {error['http_status']}" if head else f"HTTP {error['http_status']}"
    body = error.get("message") or ""
    if error.get("user_message") and error["user_message"] != body:
        body = f"{body} ({error['user_message']})" if body else error["user_message"]
    text = f"{head}: {body}" if head and body else head or body
    if error.get("fbtrace_id"):
        text += f" [fbtrace_id {error['fbtrace_id']}]"
    return text


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------

def _blank_stages() -> dict[str, dict[str, Any]]:
    return {key: {"key": key, "label": label, "state": "waiting", "started_at": None,
                  "finished_at": None, "message": "", "detail": {}} for key, label in STEPS}


def _split_runs(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    runs: list[list[dict[str, Any]]] = []
    for event in events:
        if event["step"] == "requested" or not runs:
            runs.append([event])
        else:
            runs[-1].append(event)
    return runs


def _decode(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def fold(run: list[dict[str, Any]]) -> dict[str, Any]:
    """The stages of one run, from its events alone."""
    stages = _blank_stages()
    out: dict[str, Any] = {"outcome": None, "failed_step": None, "error": None, "retry": None,
                           "dry_run": False, "permalink": None, "post_id": None, "command_id": None,
                           "kind": None, "started_at": run[0]["created_at"] if run else None,
                           "finished_at": None, "media_id": None}
    for event in run:
        detail = _decode(event.get("detail"))
        out["post_id"] = event.get("post_id") or out["post_id"]
        out["command_id"] = event.get("command_id") or out["command_id"]
        if detail.get("dry_run"):
            out["dry_run"] = True
        if event["step"] == "requested":
            out["kind"] = detail.get("kind") or out["kind"]
        if event["step"] == OUTCOME:
            out["outcome"] = event["state"]
            out["finished_at"] = event["created_at"]
            if event["state"] == "failed":
                out["error"] = out["error"] or detail.get("error") or {"message": event.get("message")}
                out["retry"] = detail.get("retry") or out["retry"]
                out["failed_step"] = out["failed_step"] or detail.get("failed_step")
            out["permalink"] = detail.get("permalink") or out["permalink"]
            continue
        stage = stages.get(event["step"])
        if stage is None:
            continue
        index = STEP_KEYS.index(event["step"])
        state = event["state"]
        if state == "running" and stage["state"] in ("done", "failed", "skipped"):
            # Two processes can record the same stage (the dashboard and the runner's posting
            # check); a late "running" never reopens a stage that already finished.
            continue
        if state in ("running", "done", "failed", "skipped"):
            # A later stage starting means every earlier one still running has finished.
            for key in STEP_KEYS[:index]:
                earlier = stages[key]
                if earlier["state"] == "running" or (earlier["state"] == "waiting" and earlier["started_at"]):
                    earlier["state"] = "done"
                    earlier["finished_at"] = earlier["finished_at"] or event["created_at"]
        if state == "running":
            stage["started_at"] = stage["started_at"] or event["created_at"]
            stage["finished_at"] = None
            stage["state"] = "running"
        elif state in ("done", "failed", "skipped"):
            stage["started_at"] = stage["started_at"] or event["created_at"]
            stage["finished_at"] = event["created_at"]
            stage["state"] = state
            if state == "failed":
                out["failed_step"] = event["step"]
                out["error"] = detail.get("error") or {"message": event.get("message")}
                out["retry"] = detail.get("retry") or out["retry"]
        elif state == "waiting":
            stage["started_at"] = stage["started_at"] or event["created_at"]
            if stage["state"] not in ("running", "done"):
                stage["state"] = "waiting"
            stage["held"] = True
        if event.get("message"):
            stage["message"] = event["message"]
        stage["detail"].update({k: v for k, v in detail.items() if k not in ("error", "retry", "kind")})
        stage["updated_at"] = event["created_at"]
        if event["step"] == "live":
            out["permalink"] = detail.get("permalink") or out["permalink"]
            out["media_id"] = detail.get("media_id") or out["media_id"]
    if out["outcome"] == "live":
        live = stages["live"]
        if live["state"] != "done":
            live.update(state="done", finished_at=out["finished_at"], started_at=live["started_at"] or out["finished_at"])
    out["stages"] = [stages[key] for key in STEP_KEYS]
    return out


def _from_posts_row(post: dict[str, Any]) -> list[dict[str, Any]]:
    """Events reconstructed from a posts row that predates progress tracking."""
    events: list[dict[str, Any]] = []

    def add(step, state, message="", when=None, **detail):
        events.append({"id": len(events), "item_id": post["item_id"], "post_id": post["id"],
                       "command_id": None, "step": step, "state": state, "message": message,
                       "detail": json.dumps(detail) if detail else None,
                       "created_at": when or post.get("updated_at") or post.get("created_at")})

    add("requested", "done", f"Queued for {post.get('slot_datetime')}", post.get("created_at"),
        kind="scheduled", reconstructed=True)
    status = post.get("status")
    if status in ("pending",) and not post.get("remote_container_id"):
        return events
    started = post.get("publish_started_at")
    if post.get("remote_container_id"):
        add("container", "done", f"Container {post['remote_container_id']}", started)
    if status == "dry_run":
        add(OUTCOME, "dry_run", post.get("error_message") or "Dry run", post.get("updated_at"), dry_run=True)
        return events
    if started:
        add("publishing", "running", "Publish call issued", started)
    if status == "live":
        add("live", "done", "Live on Instagram" if post["platform"] == "instagram" else "Live",
            post.get("posted_at"), permalink=post.get("remote_url"), media_id=post.get("remote_id"))
        add(OUTCOME, "live", "", post.get("posted_at"), permalink=post.get("remote_url"))
    elif status == "failed":
        add(OUTCOME, "failed", post.get("error_message") or "Failed", post.get("updated_at"),
            error={"exception": post.get("error_message") or "Failed"})
    return events


def _fmt_local(value: str | None, tz: Any) -> str:
    if not value:
        return "-"
    try:
        moment = clock.parse(value)
    except (TypeError, ValueError):
        return str(value)
    return moment.astimezone(tz).strftime("%d %b %H:%M:%S") if tz else clock.iso(moment)


def panel(
    item_id: str,
    db_path: Any = None,
    *,
    lock: dict[str, Any] | None = None,
    runner_stage: str | None = None,
    dry_run_now: bool | None = None,
    tz: Any = None,
    platforms: list[str] | None = None,
    now: datetime | None = None,
    mask: Callable[[str], str] | None = None,
    run_index: int | None = None,
) -> dict[str, Any]:
    """Everything the posting panel shows for one item, as JSON-ready data."""
    now = now or clock.utcnow()
    mask = mask or (lambda text: text)
    with db.db(db_path) as conn:
        item = conn.execute("SELECT id, original_title, stage FROM items WHERE id = ?", (item_id,)).fetchone()
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM post_events WHERE item_id = ? ORDER BY id", (item_id,))]
        posts = [dict(r) for r in conn.execute(
            "SELECT * FROM posts WHERE item_id = ? ORDER BY platform", (item_id,))]
    if item is None:
        return {}
    wanted = [p for p in posts if not platforms or p["platform"] in platforms]
    primary = next((p for p in wanted if p["platform"] == "instagram"), wanted[0] if wanted else None)
    runs = _split_runs(events)
    reconstructed = False
    if not runs and primary is not None:
        runs = [_from_posts_row(primary)]
        reconstructed = True
    chosen = runs[run_index] if run_index is not None and -len(runs) <= run_index < len(runs) else (runs[-1] if runs else [])
    data = fold(chosen) if chosen else {**fold([]), "stages": fold([])["stages"]}
    stages = {stage["key"]: stage for stage in data["stages"]}
    notices: list[str] = []

    command = None
    if data.get("command_id"):
        with db.db(db_path) as conn:
            row = conn.execute("SELECT * FROM dashboard_commands WHERE id = ?", (data["command_id"],)).fetchone() \
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_commands'").fetchone() else None
        command = dict(row) if row else None

    lock_alive = bool(lock and lock.get("status") == "alive")
    kind = (lock or {}).get("kind")
    is_latest = not runs or chosen is runs[-1]
    if command and command["status"] == "pending" and data["outcome"] is None and is_latest:
        waited = max(0, int((now - clock.parse(command["created_at"])).total_seconds()))
        waiting = stages["waiting"]
        waiting["state"] = "running"
        waiting["started_at"] = waiting["started_at"] or command["created_at"]
        if lock_alive and kind == "loop":
            waiting["message"] = f"Runner busy in {runner_stage or 'its cycle'}. It picks this up between checks, within about a minute."
        elif lock_alive and kind == "make":
            waiting["message"] = f"A Make job holds the runner lock (busy in {runner_stage or 'make'}). It picks this up within about a minute."
        elif lock_alive and kind == "post":
            waiting["message"] = "The one-off posting worker holds the runner lock and is about to apply it."
        elif lock_alive:
            waiting["message"] = f"The runner lock is held ({kind or 'unknown'}). Waiting for it to be released."
        else:
            waiting["message"] = waiting["message"] or "No runner is running."
        waiting["detail"]["waited_s"] = waited
        waiting["detail"]["runner"] = {"alive": lock_alive, "kind": kind, "stage": runner_stage}
    if command and command["status"] in ("refused", "failed") and data["outcome"] is None and is_latest:
        checks = stages["checks"]
        checks.update(state="failed", message=command.get("result") or command["status"],
                      started_at=checks["started_at"] or command.get("completed_at"),
                      finished_at=command.get("completed_at"))
        data.update(outcome="failed", failed_step="checks",
                    error={"exception": command.get("result") or command["status"]},
                    retry={"will_retry": False, "text": "No. Open Post now again to get a new slot."})

    slot = min((p["slot_datetime"] for p in wanted if p.get("slot_datetime")), default=None)
    open_rows = [p for p in wanted if p["status"] in ("pending", "uploading", "container_ready", "publishing", "failed")]
    if (data["outcome"] is None and is_latest and slot and clock.parse(slot) > now
            and stages["checks"]["state"] == "waiting" and stages["container"]["state"] == "waiting"
            and (not command or command["status"] == "applied")):
        stages["checks"]["message"] = (
            f"Earliest allowed slot is {_fmt_local(slot, tz)} (minimum gap and daily cap). It publishes then.")
        stages["checks"]["started_at"] = stages["checks"]["started_at"] or (command or {}).get("completed_at")
        stages["checks"]["detail"]["slot"] = slot
        if stages["waiting"]["state"] == "running":
            stages["waiting"]["state"] = "done"
            stages["waiting"]["finished_at"] = (command or {}).get("completed_at") or stages["waiting"]["started_at"]

    if primary is not None and data["outcome"] is None and is_latest:
        if primary["status"] == "live":
            data["outcome"] = "live"
            data["permalink"] = data["permalink"] or primary.get("remote_url")
            stages["live"].update(state="done", finished_at=primary.get("posted_at"))
        elif primary["status"] == "dry_run" and not open_rows:
            data["outcome"] = "dry_run"

    if data["outcome"] == "dry_run":
        data["dry_run"] = True
        for key in ("container", "upload", "processing", "publishing", "live"):
            if stages[key]["state"] in ("waiting", "running"):
                stages[key]["state"] = "skipped"
                stages[key]["message"] = stages[key]["message"] or "Not sent (dry run)."
    if data["outcome"] is None and dry_run_now:
        data["dry_run"] = True

    active = bool(chosen) and data["outcome"] is None and is_latest
    if active and not lock_alive:
        notices.append("No runner or posting worker is running right now, so nothing is moving. "
                       "Start the runner, or press Post now again, to continue.")
    if reconstructed:
        notices.append("Reconstructed from the posts row: this post was published before progress tracking existed.")

    for stage in data["stages"]:
        stage["message"] = mask(stage["message"] or "")
    if data.get("error"):
        data["error"] = {k: (mask(str(v)) if isinstance(v, str) else v) for k, v in data["error"].items()}
    error_line = mask(error_text(data.get("error")))

    summary_runs = []
    for index, run in enumerate(runs):
        folded = fold(run)
        summary_runs.append({"index": index, "started_at": folded["started_at"], "kind": folded["kind"],
                             "outcome": folded["outcome"], "dry_run": folded["dry_run"]})

    result = {
        "item_id": item_id,
        "title": item["original_title"],
        "item_stage": item["stage"],
        "post_id": data.get("post_id") or (primary or {}).get("id"),
        "platform": (primary or {}).get("platform", "instagram"),
        "post_status": (primary or {}).get("status"),
        "attempts": (primary or {}).get("attempt_count"),
        "slot": slot,
        "kind": data.get("kind"),
        "kind_label": KIND_LABELS.get(data.get("kind") or "", "Post"),
        "command": {k: command.get(k) for k in ("id", "status", "result", "created_at", "completed_at")} if command else None,
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "outcome": data["outcome"],
        "active": active,
        "dry_run": bool(data["dry_run"]),
        "failed_step": data["failed_step"],
        "error": data.get("error"),
        "error_text": error_line,
        "retry": data.get("retry"),
        "permalink": data.get("permalink") if str(data.get("permalink") or "").startswith("http") else None,
        "stages": data["stages"],
        "notices": notices,
        "runs": summary_runs,
        "run_index": runs.index(chosen) if chosen in runs else None,
        "runner": {"alive": lock_alive, "kind": kind, "stage": runner_stage},
        "at": clock.iso(now),
    }
    result["copy_text"] = copy_text(result, tz)
    return result


def copy_text(panel_data: dict[str, Any], tz: Any = None) -> str:
    """Plain text of a panel, for pasting into a chat with Claude."""
    lines = [
        f"Posting progress for {panel_data['item_id']} ({panel_data.get('title') or 'untitled'})",
        f"Post id: {panel_data.get('post_id') or '-'} ({panel_data.get('platform') or 'instagram'}), "
        f"status {panel_data.get('post_status') or '-'}, attempts {panel_data.get('attempts') or 0}",
        f"Run: {panel_data.get('kind_label')} started {panel_data.get('started_at') or '-'} UTC "
        f"({_fmt_local(panel_data.get('started_at'), tz)} local)",
        f"Mode: {'DRY RUN, nothing reaches Instagram' if panel_data.get('dry_run') else 'LIVE'}",
    ]
    if panel_data.get("command"):
        command = panel_data["command"]
        lines.append(f"Command: {command['id']} {command['status']} ({command.get('result') or ''})")
    lines.append("Stages:")
    for number, stage in enumerate(panel_data["stages"], start=1):
        times = f"{stage.get('started_at') or '-'} -> {stage.get('finished_at') or '-'}"
        message = f" | {stage['message']}" if stage.get("message") else ""
        lines.append(f"  {number}. {stage['label']}: {stage['state']} ({times}){message}")
    if panel_data.get("failed_step"):
        label = dict(STEPS).get(panel_data["failed_step"], panel_data["failed_step"])
        lines.append(f"Failed at: {label}")
    if panel_data.get("error_text"):
        lines.append(f"Error: {panel_data['error_text']}")
    if panel_data.get("retry"):
        lines.append(f"Retry: {panel_data['retry'].get('text')}")
    if panel_data.get("permalink"):
        lines.append(f"Permalink: {panel_data['permalink']}")
    for notice in panel_data.get("notices") or []:
        lines.append(f"Note: {notice}")
    lines.append(f"Generated {panel_data.get('at')} UTC")
    return "\n".join(lines)


def retry_plan(post: dict[str, Any], max_attempts: int, delay_s: float, terminal: bool = False,
               now: datetime | None = None) -> dict[str, Any]:
    """Will this failed row be tried again, and when."""
    attempts = int(post.get("attempt_count") or 0)
    if terminal or post.get("no_retry"):
        return {"will_retry": False, "text": "No. This row is marked no-retry, so it is never sent again."}
    if attempts >= max_attempts:
        return {"will_retry": False, "text": f"No. All {max_attempts} attempts are used up."}
    when = (now or clock.utcnow()) + timedelta(seconds=delay_s)
    return {"will_retry": True, "retry_at": clock.iso(when),
            "text": f"Yes. The posting check tries again from {clock.iso(when)} UTC "
                    f"(attempt {attempts + 1} of {max_attempts})."}
