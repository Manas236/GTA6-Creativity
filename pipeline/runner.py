"""Main pipeline orchestrator and CLI entry points with heartbeat-based locking."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import platform
import socket
import sys
import threading
import time
from typing import Any

from . import config, cut, db, discover, download, hooks, posting, render, telegram_bot, voiceover

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gta6.runner")

LOCK_KEY = "runner:lock"
_HEARTBEAT_THREAD: threading.Thread | None = None
_STOP_HEARTBEAT = threading.Event()


def is_pid_alive(pid: int) -> bool:
    """Check if process with given PID is currently active."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                exit_code = ctypes.c_ulong()
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel32.CloseHandle(handle)
                return exit_code.value == 259  # STILL_ACTIVE
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _heartbeat_worker(db_path: Any, interval_s: float) -> None:
    while not _STOP_HEARTBEAT.wait(timeout=interval_s):
        try:
            raw = db.kv_get(LOCK_KEY, db_path=db_path)
            if raw:
                data = json.loads(raw)
                if data.get("pid") == os.getpid():
                    data["heartbeat"] = time.time()
                    db.kv_set(LOCK_KEY, json.dumps(data), db_path=db_path)
        except Exception as e:
            log.debug("Heartbeat update failed: %s", e)


def acquire_lock(db_path: Any = None, stale_timeout_s: float = 120.0, heartbeat_interval_s: float = 30.0) -> bool:
    """Acquire orchestrator lock. Breaks lock if PID is dead or heartbeat is older than 120s."""
    now_t = time.time()
    current_host = socket.gethostname()
    current_pid = os.getpid()

    raw = db.kv_get(LOCK_KEY, db_path=db_path)
    if raw:
        try:
            data = json.loads(raw)
            lock_pid = data.get("pid")
            lock_host = data.get("host")
            lock_heartbeat = float(data.get("heartbeat", 0.0))

            if lock_host == current_host:
                if not is_pid_alive(lock_pid):
                    log.warning("Breaking stale lock: process PID %s on host %s is dead", lock_pid, lock_host)
                elif (now_t - lock_heartbeat) > stale_timeout_s:
                    log.warning("Breaking stale lock: heartbeat is %.1fs old (>= %.0fs)", now_t - lock_heartbeat, stale_timeout_s)
                else:
                    log.warning("Runner lock active: PID %s on host %s is alive (heartbeat %.1fs ago)", lock_pid, lock_host, now_t - lock_heartbeat)
                    return False
            else:
                if (now_t - lock_heartbeat) > stale_timeout_s:
                    log.warning("Breaking stale remote lock: host %s heartbeat is %.1fs old (>= %.0fs)", lock_host, now_t - lock_heartbeat, stale_timeout_s)
                else:
                    log.warning("Runner lock active on remote host %s (heartbeat %.1fs ago)", lock_host, now_t - lock_heartbeat)
                    return False
        except (ValueError, TypeError):
            log.warning("Corrupt lock payload in database; clearing and acquiring")

    # Set new lock
    lock_payload = {
        "pid": current_pid,
        "host": current_host,
        "heartbeat": now_t,
        "started_at": now_t,
    }
    db.kv_set(LOCK_KEY, json.dumps(lock_payload), db_path=db_path)

    # Start background heartbeat updater
    global _HEARTBEAT_THREAD, _STOP_HEARTBEAT
    _STOP_HEARTBEAT.clear()
    _HEARTBEAT_THREAD = threading.Thread(
        target=_heartbeat_worker,
        args=(db_path, heartbeat_interval_s),
        daemon=True,
    )
    _HEARTBEAT_THREAD.start()
    return True


def release_lock(db_path: Any = None) -> None:
    """Release runner lock and stop heartbeat thread."""
    global _STOP_HEARTBEAT
    _STOP_HEARTBEAT.set()
    try:
        raw = db.kv_get(LOCK_KEY, db_path=db_path)
        if raw:
            data = json.loads(raw)
            if data.get("pid") == os.getpid():
                db.kv_set(LOCK_KEY, "", db_path=db_path)
    except Exception:
        db.kv_set(LOCK_KEY, "", db_path=db_path)


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


