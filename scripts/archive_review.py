"""Take every Review item created before a day out of Review (pipeline/archive.py).

    python scripts/archive_review.py --dry-run          # list what would move (default day: today, UTC)
    python scripts/archive_review.py --before 2026-11-19

Stage -> `archived`; files moved (never deleted) to media/archive/<day>/. Items with a post row are
never touched. Back up pipeline.db first; stop the runner and the dashboard while it runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import archive, db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--before", help="YYYY-MM-DD (UTC); default today")
    ap.add_argument("--db", default=None, help="database path (default pipeline.db)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    db.init_db(args.db)
    done = archive.archive_before(args.before, db_path=args.db, dry_run=args.dry_run)
    for row in done:
        print(json.dumps(row, indent=1))
    print(f"{'Would archive' if args.dry_run else 'Archived'} {len(done)} item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
