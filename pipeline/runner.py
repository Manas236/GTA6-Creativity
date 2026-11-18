"""Main pipeline orchestrator and CLI entry points with heartbeat-based locking."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
import uuid
from typing import Any

from . import analytics, cleanup, config, cut, db, discover, download, healthcheck, hooks, posting, render, scheduler, telegram_bot, voiceover

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gta6.runner")

LOCK_KEY = "runner:lock"


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not evidence of termination.
        try:
            return kernel32.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0 means exited.
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


STOP_KEY = "runner:stop"
_owner = threading.local()


def active_lock(db_path=None, stale_timeout_s=120.0):
    """Return the current fresh lock, including its process identity."""
    try:
        data = json.loads(db.kv_get(LOCK_KEY, db_path=db_path) or "{}")
        if time.time() - float(data["heartbeat"]) > stale_timeout_s:
            return None
        if data["host"] == socket.gethostname() and not is_pid_alive(int(data["pid"])):
            return None
        return data
    except (KeyError, TypeError, ValueError):
        return None


def _heartbeat_worker(db_path, interval, token, stop):
    while not stop.wait(interval):
        try:
            with db.db(db_path) as conn:
                conn.execute(
                    "UPDATE kv SET value=json_set(value, '$.heartbeat', ?) "
                    "WHERE key=? AND json_valid(value) AND json_extract(value, '$.token')=?",
                    (time.time(), LOCK_KEY, token),
                )
        except Exception:
            log.exception("Heartbeat update failed")


def acquire_lock(db_path=None, stale_timeout_s=120.0, heartbeat_interval_s=30.0, kind="stage"):
    """Claim the lock atomically, with conditional heartbeat and release."""
    token, stop = uuid.uuid4().hex, threading.Event()
    with db.db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with db.connection_scope(conn, db_path):
            if active_lock(db_path, stale_timeout_s):
                return False
            now = time.time()
            db.kv_set(LOCK_KEY, json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(), "heartbeat": now,
                "started_at": now, "token": token, "kind": kind,
            }), db_path=db_path)
            set_stage("starting", db_path)
    _owner.token, _owner.stop = token, stop
    threading.Thread(target=_heartbeat_worker, args=(db_path, heartbeat_interval_s, token, stop),
                     daemon=True).start()
    return True


def release_lock(db_path=None):
    token = getattr(_owner, "token", None)
    if not token:
        return
    _owner.stop.set()
    with db.db(db_path) as conn:
        conn.execute(
            "UPDATE kv SET value='' WHERE key=? AND json_valid(value) AND json_extract(value, '$.token')=?",
            (LOCK_KEY, token),
        )
    _owner.token = None


def set_stage(name, db_path=None):
    db.kv_set("runner:stage", name, db_path=db_path)


def stopping(db_path=None):
    token = getattr(_owner, "token", None)
    if not token:
        return False
    raw = db.kv_get(STOP_KEY, db_path=db_path)
    return bool(raw and json.loads(raw).get("token") == token)


def print_status(db_path: Any = None) -> None:
    db.init_db(db_path)
    with db.db(db_path) as conn:
        counts = conn.execute("SELECT stage, COUNT(*) as cnt FROM items GROUP BY stage").fetchall()
        recent = conn.execute(
            """
            SELECT id, source, author_name, original_title, view_count, raw_velocity, percentile_rank, discovery_score, stage 
            FROM items ORDER BY discovery_score DESC LIMIT 15
            """
        ).fetchall()

    print("\n=== GTA 6 Pipeline Stage Counts ===")
    if not counts:
        print("No items in database.")
    else:
        for r in counts:
            print(f"  {r['stage']:<20}: {r['cnt']}")

    print("\n=== Cut: no_clean_ending ===")
    for label, hours in (("last 24 h", 24), ("all time", None)):
        st = db.cut_outcome_stats(db_path, hours=hours)
        print(f"  {label:<10}: {st['no_clean_ending']} of {st['attempts']} cut attempts ({st['rate']:.0%})")

    print("\n=== Top Discovered Items (by score) ===")
    print(f"  {'Stage':<10} | {'Score':>7} | {'Views':>9} | {'Velocity':>8} | {'P-Rank':>6} | {'Author':<18} | Title")
    print("  " + "-" * 95)
    for r in recent:
        title = (r["original_title"] or "")[:40]
        vel = r["raw_velocity"] or 0.0
        views = r["view_count"] or 0
        prank = r["percentile_rank"] or 0.0
        print(f"  {r['stage']:<10} | {r['discovery_score']:>7.3f} | {views:>9d} | {vel:>8.1f} | {prank:>6.2f} | {r['author_name'][:18]:<18} | {title}")
    print()


# ---------------------------------------------------------------------------
# Stages: every one isolated, so one failure never stops the others
# ---------------------------------------------------------------------------

def _stage_settle(cfg: dict[str, Any], db_path: Any) -> Any:
    # CP3 crash recovery comes first: rows left in 'publishing' by a crash are resolved against
    # the remote before anything new is published, and never published a second time.
    if posting.meta_paused(db_path)[0]:
        return []
    return posting.settle_publishing_rows(cfg=cfg, db_path=db_path)


def _stage_discover(cfg: dict[str, Any], db_path: Any) -> Any:
    from .creation import run_jobs
    with db.db(db_path) as conn:
        mailbox = conn.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_commands'").fetchone()
    if mailbox:
        run_jobs(cfg, db_path)
    if stopping(db_path) or db.kv_get("dashboard:demo", db_path=db_path):
        return {"candidates": 0}
    return {"candidates": len(discover.run_discovery(cfg=cfg, db_path=db_path))}


def _stage_download(cfg: dict[str, Any], db_path: Any) -> Any:
    return {"downloaded": len(download.run_download_pipeline(cfg=cfg, db_path=db_path))}


def _stage_cut(cfg: dict[str, Any], db_path: Any) -> Any:
    return {"cut": len(cut.run_cut_pipeline(cfg=cfg, db_path=db_path))}


def _stage_render(cfg: dict[str, Any], db_path: Any) -> Any:
    """Render every item waiting in 'cut' (not just the ones cut this cycle, so an item whose
    render was interrupted is picked up again), up to the room left in the ready buffer.
    preview_only items are skipped; one failing item does not stop the next."""
    target = int((cfg.get("buffer") or {}).get("target_ready_items", 6))
    room = max(0, target - db.get_ready_buffer_count(db_path=db_path))
    rendered, failed = [], []
    for item in db.get_items_by_stage("cut", db_path=db_path, releasable_only=True)[:room]:
        try:
            ok, msg, _stats = render.run_render_item(item["id"], cfg=cfg, db_path=db_path)
        except Exception as e:  # noqa: BLE001 - isolate the item
            log.exception("Render crashed for %s", item["id"])
            db.mark_failed(item["id"], "render", f"render crashed: {e}", db_path=db_path)
            ok, msg = False, str(e)
        (rendered if ok else failed).append(item["id"])
        if not ok:
            log.warning("Render failed for %s: %s", item["id"], msg)
    return {"rendered": rendered, "failed": failed}


def _stage_approve(cfg: dict[str, Any], db_path: Any) -> Any:
    # The Telegram long poll runs here, inside the runner's loop: one card, one poll round.
    return telegram_bot.run_approval_stage(cfg=cfg, db_path=db_path)


def _stage_schedule(cfg: dict[str, Any], db_path: Any) -> Any:
    if not posting.enabled_platforms(cfg):
        return {"skipped": "No posting platforms enabled"}
    return scheduler.run_scheduler_stage(cfg=cfg, db_path=db_path)


def _stage_post(cfg: dict[str, Any], db_path: Any) -> Any:
    return posting.run_posting_stage(cfg=cfg, db_path=db_path)


def _stage_analytics(cfg: dict[str, Any], db_path: Any) -> Any:
    return analytics.run_analytics_stage(cfg=cfg, db_path=db_path)


def _stage_cleanup(cfg: dict[str, Any], db_path: Any) -> Any:
    return cleanup.run_cleanup_stage(cfg=cfg, db_path=db_path)


#: Healthchecks.io transport; None = requests. Tests and scripts/cp4_dry_run.py replace it.
healthcheck_transport: Any = None

#: The cycle, in order. Each entry runs in its own try/except (see run_stages).
STAGES: list[tuple[str, Any]] = [
    ("settle", _stage_settle),
    ("discover", _stage_discover),
    ("download", _stage_download),
    ("cut", _stage_cut),
    ("render", _stage_render),
    ("approve", _stage_approve),
    ("schedule", _stage_schedule),
    ("post", _stage_post),
    ("analytics", _stage_analytics),
    ("cleanup", _stage_cleanup),
]


#: Single-stage CLI commands that just run one stage function under the lock.
EXTRA_COMMANDS: dict[str, Any] = {"analytics": _stage_analytics, "cleanup": _stage_cleanup}


def run_stages(
    cfg: dict[str, Any],
    db_path: Any = None,
    stages: list[tuple[str, Any]] | None = None,
    graceful: bool = False,
    reload_before_post: bool = False,
) -> dict[str, Any]:
    """Run every stage in order WITHOUT taking the lock (the caller holds it).

    A stage that raises is logged, alerted once an hour, recorded under `errors`, and the next
    stage still runs: a YouTube outage must not stop posting, a Meta outage must not stop cleanup.
    """
    t0 = time.time()
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        from dashboard.commands import runner_boundary
    except ModuleNotFoundError as exc:
        if exc.name not in ("dashboard", "dashboard.commands"):
            log.exception("Dashboard could not load; skipping controls this cycle")
        runner_boundary = None
    except Exception:
        log.exception("Dashboard could not load; skipping controls this cycle")
        runner_boundary = None

    if stages is None and db.kv_get("dashboard:demo", db_path=db_path):
        stages = [("discover", _stage_discover)]
    for name, fn in stages or STAGES:
        if graceful:
            with db.db(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                with db.connection_scope(conn, db_path):
                    if stopping(db_path):
                        break
                    db.kv_set("runner:stage", name, db_path=db_path)
        t_stage = time.time()
        if runner_boundary is not None:
            try:
                runner_boundary(cfg, db_path, name)
            except Exception:
                log.exception("Dashboard boundary failed before %s; running the stage", name)
        try:
            if name == "post" and reload_before_post:
                cfg = config.get_config(reload=True)
            if name == "analytics":
                run_dashboard_operations(cfg, db_path)
            results[name] = fn(cfg, db_path)
            log.info("Stage %-9s ok in %.1fs: %s", name, time.time() - t_stage, str(results[name])[:300])
        except Exception as e:  # noqa: BLE001 - this is the isolation boundary
            log.exception("Stage %s failed; carrying on with the next stage", name)
            errors[name] = f"{type(e).__name__}: {e}"[:500]
            telegram_bot.send_alert(
                f"Pipeline stage '{name}' failed: {errors[name][:300]}",
                key=f"stage_failed:{name}", cfg=cfg, db_path=db_path,
            )
    return {
        "status": "success" if not errors else "partial",
        "stages": results,
        "errors": errors,
        "duration_s": time.time() - t0,
    }


def _lock_settings(cfg: dict[str, Any]) -> tuple[float, float]:
    orch_cfg = cfg.get("orchestrator", {}) or {}
    return float(orch_cfg.get("heartbeat_stale_seconds", 120.0)), float(orch_cfg.get("heartbeat_interval_seconds", 30.0))


def run_single_cycle(cfg: dict[str, Any] | None = None, db_path: Any = None) -> dict[str, Any]:
    """One complete cycle under the runner lock: every stage in STAGES, each isolated."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)
    stale_timeout, hb_interval = _lock_settings(cfg)
    if not acquire_lock(db_path=db_path, stale_timeout_s=stale_timeout, heartbeat_interval_s=hb_interval):
        log.warning("Could not acquire lock; another runner is active.")
        return {"status": "locked"}
    try:
        log.info("Starting pipeline cycle...")
        result = run_stages(cfg, db_path)
        log.info("Cycle finished in %.2fs (%s)", result["duration_s"], result["status"])
        result["healthcheck"] = healthcheck.ping(result, cfg=cfg, transport=healthcheck_transport)
        return result
    finally:
        release_lock(db_path=db_path)


