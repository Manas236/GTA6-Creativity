"""Synthetic verification of the style layer: renders P0-P3 x A/B and proves the guard.

Builds a 22 s synthetic source that exercises every element:
  0.0 -  8.0 s  static, text-heavy card (a tweet-screenshot stand-in)  -> F1 keeps the fit
  8.0 - 12.0 s  moving footage                                         -> F1 fills
 12.0 - 16.0 s  moving footage, different scene (scene cut at 12 s)
 16.0 - 22.0 s  moving footage, different scene (scene cut at 16 s)
  speech-like word bursts with a 1.0 s silent gap at 5.0-6.0 s (inside the static card,
  so it is both quiet AND motionless -> A3 drops it) and loud peaks at 9.5 / 13.5 / 17.5 s
  (-> M3 punch-in anchors).

Usage:
    python scripts/style_check.py                 # all presets, writes the sheet
    python scripts/style_check.py --presets P0 P3
    python scripts/style_check.py --no-sheet

Outputs (mp4s are gitignored; only presets_sheet.png is committed):
    samples/synthetic/presets/P{n}_{A,B}.mp4
    samples/synthetic/presets_sheet.png
    samples/synthetic/style_report.json
Exits non-zero if any render fails the ffprobe guard.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import audio_mix, config, edits, plan as planning, render, style  # noqa: E402

HOOK = "Nobody noticed this GTA 6 detail"
CHANNEL = "GamingBolt"
VO_SCRIPT = "Is this the craziest GTA six detail yet?"

# (word, start, end) on the 22 s source timeline. Keywords (VICE/CITY/MAP/CARS/MONEY/
# POLICE/LEAK) drive C1 + C2; nothing is spoken across 5.0-6.0 s.
WORDS: list[tuple[str, float, float]] = [
    ("LOOK", 0.6, 0.95), ("AT", 1.0, 1.15), ("THIS", 1.2, 1.55), ("LEAK", 1.6, 2.1),
    ("THE", 3.0, 3.15), ("MAP", 3.2, 3.7), ("IS", 3.75, 3.95), ("HUGE", 4.0, 4.6),
    # 5.0 - 6.0 s: silence over the static card -> A3 target
    ("VICE", 6.4, 6.85), ("CITY", 6.9, 7.4),
    ("THOSE", 8.6, 8.95), ("CARS", 9.0, 9.5), ("LOOK", 9.6, 9.95), ("INSANE", 10.0, 10.7),
    ("ALL", 11.6, 11.85), ("THAT", 11.9, 12.15), ("MONEY", 12.2, 12.8),
    ("THE", 13.8, 13.95), ("POLICE", 14.0, 14.6), ("CHASE", 14.65, 15.2),
    ("IS", 16.2, 16.4), ("THIS", 16.45, 16.8), ("REAL", 16.85, 17.4),
    ("WAIT", 18.6, 18.95), ("FOR", 19.0, 19.2), ("IT", 19.25, 19.5),
    ("RIGHT", 20.4, 20.75), ("THERE", 20.8, 21.1),
]
PEAKS = [(9.4, 10.1), (13.4, 14.1), (17.4, 18.1)]   # loud stretches for M3
SOURCE_S = 22.0


def run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {res.stderr[-1200:]}")


# --------------------------------------------------------------------------------------
# Synthetic source
# --------------------------------------------------------------------------------------

def text_card(path: Path) -> Path:
    """A static, text-heavy card: near-zero frame difference, so F1 must keep the fit."""
    im = Image.new("RGB", (1920, 1080), (248, 249, 250))
    d = ImageDraw.Draw(im)
    bold = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Black.ttf"), 54)
    body = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Bold.ttf"), 44)
    small = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Bold.ttf"), 34)
    d.rounded_rectangle((120, 120, 1800, 960), radius=28, fill=(255, 255, 255), outline=(207, 217, 222), width=3)
    d.ellipse((170, 180, 266, 276), fill=(29, 155, 240))
    d.text((290, 190), "SYNTHETIC SOURCE", font=bold, fill=(15, 20, 25))
    d.text((290, 250), "@style_layer_test", font=small, fill=(83, 100, 113))
    lines = [
        "This card is deliberately STATIC and text heavy.",
        "Mean frame difference is ~0, so the F1 per-segment",
        "decision must keep the full-width fit here and only",
        "zoom-to-fill once the moving footage starts at 8 s.",
        "",
        "Audio goes completely silent from 5.0 s to 6.0 s while",
        "nothing moves, which is the A3 dead-air target.",
    ]
    y = 360
    for ln in lines:
        d.text((190, y), ln, font=body, fill=(15, 20, 25))
        y += 76
    d.text((190, 880), "22 s - 1920x1080 - 30 fps", font=small, fill=(83, 100, 113))
    im.save(path)
    return path


def build_source(d: Path) -> tuple[Path, Path]:
    """Write cut.mp4 (22 s, 4 segments, 2 scene cuts) and vo.wav."""
    d.mkdir(parents=True, exist_ok=True)
    cut, vo = d / "cut.mp4", d / "vo.wav"
    card = text_card(d / "card.png")

    speech_gate = "+".join(f"between(t,{s},{e})" for _, s, e in WORDS)
    peak_gate = "+".join(f"between(t,{s},{e})" for s, e in PEAKS)
    # Background bed is muted during the 5-6 s gap so the stretch is genuinely silent.
    bed_gate = "1-between(t,5.0,6.0)"

    run([
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-loop", "1", "-t", "8", "-i", str(card),
        "-f", "lavfi", "-t", "4", "-i", "mandelbrot=size=1920x1080:rate=30",
        "-f", "lavfi", "-t", "4", "-i", "life=size=1920x1080:rate=30:mold=10:ratio=0.12:death_color=#102030",
        "-f", "lavfi", "-t", "6", "-i", "testsrc2=size=1920x1080:rate=30",
        "-f", "lavfi", "-i", f"sine=frequency=190:sample_rate=48000:duration={SOURCE_S}",
        "-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=48000:amplitude=0.05:duration={SOURCE_S}",
        "-filter_complex",
        # video: 4 segments concatenated -> 2 strong scene cuts at 8/12/16 s
        "[0:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v0];"
        "[1:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v1];"
        "[2:v]fps=30,format=yuv420p,setpts=PTS-STARTPTS[v2];"
        "[3:v]fps=30,hue=H=2*PI*t,format=yuv420p,setpts=PTS-STARTPTS[v3];"
        "[v0][v1][v2][v3]concat=n=4:v=1:a=0[v];"
        # audio: gated speech-like bursts + louder peaks + a gated noise bed
        f"[4:a]volume='if({speech_gate},0.55,0)+if({peak_gate},0.40,0)':eval=frame,"
        "tremolo=f=7:d=0.7[sp];"
        f"[5:a]volume='{bed_gate}*0.9':eval=frame[bed];"
        "[sp][bed]amix=inputs=2:normalize=0[a]",
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", str(SOURCE_S), str(cut),
    ])
    run([
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=3.2",
        "-af", "volume='0.25+0.55*gt(sin(2*PI*4.5*t),0)':eval=frame",
        "-ac", "1", "-c:a", "pcm_s16le", str(vo),
    ])
    return cut, vo


def test_tone(path: Path, seconds: float = 40.0) -> Path:
    """A2 stand-in: a generated tone, used only to prove the music path end to end.

    No CC0 music host is reachable from the build sandbox, so no synthetic 'music' track
    is committed; the real CC0 bed is fetched by scripts/fetch_assets.py on the laptop.
    """
    if not path.exists():
        run([
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-f", "lavfi", "-i", f"sine=frequency=110:sample_rate=48000:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=165:sample_rate=48000:duration={seconds}",
            "-filter_complex", "[0:a][1:a]amix=inputs=2:normalize=0,volume=0.5[a]",
            "-map", "[a]", "-ac", "2", "-c:a", "pcm_s16le", str(path),
        ])
    return path


# --------------------------------------------------------------------------------------
# Contact sheet
# --------------------------------------------------------------------------------------

def grab(video: Path, t: float, out: Path, size: tuple[int, int]) -> Image.Image:
    run(["ffmpeg", "-y", "-hide_banner", "-nostats", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", str(out)])
    return Image.open(out).convert("RGB").resize(size, Image.LANCZOS)


def presets_sheet(out_png: Path, results: dict, tmp: Path, tile=(306, 544)) -> Path:
    """P0-P3 at one shared timestamp (moving + static), plus a punch-in before/during."""
    tmp.mkdir(parents=True, exist_ok=True)
    presets = [p for p in ("P0", "P1", "P2", "P3") if p in results]
    tw, th = tile
    pad, lab = 8, 30

    fill_t = 10.0                       # inside the moving footage -> F1 fills
    fit_t = 3.0                         # inside the static card    -> F1 keeps the fit
    punch = next((p["at"] for r in results.values() for p in
                  [{"at": a} for a, _ in r["style_a"]["punches"]]), None)

    rows: list[tuple[str, list[tuple[Path, float, str]]]] = [
        (f"Variant A @ {fill_t:.1f} s - moving footage (F1 zoom-to-fill)",
         [(Path(results[p]["a"]), fill_t, p) for p in presets]),
        (f"Variant A @ {fit_t:.1f} s - static text card (F1 keeps the full-width fit)",
         [(Path(results[p]["a"]), fit_t, p) for p in presets]),
    ]
    third: list[tuple[Path, float, str]] = []
    if punch is not None and "P3" in results:
        p3 = Path(results["P3"]["a"])
        third = [
            (p3, max(0.0, punch - 0.5), f"P3 @ {punch - 0.5:.2f} s (before punch-in)"),
            (p3, punch + 0.3, f"P3 @ {punch + 0.3:.2f} s (DURING punch-in 1.12x)"),
        ]
    emoji = next((c for r in results.values() for c in r["style_a"]["emoji"]), None)
    if emoji is not None and "P1" in results:
        at, word = emoji[0], emoji[1]
        third.append((Path(results["P1"]["a"]), at + 0.5, f"P1 @ {at + 0.5:.2f} s (C2 emoji: {word})"))
    if "P0" in results and "P3" in results:
        fz = results["P3"]["freeze_s"]
        third += [
            (Path(results["P0"]["b"]), fz / 2, f"P0 B @ {fz / 2:.2f} s (freeze)"),
            (Path(results["P3"]["b"]), fz / 2, f"P3 B @ {fz / 2:.2f} s (freeze + push)"),
        ]
    if third:
        rows.append(("Punch-in (M3), keyword emoji (C2) and the Variant B freeze (M4 slow push)", third))

    cols = max(len(r[1]) for r in rows)
    W = pad + cols * (tw + pad)
    H = sum(lab + th + pad for _ in rows) + lab
    sheet = Image.new("RGB", (W, H), (22, 22, 24))
    draw = ImageDraw.Draw(sheet)
    head = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Black.ttf"), 19)
    small = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Bold.ttf"), 15)

    draw.text((pad, 6), "GTA 6 style layer - synthetic verification (presets P0-P3)",
              fill=(255, 220, 0), font=head)
    y = lab
    for i, (title, shots) in enumerate(rows):
        draw.text((pad, y + 4), title, fill=(230, 230, 230), font=small)
        y += lab - 6
        for j, (video, t, label) in enumerate(shots):
            im = grab(video, t, tmp / f"s{i}_{j}.png", (tw, th))
            x = pad + j * (tw + pad)
            sheet.paste(im, (x, y))
            draw.rectangle((x, y, x + tw - 1, y + th - 1), outline=(70, 70, 74))
            draw.rectangle((x, y, x + min(tw, 11 * len(label) + 12), y + 22), fill=(0, 0, 0))
            draw.text((x + 5, y + 3), label, fill=(255, 220, 0), font=small)
        y += th + pad
    # Palette-quantize: the sheet is a reference image and must stay small enough to commit.
    sheet.convert("P", palette=Image.ADAPTIVE, colors=256).save(out_png, optimize=True)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return out_png


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--presets", nargs="*", default=["P0", "P1", "P2", "P3"])
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--rebuild-source", action="store_true")
    args = ap.parse_args()

    out = ROOT / "samples" / "synthetic"
    work = out / "presets"
    work.mkdir(parents=True, exist_ok=True)
    cut, vo = work / "cut.mp4", work / "vo.wav"
    if args.rebuild_source or not cut.exists() or not vo.exists():
        print("Building the synthetic source clip...")
        cut, vo = build_source(work)

    cfg = config.get_config()
    audio_mix.ensure_sfx_assets(style.style_settings(cfg))
    tone = test_tone(work / "music_test_tone.wav")
    mp = audio_mix.music_path(style.style_settings(cfg))
    if not mp.exists():
        cfg = json.loads(json.dumps(cfg))
        cfg["style"]["music_bed"]["file"] = str(tone)
        print(f"NOTE: no CC0 bed at {mp}; A2 verified with a generated test tone (not committed).")

    words = [{"word": w, "start": s, "end": e} for w, s, e in WORDS]
    vo_words = render.estimate_words_from_script(VO_SCRIPT, render.media_duration(vo))
    keyword_list = ["LEAK", "MAP", "VICE", "CITY", "CARS", "MONEY"]

    results: dict[str, dict] = {}
    rows: list[tuple] = []
    failed = False
    for preset in args.presets:
        t0 = time.time()
        a, b = work / f"{preset}_A.mp4", work / f"{preset}_B.mp4"
        try:
            res = render.render_item_variants(
                cut, vo, HOOK, words, vo_words, a, b, work / ".subs", cfg=cfg,
                name=preset, preset=preset, channel_name=CHANNEL, keyword_list=keyword_list,
            )
        except render.RenderVerificationError as e:
            print(f"FAIL {preset}: {e}")
            failed = True
            continue
        ass_a, ass_b = Path(res["ass_a"]).read_text(), Path(res["ass_b"]).read_text()
        for k in ("ass_a", "ass_b"):
            Path(res[k]).unlink(missing_ok=True)
        res["a"], res["b"] = str(a), str(b)
        res["ass_a_events"] = ass_a.count("Dialogue:")
        res["ass_b_events"] = ass_b.count("Dialogue:")
        results[preset] = res
        for name, p, path in (("A", res["probe_a"], a), ("B", res["probe_b"], b)):
            rows.append((preset, name, p["video_duration"], p["audio_duration"], p["fps"],
                         p["sample_rate"], p["channels"], f"{p['width']}x{p['height']}"))
        sa = res["style_a"]
        print(
            f"{preset}: {time.time() - t0:5.1f}s  A={res['probe_a']['video_duration']:.3f}s "
            f"B={res['probe_b']['video_duration']:.3f}s  modes={sa['modes']} "
            f"segments={len(sa['segments'])} punches={len(sa['punches'])} "
            f"emoji={len(sa['emoji'])} sfx={len(sa['sfx'])} dead_air={sa['dead_air_dropped']} "
            f"fade={sa['audio_fade_s']}s"
        )

    print("\n| preset | out | video s | audio s | fps | sample rate | ch | size | guard |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        ok = abs(r[2] - r[3]) <= 0.1 and r[4] == 30 and r[5] == 48000 and r[6] == 2 and r[7] == "1080x1920"
        print(f"| {r[0]} | {r[1]} | {r[2]:.3f} | {r[3]:.3f} | {r[4]:g} | {r[5]} | {r[6]} | {r[7]} | "
              f"{'PASS' if ok else 'FAIL'} |")
        failed = failed or not ok

    sheet = None
    if results and not args.no_sheet:
        sheet = presets_sheet(out / "presets_sheet.png", results, work / ".frames")
        print(f"\nContact sheet: {sheet} ({sheet.stat().st_size // 1024} KB)")

    report = {
        "source": {"path": "samples/synthetic/presets/cut.mp4", "duration_s": SOURCE_S,
                   "layout": "0-8 s static text card, 8-12/12-16/16-22 s moving (cuts at 8/12/16 s)",
                   "silent_gap_s": [5.0, 6.0], "loud_peaks_s": PEAKS},
        "presets": {
            p: {
                "probe_a": r["probe_a"], "probe_b": r["probe_b"],
                "elapsed_a_s": r["elapsed_a_s"], "elapsed_b_s": r["elapsed_b_s"],
                "ass_events_a": r["ass_a_events"], "ass_events_b": r["ass_b_events"],
                "style_a": r["style_a"], "style_b": r["style_b"],
            } for p, r in results.items()
        },
        "music_source": "generated test tone (no CC0 host reachable from the sandbox)"
        if not mp.exists() else str(mp),
        "all_guards_passed": not failed,
    }
    (out / "style_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Report: {out / 'style_report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
