"""Main pipeline orchestrator and CLI entry points."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from . import config, db, discover, download

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gta6.runner")

LOCK_KEY = "runner:lock"


def acquire_lock(stale_seconds: int = 600, db_path: Any = None) -> bool:
    """Acquire orchestrator lock in kv table with stale-lock breaking."""
    now_ts = datetime.now(timezone.utc).timestamp()
    raw = db.kv_get(LOCK_KEY, db_path=db_path)
    if raw:
        try:
            lock_time = float(raw)
            if (now_ts - lock_time) < stale_seconds:
                log.warning("Runner lock active (age %.1fs < %ds)", now_ts - lock_time, stale_seconds)
                return False
            log.warning("Breaking stale lock (age %.1fs >= %ds)", now_ts - lock_time, stale_seconds)
        except ValueError:
            pass

    db.kv_set(LOCK_KEY, str(now_ts), db_path=db_path)
    return True


def release_lock(db_path: Any = None) -> None:
    db.kv_set(LOCK_KEY, "0", db_path=db_path)


def print_status(db_path: Any = None) -> None:
    db.init_db(db_path)
    with db.db(db_path) as conn:
        counts = conn.execute("SELECT stage, COUNT(*) as cnt FROM items GROUP BY stage").fetchall()
        recent = conn.execute(
            """
            SELECT id, source, author_name, original_title, discovery_score, stage 
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
    for r in recent:
        title = (r["original_title"] or "")[:45]
        print(f"  [{r['stage']:<10}] Score {r['discovery_score']:>7.2f} | {r['source']:<7} | {r['author_name'][:18]:<18} | {title}")
    print()


def run_single_cycle(cfg: dict[str, Any] | None = None, db_path: Any = None) -> dict[str, Any]:
    """Execute a single pipeline cycle for CP1: discover + download + dedupe."""
    cfg = cfg or config.get_config()
    db.init_db(db_path)

    log.info("Starting pipeline cycle...")
    t0 = time.time()

    # Step 1: Discover
    discovered = discover.run_discovery(cfg=cfg, db_path=db_path)
    log.info("Discovered %d candidate items", len(discovered))

    # Step 2: Download & Dedupe (pull-based)
    downloaded = download.run_download_pipeline(cfg=cfg, db_path=db_path)
    log.info("Downloaded & verified %d items", len(downloaded))

    elapsed = time.time() - t0
    log.info("Cycle finished in %.2fs", elapsed)
    return {
        "discovered_count": len(discovered),
        "downloaded_count": len(downloaded),
        "duration_s": elapsed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="GTA 6 Pipeline Runner")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("discover", help="Run discovery stage only")
    subparsers.add_parser("download", help="Run download & dedupe stage only")
    subparsers.add_parser("cycle", help="Run complete cycle (discover + download + dedupe)")
    subparsers.add_parser("status", help="Print current database status and stage counts")

    args = parser.parse_args()

    if args.command == "discover":
        items = discover.run_discovery()
        print(f"Discovered {len(items)} items.")
    elif args.command == "download":
        items = download.run_download_pipeline()
        print(f"Downloaded {len(items)} items.")
    elif args.command == "cycle":
        run_single_cycle()
    elif args.command == "status":
        print_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