def run_loop(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    interval_s: float | None = None,
    max_cycles: int | None = None,
    sleep: Any = time.sleep,
) -> int:
    """The one long-running process: holds the runner lock (with its heartbeat) for its whole life
    and drives every stage, in order, every `orchestrator.loop_interval_seconds`.

    Returns the number of cycles run (`max_cycles` is for tests; production runs until Ctrl-C or
    SIGTERM). If another runner holds the lock it returns 0 at once rather than waiting.
    """
    fixed_config = cfg
    cfg = cfg or config.get_config()
    db.init_db(db_path)
    gap = interval_s if interval_s is not None else float(
        (cfg.get("orchestrator") or {}).get("loop_interval_seconds", 60)
    )
    stale_timeout, hb_interval = _lock_settings(cfg)
    if not acquire_lock(db_path=db_path, stale_timeout_s=stale_timeout, heartbeat_interval_s=hb_interval, kind="loop"):
        log.warning("Could not acquire lock; another runner is active. Loop not started.")
        return 0

    log.info("Runner loop started (interval %.0fs). Ctrl-C to stop.", gap)
    cycles = 0
    try:
        while (max_cycles is None or cycles < max_cycles) and not stopping(db_path):
            try:
                cfg = fixed_config or config.get_config(reload=True)
                result = run_stages(cfg, db_path, graceful=True, reload_before_post=fixed_config is None)
                log.info("Cycle %d: %s%s", cycles + 1, result["status"],
                         f" (failed stages: {sorted(result['errors'])})" if result["errors"] else "")
                # Dead-man switch: only after a fully successful cycle; never raises.
                if not stopping(db_path) and not db.kv_get("dashboard:demo", db_path=db_path):
                    healthcheck.ping(result, cfg=cfg, transport=healthcheck_transport)
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - run_stages already isolates; this is the last net
                log.exception("Cycle failed; carrying on after the interval")
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            db.kv_set("runner:stage", "idle", db_path=db_path)
            gap = interval_s if interval_s is not None else float(
                (cfg.get("orchestrator") or {}).get("loop_interval_seconds", 60))
            if sleep is not time.sleep:
                sleep(gap)
            else:
                until = time.monotonic() + gap
                while time.monotonic() < until and not stopping(db_path):
                    sleep(min(0.5, max(0.0, until - time.monotonic())))
    except KeyboardInterrupt:
        log.info("Runner loop stopped.")
    finally:
        release_lock(db_path=db_path)
    return cycles


