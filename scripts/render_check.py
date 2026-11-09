"""Render verification for the CP2 render fixes.

Usage:
    python scripts/render_check.py            # sample pack if present, else synthetic clip
    python scripts/render_check.py --synthetic

Sample pack layout (laptop only, not in the public repo):
    samples/cp2/<item_id>/cut.mp4, vo.wav, item.json
item.json keys read (first one present wins):
    hook:         hook_text | hook
    speech words: speech_words | words | transcript_words   (cut timeline, [{word,start,end}])
    vo words:     vo_words                                   (VO timeline; else whisper / script estimate)
    vo script:    voiceover_script | vo_script

Writes per item: A_v2.mp4, B_v2.mp4, contact_sheet.png, render_report.json,
and prints an ffprobe table plus measured hook / caption cap heights.
Exits non-zero if any verification fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from pipeline import config, render  # noqa: E402

SYNTH_WORDS = [
    # (word, start, end) on the 20 s cut timeline; speech-like bursts with pauses.
    ("NO", 0.6, 0.9), ("WAY", 0.95, 1.35), ("THIS", 1.4, 1.7), ("IS", 1.75, 1.95), ("REAL", 2.0, 2.5),
    ("LOOK", 4.0, 4.35), ("AT", 4.4, 4.55), ("THE", 4.6, 4.75), ("CITY", 4.8, 5.3),
    ("VICE", 8.0, 8.4), ("CITY", 8.45, 8.9), ("LOOKS", 9.0, 9.4), ("INSANE", 9.45, 10.1),
    ("THE", 13.0, 13.15), ("DETAIL", 13.2, 13.7), ("ON", 13.75, 13.9), ("THOSE", 13.95, 14.3), ("CARS", 14.35, 14.9),
    ("WAIT", 16.6, 17.0), ("FOR", 17.05, 17.25), ("IT", 17.3, 17.5),
    ("RIGHT", 18.4, 18.75), ("THERE", 18.8, 19.1),
    # pause 19.1 -> 19.5, then a word that runs past the 20.0 s cut end (the old cut-off)
    ("BOOM", 19.5, 20.4),
]
SYNTH_VO_SCRIPT = "Is this the craziest GTA six moment yet?"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)


def make_synthetic_item(d: Path) -> dict:
    """20 s 16:9 59.94 fps clip, 96 kHz audio: speech-like bursts + continuous noise to the last frame."""
    d.mkdir(parents=True, exist_ok=True)
    gate = "+".join(f"between(t,{s},{min(e, 20.0)})" for _, s, e in SYNTH_WORDS)
    run([
        "ffmpeg", "-y", "-hide_banner",
        "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=60000/1001:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=210:sample_rate=96000:duration=20",
        "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=96000:amplitude=0.08:duration=20",
        "-filter_complex",
        f"[1:a]volume='if({gate},0.9,0)':eval=frame,tremolo=f=6:d=0.6[sp];"
        f"[sp][2:a]amix=inputs=2:normalize=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "96000", "-t", "20", str(d / "cut.mp4"),
    ])
    # 3.1 s 24 kHz mono "VO" (the CP2 VO length), syllable-gated tone.
    run([
        "ffmpeg", "-y", "-hide_banner",
        "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=3.1",
        "-af", "volume='0.3+0.6*gt(sin(2*PI*4*t),0)':eval=frame",
        "-ac", "1", "-c:a", "pcm_s16le", str(d / "vo.wav"),
    ])
    item = {
        "hook_text": "The craziest GTA 6 leak yet?",
        "speech_words": [{"word": w, "start": s, "end": e} for w, s, e in SYNTH_WORDS],
        "voiceover_script": SYNTH_VO_SCRIPT,
        "synthetic": True,
    }
    (d / "item.json").write_text(json.dumps(item, indent=2), encoding="utf-8")
    return item


def load_item(d: Path) -> dict:
    data = json.loads((d / "item.json").read_text(encoding="utf-8"))
    hook = data.get("hook_text") or data.get("hook") or "IS THIS REALLY GTA 6?"
    words = data.get("speech_words") or data.get("words") or data.get("transcript_words")
    if words is None:
        words = render.transcribe_words(d / "cut.mp4")
    vo_words = data.get("vo_words")
    script = data.get("voiceover_script") or data.get("vo_script") or ""
    if not vo_words:
        vo_words = render.transcribe_words(d / "vo.wav") if not data.get("synthetic") else []
    if not vo_words:
        vo_words = render.estimate_words_from_script(script or SYNTH_VO_SCRIPT, render.media_duration(d / "vo.wav"))
    norm = lambda ws: [{"word": w["word"], "start": float(w["start"]), "end": float(w["end"])} for w in ws]  # noqa: E731
    return {"hook": hook, "speech_words": norm(words), "vo_words": norm(vo_words)}


def grab(video: Path, t: float, out: Path) -> Image.Image:
    run(["ffmpeg", "-y", "-hide_banner", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", str(out)])
    return Image.open(out).convert("RGB")


def wave(video: Path, out: Path, w: int, h: int) -> Image.Image:
    run(["ffmpeg", "-y", "-hide_banner", "-i", str(video), "-filter_complex",
         f"aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors=0x33ccff", "-frames:v", "1", str(out)])
    return Image.open(out).convert("RGB")


def contact_sheet(d: Path, res: dict) -> Path:
    a, b = d / "A_v2.mp4", d / "B_v2.mp4"
    tmp = d / ".frames"
    tmp.mkdir(exist_ok=True)
    fz, dur_a = res["freeze_s"], res["probe_a"]["video_duration"]
    shots = [
        (a, 1.0, "A_v2 @ 1.0 s"),
        (b, fz / 2, f"B_v2 @ {fz / 2:.2f} s (freeze 0-{fz:.2f} s)"),
        (b, fz + 2.0, f"B_v2 @ {fz + 2.0:.2f} s (freeze end + 2 s)"),
        (a, dur_a - 0.25, f"A_v2 @ {dur_a - 0.25:.2f} s (last 0.5 s)"),
    ]
    tw, th = 540, 960
    font = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Black.ttf"), 22)
    small = ImageFont.truetype(str(ROOT / "assets/fonts/Montserrat-Bold.ttf"), 18)
    wave_h = 90
    sheet = Image.new("RGB", (tw * 4 + 50, th + 50 + 2 * (wave_h + 34) + 10), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    for i, (v, t, label) in enumerate(shots):
        im = grab(v, t, tmp / f"f{i}.png").resize((tw, th), Image.LANCZOS)
        x = 10 + i * (tw + 10)
        sheet.paste(im, (x, 40))
        draw.text((x, 10), label, fill=(255, 220, 0), font=font)
    y = th + 50
    full_w = tw * 4 + 30
    for v, name in ((a, "A_v2"), (b, "B_v2")):
        info = res["probe_a"] if name == "A_v2" else res["probe_b"]
        txt = f"{name} audio waveform (0 - {info['audio_duration']:.2f} s)"
        if name == "B_v2":
            txt += f"; freeze/VO ends at {fz:.2f} s, cut audio continues to the end"
        draw.text((10, y), txt, fill=(220, 220, 220), font=small)
        sheet.paste(wave(v, tmp / f"w_{name}.png", full_w, wave_h), (10, y + 26))
        if name == "B_v2":
            xf = 10 + int(full_w * fz / info["audio_duration"])
            draw.line([(xf, y + 26), (xf, y + 26 + wave_h)], fill=(255, 60, 60), width=3)
        y += wave_h + 34
    out = d / "contact_sheet.png"
    sheet.save(out, optimize=True)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    return out


def measure_cap_heights(cfg: dict, work: Path) -> dict:
    """Render 'H' in each configured style (outline/shadow 0) and measure the ink height in px."""
    rs = render.render_settings(cfg)
    work.mkdir(parents=True, exist_ok=True)
    out: dict[str, float] = {}
    for style in ("hook", "caption"):
        size = rs[style]["font_size"]
        ass = work / f"cap_{style}.ass"
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: S,{rs['font_name']},{size},&H00FFFFFF,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,S,,0,0,0,,{\\pos(540,960)}HEH\n",
            encoding="utf-8",
        )
        png = work / f"cap_{style}.png"
        fonts = render.escape_filter_path(render._abs_project_path(rs["fonts_dir"]))
        run(["ffmpeg", "-y", "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=0.1",
             "-vf", f"ass=filename='{render.escape_filter_path(ass)}':fontsdir='{fonts}'", "-frames:v", "1", str(png)])
        g = np.array(Image.open(png).convert("L"))
        ys = np.where((g > 128).any(axis=1))[0]
        out[style] = int(ys.max() - ys.min() + 1) if len(ys) else 0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="ignore samples/cp2 and use a synthetic clip")
    ap.add_argument("--out", default=str(ROOT / "samples"), help="root for synthetic output")
    args = ap.parse_args()
    cfg = config.get_config()

    pack = ROOT / "samples" / "cp2"
    items: list[Path] = []
    if not args.synthetic and pack.is_dir():
        items = sorted(p for p in pack.iterdir() if (p / "cut.mp4").exists() and (p / "vo.wav").exists())
    synthetic = not items
    if synthetic:
        d = Path(args.out) / "synthetic"
        make_synthetic_item(d)
        items = [d]
        print("NOTE: no sample pack found; verifying on a SYNTHETIC clip. Real-clip verification must run on the laptop.")

    caps = measure_cap_heights(cfg, items[0] / ".caps")
    for f in (items[0] / ".caps").iterdir():
        f.unlink()
    (items[0] / ".caps").rmdir()

    failed = False
    rows = []
    for d in items:
        it = load_item(d)
        try:
            res = render.render_item_variants(
                d / "cut.mp4", d / "vo.wav", it["hook"], it["speech_words"], it["vo_words"],
                d / "A_v2.mp4", d / "B_v2.mp4", d / ".subs", cfg=cfg, name=d.name,
            )
        except render.RenderVerificationError as e:
            print(f"FAIL {d.name}: {e}")
            failed = True
            continue
        for k in ("ass_a", "ass_b"):
            Path(res[k]).unlink(missing_ok=True)
        (d / ".subs").rmdir()
        sheet = contact_sheet(d, res)
        b_audio_after_freeze = res["probe_b"]["audio_duration"] - res["freeze_s"]
        for k in ("probe_a", "probe_b"):
            res[k]["path"] = Path(res[k]["path"]).name
        res.pop("ass_a"), res.pop("ass_b")
        report = {**res, "cap_heights_px": caps, "b_audio_after_freeze_s": round(b_audio_after_freeze, 3),
                  "synthetic": synthetic}
        (d / "render_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        for name, p in (("A_v2", res["probe_a"]), ("B_v2", res["probe_b"])):
            rows.append((d.name, name, p["video_duration"], p["audio_duration"], p["fps"], p["sample_rate"],
                         p["channels"], f"{p['width']}x{p['height']}"))
        print(f"{d.name}: cut={res['cut_duration_s']}s end_snapped={res['end_s']}s vo={res['vo_duration_s']}s "
              f"freeze={res['freeze_s']}s  B audio after freeze={b_audio_after_freeze:.2f}s  sheet={sheet}")

    print("\n| item | output | video s | audio s | fps | sample rate | ch | size |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]:.3f} | {r[3]:.3f} | {r[4]:g} | {r[5]} | {r[6]} | {r[7]} |")
    print(f"\nMeasured cap heights: hook {caps['hook']} px, captions {caps['caption']} px")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
