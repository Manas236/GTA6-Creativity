"""Run the content gate (pipeline/content_gate.py) on a video or a window of it and print the scores.

    python scripts/content_gate_check.py VIDEO [--window START END ...] [--step 20]

Prints the whole-source verdict (source floors), each --window (clip floors) and, with --step, every
step-long window. Exit 0 always; this is a measuring tool.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import content_gate  # noqa: E402


def line(v: dict) -> str:
    return (f"{'PASS' if v['ok'] else 'FAIL'}  motion {v['motion_share']:.2f}  gameplay {v['game_share']:.2f} "
            f"(mean P {v['mean_game_prob']:.2f}, {v['clip_frames']} CLIP frames)"
            + (f"  | {v['reason']}" if v["reason"] else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video")
    ap.add_argument("--window", nargs=2, type=float, action="append", default=[], metavar=("START", "END"))
    ap.add_argument("--step", type=float, default=0.0)
    args = ap.parse_args(argv)
    t0 = time.time()
    tl = content_gate.scan(args.video)
    print(f"{Path(args.video).name}: {tl.end:.1f} s scanned in {time.time() - t0:.1f} s")
    print(f"  whole source       {line(content_gate.verdict(tl, scope='source'))}")
    print(f"  whole source (clip floors) {line(content_gate.verdict(tl))}")
    for a, b in args.window:
        print(f"  window {a:7.2f}-{b:7.2f} {line(content_gate.verdict(tl, a, b))}")
    if args.step:
        t = 0.0
        while t + args.step <= tl.end + 1e-6:
            print(f"  {t:7.1f}-{t + args.step:7.1f}  {line(content_gate.verdict(tl, t, t + args.step))}")
            t += args.step
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