def run_dashboard_operations(cfg, db_path, job_id=None):
    """Run queued read-only analytics or retention operations under the runner lock."""
    with db.db(db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dashboard_commands'").fetchone():
            return
        jobs = conn.execute(
            "SELECT * FROM dashboard_commands WHERE status='pending' AND action IN ('snapshot_now','run_stage') "
            "AND (? IS NULL OR id=?) ORDER BY rowid", (job_id, job_id)
        ).fetchall()
    for job in jobs:
        payload = json.loads(job["payload"])
        try:
            if db.kv_get("dashboard:demo", db_path=db_path):
                result = "Demo operation completed."
            elif job["action"] == "snapshot_now":
                ids = analytics.snapshot_now(job["item_id"], cfg, db_path)
                result = f"Captured {len(ids)} live post snapshot(s)."
            else:
                result = str(EXTRA_COMMANDS[payload["stage"]](cfg, db_path))
            status = "applied"
        except Exception as exc:
            status, result = "failed", posting.redact(exc)
            log.warning("Dashboard operation %s: %s", job["id"], result)
        with db.db(db_path) as conn:
            conn.execute("UPDATE dashboard_commands SET status=?, result=?, completed_at=? WHERE id=?",
                         (status, result[:500], db.now(), job["id"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="GTA 6 Pipeline Runner")
    parser.add_argument("--db", type=str, default=None, help="Custom database path")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("discover", help="Run discovery stage only")
    subparsers.add_parser("download", help="Run download & dedupe stage only")
    subparsers.add_parser("cut", help="Run audio loudness & scene cut stage only")
    subparsers.add_parser("hooks", help="Run Gemini hook generation stage only")
    subparsers.add_parser("voiceover", help="Run Kokoro TTS voiceover stage only")
    subparsers.add_parser("render", help="Run full vertical 9:16 video render (Variants A & B)")
    subparsers.add_parser("approve", help="Run one approval pass (Telegram card + long poll, or the auto gate)")
    subparsers.add_parser("schedule", help="Give approved items a variant and the next free posting slot")
    subparsers.add_parser("post", help="Settle crashed publishes, schedule, then post items whose slot has come")
    subparsers.add_parser("analytics", help="Take due 24h/72h snapshots and rebuild ab_learning")
    subparsers.add_parser("cleanup", help="Apply disk retention (source, renders, rejected/failed media)")
    subparsers.add_parser("cycle", help="Run complete pipeline cycle")
    loop_parser = subparsers.add_parser("loop", help="The long-running process: every stage in order, forever")
    loop_parser.add_argument("--interval", type=float, default=None, help="Seconds between cycles")
    make_parser = subparsers.add_parser("make", help="Create clips without publishing")
    make_parser.add_argument("--job", required=True)
    operation = subparsers.add_parser("operation", help="Run a confirmed dashboard operation")
    operation.add_argument("--job", required=True)
    subparsers.add_parser("status", help="Print current database status and stage counts")

    args = parser.parse_args()
    db_p = args.db

    if args.command in ("discover", "download", "cut"):
        db.init_db(db_p)

    if args.command == "discover":
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            items = discover.run_discovery(db_path=db_p)
            print(f"Discovered {len(items)} items.")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "download":
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            items = download.run_download_pipeline(db_path=db_p)
            print(f"Downloaded {len(items)} items.")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "cut":
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            cut_ids = cut.run_cut_pipeline(db_path=db_p)
            print(f"Cut {len(cut_ids)} items.")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "hooks":
        db.init_db(db_p)  # apply schema migrations (e.g. items.transcript_words) first
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            items = db.get_items_by_stage("cut", db_path=db_p)
            for itm in items:
                ok, msg = hooks.run_hooks_for_item(itm["id"], db_path=db_p)
                print(f"Hooks {itm['id']}: {ok} - {msg}")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "voiceover":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            items = db.get_items_by_stage("cut", db_path=db_p)
            for itm in items:
                ok, msg = voiceover.run_voiceover_for_item(itm["id"], db_path=db_p)
                print(f"Voiceover {itm['id']}: {ok} - {msg}")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "render":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            items = db.get_items_by_stage("cut", db_path=db_p)
            print(f"Found {len(items)} items ready for render.")
            for itm in items:
                ok, msg, stats = render.run_render_item(itm["id"], db_path=db_p)
                print(f"Render {itm['id']}: {ok} - {msg} ({stats})")
        finally:
            release_lock(db_path=db_p)
    elif args.command == "approve":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            print(telegram_bot.run_approval_stage(db_path=db_p))
        finally:
            release_lock(db_path=db_p)
    elif args.command == "schedule":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            print(scheduler.run_scheduler_stage(db_path=db_p))
        finally:
            release_lock(db_path=db_p)
    elif args.command == "post":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            print(scheduler.run_scheduler_stage(db_path=db_p))
            print(posting.run_posting_stage(db_path=db_p))
        finally:
            release_lock(db_path=db_p)
    elif args.command in EXTRA_COMMANDS:
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            print(EXTRA_COMMANDS[args.command](config.get_config(), db_p))
        finally:
            release_lock(db_path=db_p)
    elif args.command == "cycle":
        run_single_cycle(db_path=db_p)
    elif args.command == "loop":
        run_loop(db_path=db_p, interval_s=args.interval)
    elif args.command == "make":
        from .creation import make_worker
        make_worker(args.job, db_path=db_p)
    elif args.command == "operation":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            return
        try:
            run_dashboard_operations(config.get_config(), db_p)
        finally:
            release_lock(db_path=db_p)
    elif args.command == "status":
        print_status(db_path=db_p)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
