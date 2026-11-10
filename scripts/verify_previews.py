"""Check every preview render for a mid-sentence ending and for SFX spacing.

One command, no prompts (run after scripts/render_previews.py):

    python scripts/verify_previews.py

For every media/previews/<item>/P{n}_{A,B}.mp4 it
  1. transcribes the LAST 3 s of the render with faster-whisper and checks that the final
     word ends a sentence (. ? !) or that nobody speaks there;
  2. checks the same end against the SOURCE: the source audio from up to 20 s before the end
     (from the cut start) to 6 s after it is transcribed and measured, and the end must sit in a sentence break by
     the pipeline's own rule (pipeline/endings.py, audio-confirmed); the audio level at the
     cut point relative to the speaker is printed - this is the ground truth for "was the
     speaker cut off", since whisper can mishear or punctuate a truncated 3 s snippet;
  3. lists the SFX cue times that variant was mixed with (from <preset>.plan.json, written
     by render_previews.py) and the gaps between them, which must be >= style.sfx.min_gap_s
     with at most style.sfx.max_per_clip cues.

It writes the table into media/previews/README.txt (replacing an older table) and prints
it. Exit code 0 only if every row passes.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import audio_mix, config, cut as cutmod, endings, render, style  # noqa: E402

TABLE_START = "Endings and SFX check (scripts/verify_previews.py)"
TAIL_S = 3.0


@dataclass
class Row:
    item: str
    file: str
    duration: float = 0.0
    last_words: str = ""
    final_word: str = ""
    snippet_ok: bool = False
    source_ok: bool | None = None
    source_note: str = ""
    sfx: list[float] = field(default_factory=list)
    gaps: list[float] = field(default_factory=list)
    sfx_ok: bool = True
    error: str = ""

    @property
    def ending_ok(self) -> bool:
        # The source check is the ground truth; a snippet without punctuation is fine when
        # the source shows a long pause (the rule's no-punctuation case).
        if self.source_ok is None:
            return self.snippet_ok
        return self.source_ok and (self.snippet_ok or "pause" in self.source_note)

    @property
    def ok(self) -> bool:
        return not self.error and self.ending_ok and self.sfx_ok


def _extract_wav(src: Path | str, start: float, dur: float, out: Path) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-ss", f"{max(0.0, start):.3f}", "-t", f"{dur:.3f}",
           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)


def tail_words(mp4: Path, cfg: dict[str, Any], seconds: float = TAIL_S) -> tuple[float, list[dict[str, Any]]]:
    dur = render.media_duration(mp4)
    with tempfile.TemporaryDirectory(prefix="gta6_verify_") as tmp:
        wav = Path(tmp) / "tail.wav"
        _extract_wav(mp4, dur - seconds, seconds, wav)
        words = render.transcribe_words(wav, raise_errors=True, cfg=cfg)
    return dur, words


def source_check(plan: dict[str, Any], es: dict[str, Any], cfg: dict[str, Any],
                 cache: dict[tuple, tuple[bool, str]]) -> tuple[bool | None, str]:
    """Is the end, placed back in the source, in a sentence break of the source speech?"""
    src = plan.get("download_path")
    if not src or not Path(src).exists() or plan.get("cut_start") is None or plan.get("source_end_s") is None:
        return None, "source not on disk"
    end_abs = float(plan["cut_start"]) + float(plan["source_end_s"])
    key = (src, round(end_abs, 3))
    if key in cache:
        return cache[key]
    # 20 s of context before the end: whisper's punctuation (and spelling) on a 5 s snippet is
    # unreliable - it wrote "level," + "they're" where 24 s of context gives "level." - and the
    # check must not be weaker than the decision it checks.
    # ...and 6 s after it: with only 2 s, whisper's alignment at the window edge pulled the next
    # word ("And", really +0.40 s) back into the silence before the cut.
    a, b = max(float(plan.get("cut_start") or 0.0), end_abs - 20.0), end_abs + 6.0
    words = cutmod.transcribe_source_span(src, a, b, cfg=cfg)
    env = cutmod.audio_envelope(src, a - 5.0, b)          # 10 s of context for the speech level
    speech = env.speech_level() if env else None
    rel = (env.at(env.index(end_abs)) - speech
           if env and speech is not None and 0 <= env.index(end_abs) < len(env.db) else None)
    level = f" | end {rel:+.0f} dB" if rel is not None else ""
    loud = rel is not None and rel > -float(es["pause_drop_db"])     # sound at the cut point
    phantoms: list[str] = []
    if env and speech is not None:
        # whisper sometimes places a short word inside measured silence (Hazard: "I" put before
        # the cut although the audio is -16..-30 dB there and the whole-source transcript has
        # "And I" 0.2 s after it). A word that is entirely quiet was not spoken there.
        quiet = speech - float(es["audio_gap_drop_db"])
        kept = []
        for w in words:
            a_i, b_i = env.index(float(w["start"])), env.index(float(w["end"]))
            vals = [env.at(k) for k in range(max(0, a_i), min(len(env.db), b_i + 1))]
            if vals and max(vals) <= quiet:
                phantoms.append(str(w["word"]))
            else:
                kept.append(w)
        words = kept
    note_ph = f" (ignored '{' '.join(phantoms)}' placed in silence)" if phantoms else ""
    if not [w for w in words if float(w["start"]) < end_abs + 6.0]:
        res = (True, "no speech around the end" + level)
    else:
        spans = endings.safe_spans(words, a, b, b, es, env=env)
        span = endings.span_at(end_abs, spans, tol=0.02)
        before = [w for w in words if float(w["end"]) <= end_abs + 1e-3]
        after = [w for w in words if float(w["start"]) >= end_abs - 1e-3]
        inside = [w for w in words if float(w["start"]) < end_abs < float(w["end"])]
        last = before[-1]["word"] if before else "-"
        nxt = f"{after[0]['word']} +{float(after[0]['start']) - end_abs:.2f}s" if after else "nothing"
        if loud:
            res = (False, f"END IN SOUND{(' (' + inside[0]['word'] + ')') if inside else ''}: '{last}' -> {nxt}{level}")
        elif inside and rel is None:
            res = (False, f"END INSIDE '{inside[0]['word']}'{level}")
        elif span is None:
            res = (False, f"mid-sentence: '{last}' -> {nxt}{level}")
        else:
            # whisper sometimes starts the next word inside a measured pause; the audio is the truth
            note = f" (whisper starts '{inside[0]['word']}' early)" if inside else ""
            res = (True, f"{span.reason}: '{last}' | next {nxt}{level}{note}{note_ph}")
    cache[key] = res
    return res


def check(out_root: Path, items: Sequence[str] | None = None, cfg: dict[str, Any] | None = None) -> list[Row]:
    cfg = cfg if cfg is not None else config.get_config()
    es = endings.ending_settings(cfg)
    ss = style.style_settings(cfg)
    rows: list[Row] = []
    cache: dict[tuple, tuple[bool, str]] = {}
    dirs = [out_root / i for i in items] if items else sorted(d for d in out_root.iterdir()
                                                              if d.is_dir() and not d.name.startswith("_"))
    for d in dirs:
        for mp4 in sorted(d.glob("P*_[AB].mp4")):
            preset, variant = mp4.stem.split("_")
            row = Row(item=d.name, file=mp4.name)
            plan_file = d / f"{preset}.plan.json"
            plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else {}
            try:
                row.duration, words = tail_words(mp4, cfg)
                row.last_words = " ".join(w["word"] for w in words[-4:]) or "(no speech)"
                row.final_word = words[-1]["word"] if words else ""
                row.snippet_ok = (not words) or endings.ends_sentence(row.final_word, es)
                row.source_ok, row.source_note = source_check(plan, es, cfg, cache) if plan else (None, "no plan")
            except Exception as e:  # noqa: BLE001 - reported per row
                row.error = f"{type(e).__name__}: {e}"
            cues = (plan.get("sfx") or {}).get(variant, [])
            row.sfx = sorted(round(float(c[0]), 2) for c in cues)
            row.gaps = [round(b - a, 2) for a, b in zip(row.sfx, row.sfx[1:])]
            problems = audio_mix.sfx_spacing_problems(
                [audio_mix.SfxCue(float(c[0]), str(c[1]), "") for c in cues], ss)
            row.sfx_ok = not problems and plan_file.exists()
            rows.append(row)
    return rows


def table(rows: Sequence[Row], ss: dict[str, Any]) -> list[str]:
    gap = ss["sfx"]["min_gap_s"]
    cap = ss["sfx"]["max_per_clip"]
    lines = [
        TABLE_START,
        "-" * len(TABLE_START),
        f"Ending: whisper on the last {TAIL_S:.0f} s of the render (final word must end in . ? !, or no speech),",
        "and the end re-checked against the source audio after it (pipeline/endings.py rule).",
        f"SFX: cue start times on that variant's output timeline; every gap must be >= {gap} s, at most {cap} cues.",
        "",
        f"{'item':<18} {'file':<9} {'dur':>6}  {'last words (render tail)':<34} {'source check':<70} "
        f"{'SFX cues (s)':<16} {'gaps (s)':<9} result",
    ]
    for r in rows:
        sfx = ", ".join(f"{t:.2f}" for t in r.sfx) or "-"
        gaps = ", ".join(f"{g:.2f}" for g in r.gaps) or "-"
        verdict = "OK" if r.ok else ("ERROR " + r.error if r.error else
                                     "FAIL" + ("" if r.ending_ok else " ending") + ("" if r.sfx_ok else " sfx"))
        lines.append(f"{r.item:<18} {r.file:<9} {r.duration:6.2f}  {r.last_words[-34:]:<34} "
                     f"{r.source_note[:70]:<70} {sfx:<16} {gaps:<9} {verdict}")
    passed = sum(1 for r in rows if r.ok)
    lines += ["", f"{passed}/{len(rows)} renders pass."]
    return lines


def write_into_readme(out_root: Path, lines: Sequence[str]) -> Path:
    readme = out_root / "README.txt"
    text = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if TABLE_START in text:
        text = text[: text.index(TABLE_START)].rstrip() + "\n"
    readme.write_text(text + ("\n" if text else "") + "\n".join(lines) + "\n", encoding="utf-8")
    return readme


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check preview renders for mid-sentence endings and SFX spacing.")
    ap.add_argument("--out", default=None, help="previews root (default media/previews)")
    ap.add_argument("--items", nargs="*", default=None)
    args = ap.parse_args(argv)
    cfg = config.get_config()
    out_root = Path(args.out) if args.out else config.media_dir("previews")
    rows = check(out_root, args.items, cfg)
    lines = table(rows, style.style_settings(cfg))
    readme = write_into_readme(out_root, lines)
    print("\n".join(lines))
    print(f"\nWrote {readme}")
    return 0 if rows and all(r.ok for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
