"""Render the P0-P3 x A/B style previews for the three CP2 items, on the laptop.

One command, no prompts:

    python scripts/render_previews.py

For each of the three CP2 items it:
  1. re-downloads the source (yt-dlp, idempotent) and re-cuts it unless the cut on disk
     was made by the current cut stage (its end checked against a completed sentence);
     an item whose cut fails with no_clean_ending is replaced by the next real item in
     pipeline.db (highest discovery score first) so there are still three,
  2. reuses the hook, voiceover script and authenticity already stored in pipeline.db,
  3. generates the C1 keywords with Gemini when GEMINI_API_KEY is set, else the rules,
  4. renders P0, P1, P2 and P3, variants A and B, into
     media/previews/<item_id>/P{n}_{A,B}.mp4,
  5. builds media/previews/<item_id>/grid_A.mp4 and grid_B.mp4: a silent 2x2 xstack of
     540x960 tiles labelled P0-P3,
  6. writes media/previews/README.txt and prints the render time of every file.

Options:
    --items ID [ID ...]   override the three default item ids (default: the CP2 ids that
                          exist in pipeline.db, topped up with the best-scoring real items)
    --presets P0 P3       render a subset
    --skip-download       reuse the cut already on disk (fails if there is none)
    --no-grid             skip the 2x2 comparison grids
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config, cut as cutmod, db, download, endings, hooks, keywords as kwmod, render, style, transcribe  # noqa: E402

log = logging.getLogger("render_previews")

# The three CP2 items this style layer is being judged on.
CP2_ITEM_IDS: list[str] = ["item_a35aead652d3", "item_763c2e191a1f", "item_4622278cd0fc"]
PRESETS: list[str] = ["P0", "P1", "P2", "P3"]
TILE_W, TILE_H = 540, 960


@dataclass
class ItemResult:
    item_id: str
    ok: bool = False
    message: str = ""
    hook: str = ""
    channel: str = ""
    keywords: list[str] = field(default_factory=list)
    keyword_source: str = ""
    renders: dict[str, dict[str, Any]] = field(default_factory=dict)   # preset -> info
    grids: dict[str, str] = field(default_factory=dict)
    no_clean_ending: bool = False      # the cut stage found no speech-safe end: swap the item
    transcript: list[str] = field(default_factory=list)   # last cut-stage words, when not rendered
    transcript_note: str = ""
    replaces: str = ""                 # the item this one was swapped in for


# --------------------------------------------------------------------------------------
# Source, cut, hook, keywords
# --------------------------------------------------------------------------------------

def ensure_source(item: dict[str, Any], cfg: dict[str, Any], db_path: Any = None) -> Path:
    """Re-download the item's source if it is not already on disk, or if the file on disk
    is one the current downloader would refuse (AV1 / above the height cap: those decode
    slowly enough to time out dedupe and scene detection). Idempotent."""
    existing = item.get("download_path")
    if existing and Path(existing).exists():
        acceptable, why = download.source_is_acceptable(existing, cfg)
        if acceptable:
            return Path(existing)
        log.info("  the source on disk is %s; re-downloading in the capped format ...", why)
        ok, msg = download.download_single_item(item, cfg=cfg, db_path=db_path)
        path = (db.get_item(item["id"], db_path=db_path) or {}).get("download_path")
        if ok and path and Path(path).exists():
            return Path(path)
        # The download writes to a temp file first, so the old source is still intact.
        log.warning("  re-download failed (%s); keeping the existing source", msg)
        return Path(existing)
    log.info("  downloading %s ...", item.get("source_url"))
    ok, msg = download.download_single_item(item, cfg=cfg, db_path=db_path)
    refreshed = db.get_item(item["id"], db_path=db_path) or {}
    path = refreshed.get("download_path")
    if not ok or not path or not Path(path).exists():
        raise RuntimeError(f"download failed: {msg}")
    return Path(path)


def cut_is_current(item: dict[str, Any], cfg: dict[str, Any]) -> bool:
    """The cut on disk was made by the current cut stage: its end was checked for a
    completed sentence (render.cut_end_verified) for exactly this window."""
    existing = item.get("cut_path")
    if not existing or not Path(existing).exists():
        return False
    return render.cut_end_verified(item, transcribe.cache_key(cfg))


def ensure_cut(item: dict[str, Any], cfg: dict[str, Any], db_path: Any = None) -> Path:
    """Re-cut the item unless its cut is on disk AND was made by the current cut stage.
    Idempotent: a second run reuses the cut the first run made."""
    existing = item.get("cut_path")
    if existing and Path(existing).exists() and cut_is_current(item, cfg):
        return Path(existing)
    log.info("  cutting%s ...", " (the cut on disk predates the sentence-end check)" if existing else "")
    ok, msg = cutmod.run_cut_item(item["id"], cfg=cfg, db_path=db_path)
    refreshed = db.get_item(item["id"], db_path=db_path) or {}
    path = refreshed.get("cut_path")
    if not ok or not path or not Path(path).exists():
        raise RuntimeError(f"cut failed: {msg}")
    return Path(path)


def resolve_keywords(
    item: dict[str, Any],
    words: Sequence[dict[str, Any]],
    cut_path: Path,
    cfg: dict[str, Any],
    db_path: Any = None,
) -> tuple[list[str], str]:
    """C1 keywords: Gemini when GEMINI_API_KEY is set, otherwise the rule fallback."""
    cached = kwmod.load_keywords(item)
    if cached:
        return cached, "cached (db)"
    api_key = config.secret("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        try:
            transcript = " ".join(str(w.get("word", "")) for w in words)
            data = hooks.generate_hook_and_voiceover(
                cut_path,
                item.get("original_title") or "GTA 6",
                transcript_text=transcript,
                model=(cfg.get("gemini", {}) or {}).get("model", "gemini-2.5-flash"),
                api_key=api_key,
            )
            kws = kwmod.clean_keywords(data.get("keywords"), cfg)
            if kws:
                kwmod.store_keywords(item["id"], kws, db_path=db_path)
                return kws, "gemini"
        except Exception as e:
            log.warning("  Gemini keyword call failed (%s); falling back to the rules", e)
    kws = kwmod.rule_keywords(words, cfg)
    if kws:
        kwmod.store_keywords(item["id"], kws, db_path=db_path)
    return kws, "rules"


# --------------------------------------------------------------------------------------
# 2x2 comparison grid
# --------------------------------------------------------------------------------------

def build_grid_cmd(
    tiles: Sequence[tuple[str, Path, float]],
    out_path: Path,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """ffmpeg command for a silent 2x2 xstack of 540x960 tiles labelled with the preset.

    `tiles` is [(label, path, duration_s)]; every tile is clone-padded to the longest so
    no preset is cut short by a shorter neighbour.
    """
    rs = render.render_settings(cfg)
    fps = int(rs["fps"])
    font = render.escape_filter_path(render._abs_project_path(rs["font_path"]))
    longest = max((d for _, _, d in tiles), default=0.0)
    parts: list[str] = []
    for i, (label, _path, dur) in enumerate(tiles):
        pad = max(0.0, longest - float(dur))
        tpad = f"tpad=stop=-1:stop_mode=clone:stop_duration={pad:.3f}," if pad > 0.01 else ""
        parts.append(
            f"[{i}:v]fps={fps},scale={TILE_W}:{TILE_H},{tpad}"
            f"drawtext=fontfile='{font}':text='{label}':x=16:y=16:fontsize=54:"
            f"fontcolor=yellow:box=1:boxcolor=black@0.65:boxborderw=12,"
            f"setsar=1,setpts=PTS-STARTPTS[t{i}]"
        )
    layout = "|".join(["0_0", "w0_0", "0_h0", "w0_h0"][: len(tiles)])
    parts.append("".join(f"[t{i}]" for i in range(len(tiles))) +
                 f"xstack=inputs={len(tiles)}:layout={layout}:fill=black[v]")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats"]
    for _, p, _d in tiles:
        cmd += ["-i", str(p)]
    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[v]", "-an",
        "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_path),
    ]
    return cmd


def build_grid(tiles: Sequence[tuple[str, Path, float]], out_path: Path, cfg=None) -> tuple[bool, float]:
    if len(tiles) < 2:
        return False, 0.0
    t0 = time.time()
    res = subprocess.run(build_grid_cmd(tiles, out_path, cfg), capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        log.error("  grid failed: %s", res.stderr[-800:])
        return False, time.time() - t0
    return True, time.time() - t0


# --------------------------------------------------------------------------------------
# Per-item render
# --------------------------------------------------------------------------------------

def render_item(
    item_id: str,
    cfg: dict[str, Any],
    presets: Sequence[str],
    out_root: Path,
    db_path: Any = None,
    skip_download: bool = False,
    make_grid: bool = True,
) -> ItemResult:
    result = ItemResult(item_id=item_id)
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        result.message = f"{item_id} is not in pipeline.db - run the CP1/CP2 stages first"
        return result

    try:
        if not skip_download:
            ensure_source(item, cfg, db_path=db_path)
            item = db.get_item(item_id, db_path=db_path) or item
        cut_path = ensure_cut(item, cfg, db_path=db_path)
        item = db.get_item(item_id, db_path=db_path) or item
    except Exception as e:
        result.message = str(e)
        result.no_clean_ending = "no_clean_ending" in str(e)
        try:   # show what the speech said even though nothing was rendered
            raw = json.loads((db.get_item(item_id, db_path=db_path) or {}).get("transcript_words") or "{}")
            result.transcript = [str(w.get("word", "")) for w in raw.get("words") or []]
            result.transcript_note = (f"{raw.get('key', '?')}, window {raw.get('cut_start')}-{raw.get('cut_end')} s "
                                      "from an earlier cut")
        except Exception:  # noqa: BLE001
            pass
        return result

    hook = item.get("hook_text") or "IS THIS REALLY GTA 6?"
    channel = item.get("author_name") or "Creator"
    result.hook, result.channel = hook, channel

    rs = render.render_settings(cfg)
    # Cut words cached by the cut/hooks stage when they match this cut window; else transcribe.
    tkey = transcribe.cache_key(cfg)
    words = render.cut_speech_words(item, cut_path, tkey, cfg)
    clean_cut_end = render.cut_end_verified(item, tkey)

    vo_path = item.get("voiceover_audio_path")
    if not vo_path or not Path(vo_path).exists():
        from pipeline import voiceover
        ok, msg = voiceover.run_voiceover_for_item(item_id, cfg=cfg, db_path=db_path)
        item = db.get_item(item_id, db_path=db_path) or item
        vo_path = item.get("voiceover_audio_path")
        if not ok or not vo_path or not Path(vo_path).exists():
            result.message = f"voiceover failed: {msg}"
            return result
    vo_path = Path(vo_path)
    vo_words = render.transcribe_words(vo_path, cfg=cfg)
    if not vo_words:
        vo_words = render.estimate_words_from_script(
            item.get("voiceover_script") or "", render.media_duration(vo_path)
        )

    result.keywords, result.keyword_source = resolve_keywords(item, words, cut_path, cfg, db_path=db_path)
    log.info("  keywords (%s): %s", result.keyword_source, ", ".join(result.keywords) or "-")

    item_dir = out_root / item_id
    item_dir.mkdir(parents=True, exist_ok=True)
    for preset in presets:
        a, b = item_dir / f"{preset}_A.mp4", item_dir / f"{preset}_B.mp4"
        t0 = time.time()
        try:
            res = render.render_item_variants(
                cut_path, vo_path, hook, words, vo_words, a, b, item_dir / ".subs",
                cfg=cfg, name=f"{item_id}_{preset}", preset=preset,
                channel_name=channel, keyword_list=result.keywords, clean_cut_end=clean_cut_end,
            )
        except (render.RenderVerificationError, endings.NoCleanEnding) as e:
            log.error("  %s FAILED: %s", preset, e)
            result.renders[preset] = {"ok": False, "error": str(e)}
            continue
        for k in ("ass_a", "ass_b"):
            Path(res[k]).unlink(missing_ok=True)
        result.renders[preset] = {
            "ok": True,
            "a": str(a), "b": str(b),
            "a_s": res["probe_a"]["video_duration"], "b_s": res["probe_b"]["video_duration"],
            "elapsed_a_s": res["elapsed_a_s"], "elapsed_b_s": res["elapsed_b_s"],
            "style_a": res["style_a"],
            "captions_a": res.get("caption_words_a") or [],
            "captions_b": res.get("caption_words_b") or [],
        }
        # What scripts/verify_previews.py checks: where the end sits in the source, and the
        # SFX cues each variant was mixed with (both on that variant's output timeline).
        (item_dir / f"{preset}.plan.json").write_text(json.dumps({
            "item_id": item_id, "preset": preset,
            "cut_start": item.get("cut_start"), "cut_end": item.get("cut_end"),
            "download_path": item.get("download_path"),
            "end_s": res.get("end_s"), "source_end_s": res.get("source_end_s"),
            "freeze_s": res.get("freeze_s"),
            "sfx": {"A": (res.get("style_a") or {}).get("sfx", []),
                    "B": (res.get("style_b") or {}).get("sfx", [])},
        }, indent=1, default=str), encoding="utf-8")
        print(f"    {preset}_A.mp4  {res['probe_a']['video_duration']:6.2f} s  "
              f"rendered in {res['elapsed_a_s']:6.1f} s")
        print(f"    {preset}_B.mp4  {res['probe_b']['video_duration']:6.2f} s  "
              f"rendered in {res['elapsed_b_s']:6.1f} s   (total {time.time() - t0:.1f} s)")

    if make_grid:
        for variant in ("A", "B"):
            tiles = [
                (p, Path(r[variant.lower()]), r[f"{variant.lower()}_s"])
                for p, r in result.renders.items() if r.get("ok")
            ]
            if len(tiles) < 2:
                continue
            grid = item_dir / f"grid_{variant}.mp4"
            ok, el = build_grid(tiles, grid, cfg)
            if ok:
                result.grids[variant] = str(grid)
                print(f"    grid_{variant}.mp4  {len(tiles)} tiles           built in {el:6.1f} s")

    ok_renders = [p for p, r in result.renders.items() if r.get("ok")]
    result.ok = bool(ok_renders)
    result.message = f"{len(ok_renders)}/{len(presets)} presets rendered"
    return result


# --------------------------------------------------------------------------------------

def _wrap(words: Sequence[str], width: int = 96) -> list[str]:
    lines, line = [], ""
    for w in words:
        if line and len(line) + len(w) + 1 > width:
            lines.append(line)
            line = ""
        line = f"{line} {w}".strip()
    return lines + ([line] if line else [])


def caption_lines(r: ItemResult, presets: Sequence[str]) -> list[str]:
    """Every caption word burned into this item's renders, so they can be read and checked.
    One list per variant; a preset whose captions differ (e.g. A4 ended it earlier) gets its own."""
    out: list[str] = []
    if not r.ok and r.transcript:
        out.append(f"  NOT RENDERED. Last cut-stage transcript ({r.transcript_note}) - not burned in anywhere:")
        out += ["    " + line for line in _wrap(r.transcript)]
    ok = [(p, r.renders[p]) for p in presets if (r.renders.get(p) or {}).get("ok")]
    for v in ("a", "b"):
        groups: list[tuple[list[str], list[str]]] = []          # (presets, words)
        for p, info in ok:
            words = list(info.get(f"captions_{v}") or [])
            for g in groups:
                if g[1] == words:
                    g[0].append(p)
                    break
            else:
                groups.append(([p], words))
        for ps, words in groups:
            label = f"captions {v.upper()} ({'/'.join(ps)}, {len(words)} words)"
            out.append(f"  {label}:")
            line = "    "
            for w in words:
                if len(line) + len(w) + 1 > 100:
                    out.append(line.rstrip())
                    line = "    "
                line += w + " "
            if line.strip():
                out.append(line.rstrip())
    return out


def write_readme(out_root: Path, results: Sequence[ItemResult], presets: Sequence[str]) -> Path:
    lines = [
        "GTA 6 clipping pipeline - style layer previews",
        "=" * 46,
        "",
        "Generated by: python scripts/render_previews.py",
        "",
        "Presets (every number lives in config.yaml -> style:)",
        "  P0 Control    zoom-to-fill framing only (the pipeline default)",
        "  P1 Clean      P0 + keyword colour/emoji, progress bar, credit tag,",
        "                caption pop-in, dead-air removal, loop-friendly end",
        "  P2 Punchy     P1 + hook pop/shake, punch-ins, SFX",
        "  P3 Full hype  P2 + slow push-in, music bed",
        "",
        "Per item:",
        "  P{n}_A.mp4   Variant A (cut + hook + captions)",
        "  P{n}_B.mp4   Variant B (freeze-frame voiceover intro, then the cut)",
        "  grid_A.mp4   silent 2x2 comparison, 540x960 tiles labelled P0-P3",
        "  grid_B.mp4   the same for Variant B",
        "",
        "Pick a preset, then set `style.preset` in config.yaml. The pipeline default",
        "stays P0 until that choice is made.",
        "",
        "Every caption word that is burned in is listed under each item (transcribed with",
        "transcribe.model and the GTA glossary from config.yaml). Variant B starts with the",
        "voiceover's words.",
        "",
    ]
    for r in results:
        lines.append(f"{r.item_id}" + (f"  (swapped in for {r.replaces}: no_clean_ending)" if r.replaces else ""))
        lines.append(f"  channel  : {r.channel}")
        lines.append(f"  hook     : {r.hook}")
        lines.append(f"  keywords : {', '.join(r.keywords) or '-'}  (source: {r.keyword_source or '-'})")
        if not r.ok:
            lines.append(f"  STATUS   : FAILED - {r.message}")
        for preset in presets:
            info = r.renders.get(preset)
            if not info:
                continue
            if info.get("ok"):
                lines.append(
                    f"  {preset}: A {info['a_s']:.2f}s ({info['elapsed_a_s']:.1f}s to render), "
                    f"B {info['b_s']:.2f}s ({info['elapsed_b_s']:.1f}s)"
                )
            else:
                lines.append(f"  {preset}: FAILED - {info.get('error')}")
        for v, g in r.grids.items():
            lines.append(f"  grid_{v}: {Path(g).name}")
        lines += caption_lines(r, presets)
        lines.append("")
    path = out_root / "README.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def candidate_items(db_path: Any = None) -> list[str]:
    """Real items worth previewing, best discovery score first: not synthetic, with a
    source URL, and not rejected or failed."""
    with db.db(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM items WHERE source_url LIKE 'http%' AND id NOT LIKE 'synthetic%' "
            "AND stage NOT IN ('rejected', 'failed') ORDER BY discovery_score DESC"
        ).fetchall()
    return [r[0] for r in rows]


def default_items(db_path: Any = None, want: int = 3) -> list[str]:
    """The CP2 ids that exist in this pipeline.db, topped up with the best real items."""
    ids = [i for i in CP2_ITEM_IDS if db.get_item(i, db_path=db_path)]
    for i in candidate_items(db_path):
        if len(ids) >= want:
            break
        if i not in ids:
            ids.append(i)
    return ids


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render the P0-P3 x A/B style previews for the CP2 items.")
    ap.add_argument("--items", nargs="*", default=None)
    ap.add_argument("--presets", nargs="*", default=PRESETS)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--out", default=None, help="output root (default media/previews)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = config.get_config()
    db.init_db()
    for p in args.presets:
        style.resolve_preset(p, cfg)          # fail fast on a typo before any rendering

    out_root = Path(args.out) if args.out else config.media_dir("previews")
    out_root.mkdir(parents=True, exist_ok=True)

    queue = list(args.items) if args.items else default_items()
    want = len(queue)
    started = time.time()
    results: list[ItemResult] = []
    tried: set[str] = set()
    replaces: dict[str, str] = {}
    while queue:
        item_id = queue.pop(0)
        tried.add(item_id)
        print(f"\n{item_id}" + (f"  (replacing {replaces[item_id]})" if item_id in replaces else ""))
        r = render_item(item_id, cfg, args.presets, out_root,
                        skip_download=args.skip_download, make_grid=not args.no_grid)
        r.replaces = replaces.get(item_id, "")
        if not r.ok:
            print(f"  FAILED: {r.message}")
        results.append(r)
        if r.no_clean_ending:
            spare = next((i for i in candidate_items() if i not in tried and i not in queue), None)
            if spare:
                print(f"  {item_id} has no clean ending; swapping in {spare}")
                replaces[spare] = item_id
                queue.append(spare)
            else:
                print(f"  {item_id} has no clean ending and pipeline.db has no other real item; "
                      "run `python -m pipeline.runner discover` and `download`, then re-run")
    ok_count = sum(1 for r in results if r.ok)
    if ok_count < want:
        print(f"\nOnly {ok_count} of {want} items rendered.")

    readme = write_readme(out_root, results, args.presets)
    print(f"\nWrote {readme}")
    print(f"Total {time.time() - started:.1f} s for {len(results)} item(s)")
    return 0 if ok_count >= want else 1


if __name__ == "__main__":
    raise SystemExit(main())