def run_single_cycle(cfg: dict[str, Any] | None = None, db_path: Any = None) -> dict[str, Any]:
    """Execute a single pipeline cycle: discover + download + dedupe."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    orch_cfg = cfg.get("orchestrator", {})
    stale_timeout = float(orch_cfg.get("heartbeat_stale_seconds", 120.0))
    hb_interval = float(orch_cfg.get("heartbeat_interval_seconds", 30.0))

    if not acquire_lock(db_path=db_path, stale_timeout_s=stale_timeout, heartbeat_interval_s=hb_interval):
        log.warning("Could not acquire lock; another runner is active.")
        return {"status": "locked"}

    t0 = time.time()
    try:
        log.info("Starting pipeline cycle...")

        # CP3 crash recovery comes first: rows left in 'publishing' by a crash are resolved
        # against the remote before anything new is published, and never published a second time.
        settled = posting.settle_publishing_rows(cfg=cfg, db_path=db_path)
        if settled:
            log.info("Settled %d post row(s) left in publishing: %s", len(settled), settled)

        discovered = discover.run_discovery(cfg=cfg, db_path=db_path)
        log.info("Discovered %d candidate items", len(discovered))

        downloaded = download.run_download_pipeline(cfg=cfg, db_path=db_path)
        log.info("Downloaded & verified %d items", len(downloaded))

        cut_ids = cut.run_cut_pipeline(cfg=cfg, db_path=db_path)
        log.info("Cut %d items", len(cut_ids))

        rendered_count = 0
        for cid in cut_ids:
            ok, msg, stats = render.run_render_item(cid, cfg=cfg, db_path=db_path)
            if ok:
                rendered_count += 1
                log.info("Rendered %s successfully (%s)", cid, stats)
            else:
                log.warning("Render failed for %s: %s", cid, msg)

        # The Telegram long poll runs here, inside the runner's loop: one card, one poll round.
        approval = telegram_bot.run_approval_stage(cfg=cfg, db_path=db_path)
        log.info("Approval pass: %s", approval)

        posted = posting.run_posting_stage(cfg=cfg, db_path=db_path)
        log.info("Posting pass: %s", posted)

        elapsed = time.time() - t0
        log.info("Cycle finished in %.2fs", elapsed)
        return {
            "status": "success",
            "discovered_count": len(discovered),
            "downloaded_count": len(downloaded),
            "cut_count": len(cut_ids),
            "rendered_count": rendered_count,
            "approval": approval,
            "posted": posted,
            "settled": settled,
            "duration_s": elapsed,
        }
    finally:
        release_lock(db_path=db_path)


def run_loop(cfg: dict[str, Any] | None = None, db_path: Any = None, interval_s: float | None = None) -> None:
    """Run cycles until interrupted. The Telegram long poll happens inside each cycle."""
    cfg = cfg or config.get_config()
    gap = interval_s if interval_s is not None else float(
        (cfg.get("orchestrator") or {}).get("loop_interval_seconds", 60)
    )
    log.info("Runner loop started (interval %.0fs). Ctrl-C to stop.", gap)
    while True:
        try:
            result = run_single_cycle(cfg=cfg, db_path=db_path)
            log.info("Cycle: %s", result.get("status"))
        except KeyboardInterrupt:
            log.info("Runner loop stopped.")
            return
        except Exception:
            log.exception("Cycle failed; carrying on after the interval")
        try:
            time.sleep(gap)
        except KeyboardInterrupt:
            log.info("Runner loop stopped.")
            return


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
    subparsers.add_parser("post", help="Settle crashed publishes, then post approved items to Meta")
    subparsers.add_parser("cycle", help="Run complete pipeline cycle")
    loop_parser = subparsers.add_parser("loop", help="Run cycles continuously (the approval long poll runs in it)")
    loop_parser.add_argument("--interval", type=float, default=None, help="Seconds between cycles")
    subparsers.add_parser("status", help="Print current database status and stage counts")

    args = parser.parse_args()
    db_p = args.db

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
    elif args.command == "post":
        db.init_db(db_p)
        if not acquire_lock(db_path=db_p):
            print("Locked: another runner active")
            sys.exit(1)
        try:
            print(posting.run_posting_stage(db_path=db_p))
        finally:
            release_lock(db_path=db_p)
    elif args.command == "cycle":
        run_single_cycle(db_path=db_p)
    elif args.command == "loop":
        run_loop(db_path=db_p, interval_s=args.interval)
    elif args.command == "status":
        print_status(db_path=db_p)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
