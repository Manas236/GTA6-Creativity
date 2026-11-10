"""Style-layer tests that go through ffmpeg/libass: the pixels and samples, not the strings.

These prove the filtergraph actually does what the plan says - the F1 band really changes
size, the progress bar really fills, the hook outlines really do not touch, the music bed
really ducks - which string assertions alone cannot show. Zero network; small clips only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pipeline import audio_mix, config, edits, plan as planning, render, style
from pipeline.rendercore import render_settings

RS = render_settings()
SS = style.style_settings()
SA = RS["safe_area"]
GEO = style.fill_geometry(RS, SS)
ROOT = config.project_root()


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, capture_output=True)


def _stripe_source(path: Path, seconds: float = 3.0, silent: bool = False) -> Path:
    """16:9 source with fine vertical stripes: sharp in the foreground, blurred away in the
    background, so each row's variance tells us exactly where the video band is."""
    png = path.with_suffix(".png")
    a = np.zeros((1080, 1920, 3), dtype=np.uint8)
    a[:, ::8] = 255
    a[::8, :] = 200
    Image.fromarray(a).save(png)
    audio = "anullsrc=r=48000:cl=stereo" if silent else f"sine=frequency=300:sample_rate=48000:duration={seconds}"
    _ffmpeg("-loop", "1", "-t", str(seconds), "-i", str(png),
            "-f", "lavfi", "-i", audio,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", str(seconds), str(path))
    return path


def _frame(path: Path, t: float, tmp: Path) -> np.ndarray:
    out = tmp / f"f_{t:.2f}.png"
    _ffmpeg("-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(out))
    return np.asarray(Image.open(out).convert("L")).astype(np.float32)


def _band_rows(gray: np.ndarray, factor: float = 2.0) -> tuple[int, int]:
    """First and last row whose horizontal variance marks it as sharp foreground."""
    std = gray.std(axis=1)
    bg = float(np.median(np.concatenate([std[:300], std[-300:]])))
    rows = np.where(std > max(bg * factor, bg + 4.0))[0]
    assert len(rows), "no sharp foreground band found"
    return int(rows.min()), int(rows.max())


def _render_ass_frame(ass_text: str, tmp: Path, t: float = 0.0, bg: str = "0x808080", d: float = 1.0):
    ass, png = tmp / "probe.ass", tmp / f"probe_{t:.2f}.png"
    ass.write_text(ass_text, encoding="utf-8")
    fonts = render.escape_filter_path(render._abs_project_path(RS["fonts_dir"]))
    _ffmpeg("-f", "lavfi", "-i", f"color=c={bg}:s=1080x1920:r=10:d={d}",
            "-vf", f"ass=filename='{render.escape_filter_path(ass)}':fontsdir='{fonts}'",
            "-ss", f"{t:.3f}", "-frames:v", "1", str(png))
    return np.asarray(Image.open(png).convert("RGB")).astype(np.int16)


def _ink_bbox(img: np.ndarray, bg=(128, 128, 128), rows: slice | None = None):
    diff = np.abs(img - np.array(bg, dtype=np.int16)).sum(axis=2) > 24
    if rows is not None:
        mask = np.zeros_like(diff)
        mask[rows] = diff[rows]
        diff = mask
    ys, xs = np.where(diff)
    assert len(ys), "no text rendered"
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _rms(path: Path, start: float, end: float) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-to", f"{end}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        check=True, capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _peak(path: Path, start: float, end: float) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-to", f"{end}",
         "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "f32le", "-"],
        check=True, capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    return float(np.max(np.abs(a))) if a.size else 0.0


# ======================================================== F1 reaches the pixels

@pytest.fixture(scope="module")
def f1_renders(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("f1")
    cut = _stripe_source(d / "cut.mp4", 3.0)
    # No text at all: the band detector measures row variance, and burned-in text is
    # every bit as "sharp" as the foreground.
    ass = d / "a.ass"
    ass.write_text(render.build_ass_subtitles("", 3.0, []), encoding="utf-8")
    out: dict = {"dir": d}
    for mode in ("fit", "fill"):
        sp = planning.plan_from_words("HOOK", 3.0, [], preset="P0",
                                      segments=[edits.Segment(0.0, 3.0, mode)])
        assert sp.zoom.is_constant
        p = d / f"{mode}.mp4"
        ok, _ = render.render_variant_a(cut, ass, p, duration_s=3.0, sp=sp)
        out[mode] = (ok, p)
    # An animated fit -> fill change, to prove the zoom program drives zoompan.
    segs = [edits.Segment(0.0, 1.2, "fit"), edits.Segment(1.2, 3.0, "fill")]
    sp = planning.plan_from_words("HOOK", 3.0, [], preset="P0", segments=segs)
    assert not sp.zoom.is_constant
    p = d / "anim.mp4"
    ok, _ = render.render_variant_a(cut, ass, p, duration_s=3.0, sp=sp)
    out["anim"] = (ok, p)
    return out


def test_f1_fill_band_is_taller_than_the_fit_band(f1_renders):
    d = f1_renders["dir"]
    ok_fit, fit = f1_renders["fit"]
    ok_fill, fill = f1_renders["fill"]
    assert ok_fit and ok_fill
    t_fit, b_fit = _band_rows(_frame(fit, 1.0, d))
    t_fill, b_fill = _band_rows(_frame(fill, 1.0, d))
    assert abs(t_fit - GEO.band_top_fit) <= 12 and abs(b_fit - GEO.band_bottom_fit) <= 12, (t_fit, b_fit)
    assert abs(t_fill - GEO.band_top_fill) <= 12 and abs(b_fill - GEO.band_bottom_fill) <= 12, (t_fill, b_fill)
    # The fill really is ~1.4x taller, i.e. not the 1080x608 strip any more.
    assert (b_fill - t_fill) / (b_fit - t_fit) == pytest.approx(SS["zoom_fill"]["fill_scale"], abs=0.08)
    assert (b_fill - t_fill) > 800


def test_f1_animated_zoom_changes_the_band_over_time(f1_renders):
    d = f1_renders["dir"]
    ok, anim = f1_renders["anim"]
    assert ok
    early = _band_rows(_frame(anim, 0.5, d))      # still in the fit segment
    late = _band_rows(_frame(anim, 2.5, d))       # after the cross-scale, filling
    assert (late[1] - late[0]) - (early[1] - early[0]) > 150, (early, late)
    assert abs(early[0] - GEO.band_top_fit) <= 14
    assert abs(late[0] - GEO.band_top_fill) <= 14


def test_f1_fill_crops_the_sides_not_the_whole_frame(f1_renders):
    """The fill is full width: the foreground still spans the canvas horizontally."""
    d = f1_renders["dir"]
    _, fill = f1_renders["fill"]
    g = _frame(fill, 1.0, d)
    t, b = _band_rows(g)
    row = g[(t + b) // 2]
    sharp = np.where(np.abs(np.diff(row)) > 20)[0]
    assert sharp.min() < 40 and sharp.max() > 1040, (sharp.min(), sharp.max())


# ======================================================== safe area, hook spacing

def test_safe_area_placement_after_f1_in_real_libass(tmp_path: Path):
    """The re-placed layout must keep real rendered ink inside the safe area and off edges."""
    words = [
        {"word": "UNBELIEVABLE", "start": 0.0, "end": 0.4},
        {"word": "WORLDBUILDING", "start": 0.45, "end": 0.9},
    ]
    long_hook = "Nobody noticed this tiny detail hidden in the GTA 6 trailer"
    segs = [edits.Segment(0.0, 2.0, "fit"), edits.Segment(2.0, 4.0, "fill")]
    sp = planning.plan_from_words(long_hook, 4.0, words, preset="P1", segments=segs,
                                  channel_name="GamingBolt")
    assert sp.modes == ["fill", "fit"]
    img = _render_ass_frame(render.build_ass_subtitles(long_hook, 4.0, words, sp=sp), tmp_path, t=0.9)
    clearance = SS["zoom_fill"]["edge_clearance_px"]
    # Slice by the plan's own ink ranges: the C3 bar spans the full width at y 0-10 and
    # the C4 credit tag sits just under the captions.
    hook_box = _ink_bbox(img, rows=slice(SA["y_min"], sp.layout.hook_ink[1] + 20))
    cap_box = _ink_bbox(img, rows=slice(sp.layout.caption_ink[0] - 20, sp.layout.caption_ink[1] + 10))
    for x0, y0, x1, y1 in (hook_box, cap_box):
        assert x0 >= SA["x_min"] and x1 <= SA["x_max"], (x0, x1)
        assert y0 >= SA["y_min"] and y1 <= SA["y_max"], (y0, y1)
        for e in sp.layout.video_edges:
            assert y1 <= e - clearance or y0 >= e + clearance, (e, y0, y1)
    assert abs((cap_box[1] + cap_box[3]) // 2 - sp.layout.caption_center_y) <= 24
    assert abs(hook_box[1] - sp.layout.hook_ink[0]) <= 24


def test_hook_two_line_outlines_do_not_touch(tmp_path: Path):
    """There must be a clear background gap between the two hook lines."""
    long_hook = "Nobody noticed this tiny detail hidden in the GTA 6 trailer"
    sp = planning.plan_from_words(long_hook, 3.0, [], preset="P0")
    assert len(sp.hook_lines) == 2
    img = _render_ass_frame(render.build_ass_subtitles(long_hook, 3.0, [], sp=sp), tmp_path)
    ink = np.abs(img - 128).sum(axis=2) > 24
    rows = np.where(ink.any(axis=1))[0]
    band = ink[rows.min():rows.max() + 1].any(axis=1)
    empty = np.where(~band)[0]
    assert len(empty) >= 12, f"only {len(empty)} fully clear rows between the hook lines"
    # One contiguous gap, i.e. exactly two separated lines.
    assert np.all(np.diff(empty) == 1)


def test_credit_tag_renders_at_32px_inside_the_safe_area(tmp_path: Path):
    sp = planning.plan_from_words("HOOK", 3.0, [], preset="P1", channel_name="GamingBolt")
    img = _render_ass_frame(render.build_ass_subtitles("HOOK", 3.0, [], sp=sp), tmp_path)
    x0, y0, x1, y1 = _ink_bbox(img, rows=slice(1100, 1920))
    assert x0 >= SA["x_min"] and y1 <= SA["y_max"]
    assert x0 <= SA["x_min"] + 30                       # bottom-LEFT
    # The planned box must CONTAIN the real ink (the overlap guarantees depend on it).
    assert sp.layout.credit_ink[0] <= y0 and y1 <= sp.layout.credit_ink[1] + 1, (sp.layout.credit_ink, y0, y1)
    assert 36 <= (y1 - y0) <= 64                        # ~32 px caps + ascender/descender
    # Semi-transparent: the ink never reaches full white over the grey background.
    region = img[y0:y1 + 1, x0:x1 + 1]
    assert region.max() < 250


def test_progress_bar_fills_across_the_clip(tmp_path: Path):
    sp = planning.plan_from_words("HOOK", 4.0, [], preset="P1")
    ass = render.build_ass_subtitles("HOOK", 4.0, [], sp=sp)
    widths = []
    for t in (0.2, 2.0, 3.8):
        img = _render_ass_frame(ass, tmp_path, t=t, bg="black", d=4.0)
        ink = img.sum(axis=2) > 90
        ys, xs = np.where(ink[:40])
        assert len(xs), f"no bar at t={t}"
        widths.append(int(xs.max()) + 1)
        assert ys.min() == 0 and ys.max() <= SS["progress_bar"]["height_px"]
        assert xs.min() == 0                              # anchored at the left edge
    assert widths[0] < widths[1] < widths[2]
    assert widths[1] == pytest.approx(RS["width"] / 2, abs=60)
    assert widths[2] >= RS["width"] * 0.9


# ======================================================== A1 / A2 through ffmpeg

def test_a2_music_chain_ducks_8db_and_is_exactly_the_clip_length(tmp_path: Path):
    """The real `volume` automation, measured, with no loudnorm in the way."""
    total, sr = 6.0, 48000
    bed = tmp_path / "bed.wav"
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={sr}:duration=20",
            "-ac", "2", "-c:a", "pcm_s16le", str(bed))
    bed_rms = _rms(bed, 0.5, 1.5)
    words = [{"word": "A", "start": 2.0, "end": 3.0}]
    spans = audio_mix.speech_spans(words, SS)
    inputs, chain = audio_mix.music_chain(total, spans, SS, sr, "a_main", "a_mus", path=bed)
    chain = "[0:a]anull[a_main];" + render._splice_inputs(chain, len(inputs) // 2, 1, "a")
    out = tmp_path / "mix.wav"
    _ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo", *inputs,
            "-filter_complex", chain, "-map", "[a_mus]", "-t", str(total),
            "-ac", "2", "-ar", str(sr), "-c:a", "pcm_s16le", str(out))
    info = render.probe_streams(out)
    assert info["audio_duration"] == pytest.approx(total, abs=0.01)
    quiet = _rms(out, 2.2, 2.8)
    loud = _rms(out, 4.5, 5.5)
    assert 20 * np.log10(quiet / loud) == pytest.approx(SS["music_bed"]["duck_db"], abs=0.5)
    assert 20 * np.log10(loud / bed_rms) == pytest.approx(SS["music_bed"]["level_db"], abs=0.6)
    # Trimmed, not looped or left long: a 20 s bed in a 6 s clip ends with the clip.
    assert _rms(out, 5.5, 6.0) > 0.0


def test_a2_music_is_padded_when_the_bed_is_shorter_than_the_clip(tmp_path: Path):
    total, sr = 6.0, 48000
    bed = tmp_path / "short.wav"
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={sr}:duration=2",
            "-ac", "2", "-c:a", "pcm_s16le", str(bed))
    inputs, chain = audio_mix.music_chain(total, [], SS, sr, "a_main", "a_mus", path=bed)
    # a_main is the clip audio, so it is finite: amix duration=longest must then end
    # with the padded bed, not run on forever.
    chain = f"[0:a]atrim=end={total},asetpts=PTS-STARTPTS[a_main];" + \
        render._splice_inputs(chain, len(inputs) // 2, 1, "a")
    out = tmp_path / "mix.wav"
    _ffmpeg("-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=stereo", *inputs,
            "-filter_complex", chain, "-map", "[a_mus]", "-t", "8",
            "-ac", "2", "-ar", str(sr), "-c:a", "pcm_s16le", str(out))
    assert render.probe_streams(out)["audio_duration"] == pytest.approx(total, abs=0.02)


def test_a1_sfx_peaks_sit_at_least_6db_under_the_speech(tmp_path: Path):
    """The real mix, measured: the boom never comes within headroom_db of the speech.

    The punch-in sits at 6.3 s so its whoosh (6.24 s) is >= min_gap_s 5.0 after the boom
    (A1 density rule) and is actually placed."""
    sr = 48000
    speech = tmp_path / "speech.wav"
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=250:sample_rate={sr}:duration=8",
            "-af", "volume='0.7*between(t,1,2)+0.7*between(t,4,5)':eval=frame",
            "-ac", "2", "-c:a", "pcm_s16le", str(speech))
    speech_peak = audio_mix.file_peak_db(speech)
    cues = audio_mix.plan_sfx_cues([edits.Punch(6.3, 0.8, 0.12, 0.12)], SS, hook_at=0.0, duration=8.0)
    assert [c.kind for c in cues] == ["boom", "whoosh"]
    gains = {k: audio_mix.sfx_gain_db(speech_peak, audio_mix.file_peak_db(p, -1.0), SS)
             for k, p in audio_mix.sfx_files(SS).items()}
    inputs, chain = audio_mix.sfx_chain(cues, gains, sr, "a_main", "a_sfx")
    chain = render._splice_inputs(chain, len(inputs) // 2, 1, "a")
    out = tmp_path / "mix.wav"
    _ffmpeg("-i", str(speech), *inputs,
            "-filter_complex", f"[0:a]aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo[a_main];{chain}",
            "-map", "[a_sfx]", "-t", "8", "-ac", "2", "-ar", str(sr), "-c:a", "pcm_s16le", str(out))
    boom = _peak(out, 0.0, 0.7)                     # the hook's first frame
    whoosh = _peak(out, 6.2, 6.7)
    voice = _peak(out, 1.0, 2.0)
    headroom = SS["sfx"]["headroom_db"]
    for name, p in (("boom", boom), ("whoosh", whoosh)):
        assert p > 0.0, f"{name} inaudible"
        assert 20 * np.log10(p / voice) <= -headroom + 0.6, f"{name} only {20 * np.log10(p / voice):.1f} dB under"


# ======================================================== A3 end to end

def test_a3_real_render_drops_dead_air_and_shifts_the_captions(tmp_path: Path):
    """A 6 s still clip with a 1.5 s silent stretch renders ~1.3 s shorter, captions moved."""
    cut = tmp_path / "cut.mp4"
    png = tmp_path / "card.png"
    a = np.zeros((1080, 1920, 3), dtype=np.uint8)
    a[:, ::16] = 255
    Image.fromarray(a).save(png)
    _ffmpeg("-loop", "1", "-t", "6", "-i", str(png),
            "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=6",
            "-filter_complex", "[1:a]volume='1-between(t,2.0,3.8)-between(t,5.3,6.0)':eval=frame[a]",
            "-map", "0:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", "6", str(cut))

    cfg = config.get_config(reload=True)
    cfg = {
        **cfg,
        "cut": {**cfg["cut"], "min_duration": 2.0, "max_duration": 6.0, "target_duration": 6.0},
        "style": {**cfg["style"], "preset": "DA",
                  "presets": {**cfg["style"]["presets"], "DA": {"base": None, "enable": ["dead_air"]}},
                  "dead_air": {**cfg["style"]["dead_air"], "min_output_s": 3.0}},
    }
    words = [{"word": "EARLY", "start": 1.0, "end": 1.4}, {"word": "LATE", "start": 4.5, "end": 4.9}]
    a_dur = planning.analyze_clip(cut, words, cfg=cfg, preset="DA")
    assert a_dur.timeline.dropped, "nothing was detected as dead air"
    dropped = sum(e - s for s, e in a_dur.timeline.dropped)
    assert 1.0 <= dropped <= 1.8, a_dur.timeline.dropped
    assert a_dur.content_s >= cfg["style"]["dead_air"]["min_output_s"]

    sp = planning.build_style_plan(a_dur, "HOOK", cfg=cfg, preset="DA")
    late = next(w for w in sp.words if w["word"] == "LATE")
    assert late["start"] == pytest.approx(4.5 - dropped, abs=0.05)
    assert next(w for w in sp.words if w["word"] == "EARLY")["start"] == pytest.approx(1.0, abs=0.02)

    ass = tmp_path / "a.ass"
    ass.write_text(render.build_ass_subtitles("HOOK", sp.total_s, sp.words, cfg=cfg, sp=sp), encoding="utf-8")
    out = tmp_path / "out.mp4"
    ok, _ = render.render_variant_a(cut, ass, out, duration_s=sp.content_s, cfg=cfg, sp=sp)
    assert ok
    info = render.probe_streams(out)
    assert info["video_duration"] == pytest.approx(sp.content_s, abs=0.05)
    assert abs(info["video_duration"] - info["audio_duration"]) <= 0.1
    # The remaining audio is continuous: the silence was removed, not muted in place.
    assert _rms(out, 1.8, 2.6) > 0.02
