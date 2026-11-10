"""Benchmark caption transcription on the preview items: model x glossary, time and word diffs.

One command, no prompts (run on the machine that will render, since timings are CPU-bound):

    python scripts/benchmark_captions.py
    python scripts/benchmark_captions.py --items item_a item_b --runs 3

For every item it transcribes the audio the CAPTIONS are actually built from - the cut stage's
span: the source from the cut start to start + max duration + the 2 s tail, as
cut.transcribe_source_span extracts it - plus, separately, the cut file itself (what render
transcribes when no cached words exist). Each clip is transcribed `--runs` times after a
warm-up (model load excluded); it reports the mean wall time per clip and every CAPTION word
(span words that end inside the cut) that differs from the first configuration, i.e. from
what the captions said before the glossary existed. Configurations:

    base.en                   no glossary (the old captions)
    base.en + hotwords        glossary as faster-whisper hotwords
    base.en + initial_prompt  glossary in the initial prompt
    small.en                  no glossary
    small.en + hotwords       glossary as hotwords
    small.en + initial_prompt glossary in the initial prompt

Writes media/previews/caption_benchmark.json and prints a table.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config, db, endings, transcribe  # noqa: E402

DEFAULT_ITEMS = ["item_00983f85d1fd", "item_1032dd2c31da", "item_5a67cc8642ed"]
CONFIGS: list[tuple[str, str, str]] = [            # (label, model, glossary_as)
    ("base.en", "base.en", "none"),
    ("base.en + hotwords", "base.en", "hotwords"),
    ("base.en + initial_prompt", "base.en", "initial_prompt"),
    ("small.en", "small.en", "none"),
    ("small.en + hotwords", "small.en", "hotwords"),
    ("small.en + initial_prompt", "small.en", "initial_prompt"),
]


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def word_diff(before: Sequence[str], after: Sequence[str]) -> list[str]:
    """['-wanna love +wanted level', ...] for every changed stretch (case/punctuation ignored)."""
    a, b = [norm(w) for w in before], [norm(w) for w in after]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        old = " ".join(before[i1:i2])
        new = " ".join(after[j1:j2])
        out.append(f"-{old or '(none)'} +{new or '(none)'}")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark caption models and the GTA glossary.")
    ap.add_argument("--items", nargs="*", default=DEFAULT_ITEMS)
    ap.add_argument("--runs", type=int, default=2)
    args = ap.parse_args(argv)

    base_ts = transcribe.settings()
    cfg = config.get_config()
    tail = float(endings.ending_settings(cfg)["tail_s"])
    max_dur = float((cfg.get("cut") or {}).get("max_duration", 22.0))
    tmp = Path(tempfile.mkdtemp(prefix="gta6_bench_"))
    clips: dict[str, Path] = {}          # "<item>:span" and "<item>:cut"
    cut_len: dict[str, float] = {}
    for iid in args.items:
        it = db.get_item(iid) or {}
        src, cut = Path(it.get("download_path") or ""), Path(it.get("cut_path") or "")
        if not (src.exists() and cut.exists() and it.get("cut_start") is not None):
            print(f"{iid}: source or cut not on disk, skipped")
            continue
        a = float(it["cut_start"])
        wav = tmp / f"{iid}_span.wav"
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{max_dur + tail:.3f}",
                        "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                       capture_output=True, check=True)
        clips[f"{iid}:span"] = wav
        clips[f"{iid}:cut"] = cut
        cut_len[iid] = float(it["cut_end"]) - a
    if not clips:
        return 1

    results: dict[str, dict[str, Any]] = {}
    for label, model, mode in CONFIGS:
        ts = {**base_ts, "model": model, "glossary_as": mode}
        first = next(iter(clips.values()))
        t0 = time.perf_counter()
        transcribe.transcribe_words(first, ts=ts, raise_errors=True)      # load + warm-up
        warm = time.perf_counter() - t0
        per: dict[str, Any] = {}
        for iid, clip in clips.items():
            times, words = [], []
            for _ in range(max(1, args.runs)):
                t0 = time.perf_counter()
                words = transcribe.transcribe_words(clip, ts=ts, raise_errors=True)
                times.append(time.perf_counter() - t0)
            item = iid.split(":")[0]
            on_screen = [w for w in words if iid.endswith(":cut") or float(w["end"]) <= cut_len[item] + 1e-6]
            per[iid] = {"seconds": round(sum(times) / len(times), 2), "words": [w["word"] for w in on_screen]}
        results[label] = {"model": model, "glossary_as": mode, "warmup_s": round(warm, 1), "items": per,
                          "mean_s_per_clip": round(sum(v["seconds"] for v in per.values()) / len(per), 2)}
        print(f"{label:<26} {results[label]['mean_s_per_clip']:6.2f} s/clip  (load+warm-up {warm:.1f} s)", flush=True)

    for label in results:
        for kind in ("span", "cut"):
            ks = [v["seconds"] for k, v in results[label]["items"].items() if k.endswith(":" + kind)]
            results[label][f"mean_s_per_{kind}"] = round(sum(ks) / len(ks), 2) if ks else None
    print("\nmean seconds per clip:  span (24 s, feeds the captions) | cut file (~20 s)")
    for label in results:
        print(f"  {label:<26} {results[label]['mean_s_per_span']:6.2f} | {results[label]['mean_s_per_cut']:6.2f}")
    ref = CONFIGS[0][0]
    lines = ["", f"Caption-word diffs against '{ref}' (what the captions said before):"]
    for iid in clips:
        lines.append(f"\n{iid}  ({len(results[ref]['items'][iid]['words'])} words)")
        for label, *_ in CONFIGS[1:]:
            d = word_diff(results[ref]["items"][iid]["words"], results[label]["items"][iid]["words"])
            n = len(results[label]["items"][iid]["words"])
            lines.append(f"  {label:<26} {results[label]['items'][iid]['seconds']:5.2f} s  {n:3d} words  "
                         f"{len(d)} change(s): " + ("; ".join(d) if d else "-"))
    print("\n".join(lines))
    out = config.media_dir("previews") / "caption_benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
