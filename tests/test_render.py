"""Unit tests for Stage 7: subtitle generation and vertical 9:16 rendering (zero network)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from pipeline import config, render

ROOT = config.project_root()
RS = render.render_settings()
SA = RS["safe_area"]


# ----------------------------------------------------------------------------- helpers

def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True, capture_output=True)


def _make_cut(path: Path, seconds: float = 3.0, color: str | None = None,
              silences: list[tuple[float, float]] | None = None) -> Path:
    """Source-like cut: 16:9, 59.94 fps, 96 kHz audio running to the last frame (the CP2 inputs).
    `silences` mutes the tone over those spans (a speaker's pauses)."""
    video = f"color=c={color}:s=640x360:r=60000/1001:d={seconds}" if color else \
        f"testsrc=size=640x360:rate=60000/1001:duration={seconds}"
    mute = "+".join(f"between(t,{a},{b})" for a, b in silences or [])
    af = ["-af", f"volume='1-({mute})':eval=frame"] if mute else []
    _ffmpeg("-f", "lavfi", "-i", video,
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=96000:duration={seconds}",
            *af,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "96000", "-t", str(seconds), str(path))
    return path


def _make_vo(path: Path, seconds: float = 1.0) -> Path:
    _ffmpeg("-f", "lavfi", "-i", f"sine=frequency=880:sample_rate=24000:duration={seconds}",
            "-ac", "1", "-c:a", "pcm_s16le", str(path))
    return path


def _rms_window(path: Path, start: float, end: float) -> float:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{start}", "-to", f"{end}", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
        check=True, capture_output=True,
    ).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _frame(path: Path, t: float) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t}", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=270:480", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.uint8).astype(np.float32)


def _render_ass_frame(ass_text: str, tmp: Path, bg: str = "0x808080") -> np.ndarray:
    ass = tmp / "probe.ass"
    png = tmp / "probe.png"
    ass.write_text(ass_text, encoding="utf-8")
    fonts = render.escape_filter_path(render._abs_project_path(RS["fonts_dir"]))
    _ffmpeg("-f", "lavfi", "-i", f"color=c={bg}:s=1080x1920:d=0.1",
            "-vf", f"ass=filename='{render.escape_filter_path(ass)}':fontsdir='{fonts}'",
            "-frames:v", "1", str(png))
    return np.asarray(Image.open(png).convert("RGB")).astype(np.int16)


def _ink_bbox(img: np.ndarray, bg=(128, 128, 128), rows: slice | None = None) -> tuple[int, int, int, int]:
    """(x0, y0, x1, y1) of all pixels differing from the flat background (fill + outline + shadow)."""
    diff = np.abs(img - np.array(bg, dtype=np.int16)).sum(axis=2) > 24
    if rows is not None:
        mask = np.zeros_like(diff)
        mask[rows] = diff[rows]
        diff = mask
    ys, xs = np.where(diff)
    assert len(ys), "no text rendered"
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def _style(ass_text: str, name: str) -> list[str]:
    line = next(l for l in ass_text.splitlines() if l.startswith(f"Style: {name},"))
    return line[len("Style: "):].split(",")


SAMPLE_WORDS = [
    {"word": "LOOK", "start": 1.0, "end": 1.5},
    {"word": "AT", "start": 1.5, "end": 1.8},
    {"word": "THIS", "start": 1.8, "end": 2.2},
    {"word": "UNBELIEVABLE", "start": 2.3, "end": 2.9},
]


# ----------------------------------------------------------------------------- ASS content

def test_format_ass_timestamp():
    assert render.format_ass_timestamp(0.0) == "0:00:00.00"
    assert render.format_ass_timestamp(12.34) == "0:00:12.34"
    assert render.format_ass_timestamp(65.50) == "0:01:05.50"
    assert render.format_ass_timestamp(3661.05) == "1:01:01.05"


def test_build_ass_subtitles_no_speech():
    """No speech words -> hook events only."""
    ass_text = render.build_ass_subtitles("IS THIS REALLY GTA 6?", video_duration=20.0, words=[])
    assert "PlayResX: 1080" in ass_text and "PlayResY: 1920" in ass_text
    assert "Style: Hook" in ass_text
    dialogue = [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]
    dialogue_styles = [l.split(",")[3] for l in dialogue]
    assert dialogue_styles and set(dialogue_styles) == {"Hook"}

    # The two 2-line wraps are a near-tie (~1 px apart). Pillow on Windows ships without
    # libraqm and measures without GPOS kerning, Linux Pillow kerns, so the narrower split
    # can flip between platforms. Accept either split while they are within 2 px; beyond
    # that, the layout must pick the narrower one.
    lines = [re.sub(r"\{[^}]*\}", "", l.split(",", 9)[9]) for l in dialogue]
    size = RS["hook"]["font_size"]
    splits = [["IS THIS", "REALLY GTA 6?"], ["IS THIS REALLY", "GTA 6?"]]
    widths = [max(render.text_width_px(s, size, RS["font_path"]) for s in sp) for sp in splits]
    if abs(widths[0] - widths[1]) <= 2.0:
        assert lines in splits, lines
    else:
        assert lines == splits[widths.index(min(widths))], (lines, widths)


def test_build_ass_subtitles_with_speech_highlight_and_chunks():
    ass_text = render.build_ass_subtitles("OFFICIAL GTA 6 TRAILER", video_duration=20.0, words=SAMPLE_WORDS)
    assert r"{\c&H0000FFFF&}LOOK{\c&H00FFFFFF&}" in ass_text  # yellow active word, back to white
    captions = [l for l in ass_text.splitlines() if l.startswith("Dialogue: 1,")]
    assert len(captions) == len(SAMPLE_WORDS)  # one event per active word
    for line in captions:
        text = re.sub(r"\{[^}]*\}", "", line.split(",", 9)[9])
        assert 1 <= len(text.split()) <= int(RS["caption"]["max_words"])


def test_ass_styles_use_configured_sizes_and_font():
    ass_text = render.build_ass_subtitles("VICE CITY LOOKS INSANE", 10.0, SAMPLE_WORDS)
    hook, cap = _style(ass_text, "Hook"), _style(ass_text, "Caption")
    # Format: Name, Fontname, Fontsize, Primary, Secondary, Outline, Back, Bold, ..., Outline(16), Shadow(17)
    assert hook[1] == cap[1] == RS["font_name"] == "Montserrat Black"
    assert int(cap[2]) == RS["caption"]["font_size"]
    assert int(hook[2]) == RS["hook"]["font_size"]  # short hook: no shrink
    assert cap[3] == RS["caption"]["color"] == "&H00FFFFFF"
    assert hook[3] == RS["hook"]["color"] == "&H0000FFFF"
    assert float(cap[16]) == RS["caption"]["outline"] and 6 <= RS["caption"]["outline"] <= 8
    assert float(hook[16]) == RS["hook"]["outline"] and 7 <= RS["hook"]["outline"] <= 9
    assert float(cap[17]) > 0 and float(hook[17]) > 0  # small shadow
    assert (ROOT / RS["font_path"]).name == "Montserrat-Black.ttf"
    assert (ROOT / "assets/fonts/OFL.txt").exists()


def test_config_sizes_flow_through(monkeypatch):
    cfg = config.get_config(reload=True)
    custom = {**cfg, "render": {**cfg["render"], "caption": {**cfg["render"]["caption"], "font_size": 120}}}
    ass_text = render.build_ass_subtitles("HOOK", 5.0, SAMPLE_WORDS, cfg=custom)
    assert int(_style(ass_text, "Caption")[2]) == 120


def test_layout_hook_max_two_lines_inside_safe_width():
    long_hook = "Nobody noticed this tiny detail hidden in the new GTA 6 trailer"
    lines, size = render.layout_hook(long_hook, RS)
    assert 1 <= len(lines) <= 2
    max_w = (SA["x_max"] - SA["x_min"]) - 2 * (RS["hook"]["outline"] + RS["hook"]["shadow"])
    assert all(render.text_width_px(l, size, RS["font_path"]) <= max_w for l in lines)
    assert size < RS["hook"]["font_size"]  # had to shrink
    lines, size = render.layout_hook("IS THIS REAL?", RS)
    assert lines == ["IS THIS REAL?"] and size == RS["hook"]["font_size"]


def test_variant_b_words_offset_by_freeze():
    vo = [{"word": "IS", "start": 0.0, "end": 0.3}, {"word": "THIS", "start": 0.35, "end": 0.8}]
    sp = [{"word": "LOOK", "start": 1.0, "end": 1.4}]
    out = render.build_variant_b_words(vo, sp, freeze_s=1.3)
    assert [w["word"] for w in out] == ["IS", "THIS", "LOOK"]
    assert out[2]["start"] == pytest.approx(2.3) and out[2]["end"] == pytest.approx(2.7)
    assert all(w["end"] <= 1.3 for w in out[:2])


# ----------------------------------------------------------------------------- libass rendering

def test_libass_uses_black_weight(tmp_path: Path):
    """libass must render the static Black face, not fall back to Bold or a system font."""
    def ass_for(font: str, bold: int) -> str:
        return (
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: T,{font},100,&H00FFFFFF,&H00000000,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,T,,0,0,0,,{\\pos(540,960)}HHHH\n"
        )

    def ink(img: np.ndarray) -> int:
        return int((img.mean(axis=2) > 128).sum())

    libass_black = ink(_render_ass_frame(ass_for(RS["font_name"], 0), tmp_path, bg="black"))
    libass_bold = ink(_render_ass_frame(ass_for("Montserrat", 1), tmp_path, bg="black"))

    # Reference: rasterize HHHH directly from the TTF files at the same em size.
    em = render.em_px(100, RS["font_path"])

    def pil_ink(ttf: str) -> int:
        im = Image.new("L", (900, 300))
        ImageDraw.Draw(im).text((10, 10), "HHHH", font=ImageFont.truetype(str(ROOT / ttf), em), fill=255)
        return int((np.asarray(im) > 128).sum())

    ref_black = pil_ink("assets/fonts/Montserrat-Black.ttf")
    assert abs(libass_black - ref_black) / ref_black < 0.08
    assert libass_black > 1.2 * libass_bold


def test_cap_heights_match_targets(tmp_path: Path):
    """Measured cap heights: captions ~60-65 px, hook ~65-70 px."""
    measured = {}
    for key, style in (("caption", "Caption"), ("hook", "Hook")):
        ass_text = render.build_ass_subtitles("H", 1.0, [], cfg=None)
        size = RS[key]["font_size"]
        ass_text = re.sub(r"^Style: Hook,.*$",
                          f"Style: Hook,{RS['font_name']},{size},&H00FFFFFF,&H00000000,&H00000000,&H00000000,"
                          f"0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1", ass_text, flags=re.M)
        img = _render_ass_frame(ass_text, tmp_path, bg="black")
        ys = np.where((img.mean(axis=2) > 128).any(axis=1))[0]
        measured[key] = ys.max() - ys.min() + 1
    assert 60 <= measured["caption"] <= 65, measured
    assert 65 <= measured["hook"] <= 70, measured


def test_caption_and_hook_inside_instagram_safe_area(tmp_path: Path):
    words = [
        {"word": "UNBELIEVABLE", "start": 0.0, "end": 0.4},
        {"word": "WORLDBUILDING", "start": 0.45, "end": 0.9},
        {"word": "EVERYWHERE", "start": 0.95, "end": 1.5},
    ]
    long_hook = "Nobody noticed this tiny detail hidden in the GTA 6 trailer"
    ass_text = render.build_ass_subtitles(long_hook, 2.0, words)
    img = _render_ass_frame(ass_text, tmp_path)

    hook_box = _ink_bbox(img, rows=slice(0, 900))
    cap_box = _ink_bbox(img, rows=slice(900, 1920))
    for x0, y0, x1, y1 in (hook_box, cap_box):
        assert x0 >= SA["x_min"] and x1 <= SA["x_max"]
        assert y0 >= SA["y_min"] and y1 <= SA["y_max"]
    # Hook in the band above the video (video top edge is y=656).
    assert hook_box[1] >= RS["hook"]["band_top_y"] - 10 and hook_box[3] <= RS["hook"]["band_bottom_y"] + 20
    # Captions centred around y~1300, never below 1440.
    assert abs((cap_box[1] + cap_box[3]) / 2 - RS["caption"]["center_y"]) <= 20
    assert cap_box[3] <= 1440


# ----------------------------------------------------------------------------- verification guard

def test_verify_render_rejects_audio_shorter_than_video(tmp_path: Path):
    """The CP2 Variant B bug: 3.1 s of audio inside a longer video must FAIL verification."""
    bad = tmp_path / "bad.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ac", "2", str(bad))
    ok, info, errors = render.verify_render(bad)
    assert ok is False
    assert info["video_duration"] - info["audio_duration"] > 2.5
    assert any("durations differ" in e for e in errors)


def test_verify_render_rejects_wrong_fps_and_sample_rate(tmp_path: Path):
    bad = tmp_path / "bad_rates.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=60000/1001:d=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=96000:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-ar", "96000", "-ac", "2", str(bad))
    ok, _, errors = render.verify_render(bad)
    assert ok is False
    assert any(e.startswith("fps") for e in errors)
    assert any(e.startswith("sample rate") for e in errors)


def test_render_fails_when_verification_fails(tmp_path: Path, monkeypatch):
    cut = _make_cut(tmp_path / "cut.mp4", 1.0)
    ass = tmp_path / "a.ass"
    ass.write_text(render.build_ass_subtitles("HOOK", 1.0, []), encoding="utf-8")
    monkeypatch.setattr(render, "verify_render", lambda *a, **k: (False, {}, ["forced"]))
    ok, _ = render.render_variant_a(cut, ass, tmp_path / "out.mp4")
    assert ok is False


# ----------------------------------------------------------------------------- real renders

@pytest.fixture(scope="module")
def renders(tmp_path_factory) -> dict:
    d = tmp_path_factory.mktemp("renders")
    cut = _make_cut(d / "cut.mp4", 3.0)
    vo = _make_vo(d / "vo.wav", 1.0)
    freeze = render.freeze_duration(render.media_duration(vo))
    ass_a, ass_b = d / "a.ass", d / "b.ass"
    ass_a.write_text(render.build_ass_subtitles("TEST HOOK", 3.0, SAMPLE_WORDS), encoding="utf-8")
    words_b = render.build_variant_b_words([{"word": "VO", "start": 0.0, "end": 0.8}], SAMPLE_WORDS, freeze)
    ass_b.write_text(render.build_ass_subtitles("TEST HOOK B", freeze + 3.0, words_b), encoding="utf-8")
    ok_a, el_a = render.render_variant_a(cut, ass_a, d / "A.mp4")
    ok_b, el_b = render.render_variant_b(cut, vo, ass_b, d / "B.mp4", freeze_s=freeze)
    return {"dir": d, "ok_a": ok_a, "ok_b": ok_b, "el_a": el_a, "freeze": freeze,
            "A": d / "A.mp4", "B": d / "B.mp4"}


def test_variant_a_output_format_30fps_48k(renders):
    assert renders["ok_a"] is True and renders["el_a"] > 0
    info = render.probe_streams(renders["A"])
    assert (info["width"], info["height"]) == (1080, 1920)
    assert info["fps"] == pytest.approx(30.0)
    assert info["sample_rate"] == 48000 and info["channels"] == 2
    assert info["video_duration"] == pytest.approx(3.0, abs=0.05)


def test_variant_a_stream_durations_equal(renders):
    info = render.probe_streams(renders["A"])
    assert abs(info["video_duration"] - info["audio_duration"]) <= 0.1


def test_variant_a_fades_out_at_end(renders):
    a = renders["A"]
    body = _rms_window(a, 1.0, 2.0)
    tail = _rms_window(a, 2.93, 3.0)
    assert body > 0.01
    assert tail < 0.5 * body


def test_variant_b_freeze_then_cut_with_audio_after_freeze(renders):
    assert renders["ok_b"] is True
    freeze = renders["freeze"]
    assert freeze == pytest.approx(1.3, abs=1 / 30 + 1e-6)  # VO 1.0 s + 0.3 s pad
    info = render.probe_streams(renders["B"])
    # B duration = freeze + cut, audio and video streams equal.
    assert info["video_duration"] == pytest.approx(freeze + 3.0, abs=0.05)
    assert abs(info["video_duration"] - info["audio_duration"]) <= 0.1
    assert info["fps"] == pytest.approx(30.0) and info["sample_rate"] == 48000
    # Audio present well after the freeze (the CP2 bug: silence after the VO).
    assert _rms_window(renders["B"], freeze + 0.3, freeze + 2.5) > 0.01
    # VO audible during the freeze.
    assert _rms_window(renders["B"], 0.1, 0.9) > 0.01


def test_variant_b_first_frame_is_frozen(renders):
    b, freeze = renders["B"], renders["freeze"]
    f1, f2 = _frame(b, 0.1), _frame(b, freeze - 0.2)
    assert np.abs(f1 - f2).mean() < 1.0  # still image during the freeze
    g1, g2 = _frame(b, freeze + 0.5), _frame(b, freeze + 1.5)
    assert np.abs(g1 - g2).mean() > 1.0  # motion resumes after the freeze


def test_silent_cut_still_gets_matching_audio(tmp_path: Path):
    cut = tmp_path / "silent.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(cut))
    ass = tmp_path / "s.ass"
    ass.write_text(render.build_ass_subtitles("HOOK", 2.0, []), encoding="utf-8")
    ok, _ = render.render_variant_a(cut, ass, tmp_path / "out.mp4")
    assert ok is True
    ok_v, info, errors = render.verify_render(tmp_path / "out.mp4")
    assert ok_v, errors


def test_render_item_variants_snaps_end_to_pause(tmp_path: Path):
    """Full A+B pass on a 20 s cut: end snapped into the 19.1-19.5 s pause after the
    sentence-final "RIGHT." (a 0.4 s gap after a word WITHOUT . ? ! is not an end any
    more), B = freeze + A."""
    cut = _make_cut(tmp_path / "cut20.mp4", 20.0, color="navy", silences=[(19.15, 19.45)])  # a real pause
    vo = _make_vo(tmp_path / "vo.wav", 1.5)
    words = [{"word": "W", "start": float(t), "end": float(t) + 0.8} for t in range(0, 18)]
    words += [{"word": "RIGHT.", "start": 18.4, "end": 19.1}, {"word": "BOOM", "start": 19.5, "end": 20.4}]
    res = render.render_item_variants(
        cut, vo, "VICE CITY LOOKS INSANE", words, [{"word": "VO", "start": 0.0, "end": 1.4}],
        tmp_path / "A.mp4", tmp_path / "B.mp4", tmp_path / "subs",
    )
    assert 19.1 < res["end_s"] < 19.5
    pa, pb = res["probe_a"], res["probe_b"]
    assert pa["video_duration"] == pytest.approx(res["end_s"], abs=0.05)
    assert pb["video_duration"] == pytest.approx(res["freeze_s"] + res["end_s"], abs=0.05)
    for p in (pa, pb):
        assert abs(p["video_duration"] - p["audio_duration"]) <= 0.1


# ----------------------------------------------------------------------------- cached cut words

KEY = render.transcribe.cache_key()

CACHED_WORDS = [{"word": "VICE", "start": 0.5, "end": 0.9, "prob": 0.9},
                {"word": "CITY", "start": 0.9, "end": 1.3, "prob": 0.9}]


def _cached_item(cut_start=10.0, cut_end=30.0, model=None, words=CACHED_WORDS):
    import json
    payload = {"cut_start": 10.0, "cut_end": 30.0, "key": model or KEY, "words": words}
    return {"id": "item_r", "cut_start": cut_start, "cut_end": cut_end, "transcript_words": json.dumps(payload)}


def test_cached_cut_words_only_valid_for_the_same_cut_window_and_model():
    assert render.cached_cut_words(_cached_item(), KEY) == CACHED_WORDS
    assert render.cached_cut_words(_cached_item(cut_start=12.0), KEY) is None    # re-cut
    assert render.cached_cut_words(_cached_item(model="small.en|other"), KEY) is None  # other model / glossary
    assert render.cached_cut_words({"id": "x", "cut_start": 1, "cut_end": 2}, KEY) is None
    assert render.cached_cut_words({"transcript_words": "{not json"}, KEY) is None


def test_run_render_item_reuses_the_cached_cut_words(tmp_db: Path, tmp_path: Path):
    """The render stage transcribes only the VO; the cut words come from the hooks-stage cache."""
    from unittest.mock import patch

    from pipeline import db

    cut, vo = tmp_path / "cut.mp4", tmp_path / "vo.wav"
    cut.write_bytes(b"0")
    vo.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_r", "source": "youtube", "source_id": "vid_r",
                               "source_url": "https://example.invalid/r", "author_name": "Creator",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    from pipeline import binding

    seg = {"id": "item_r", "cut_path": str(cut), "cut_start": 10.0, "cut_end": 30.0}
    db.transition_stage("item_r", "cut", extra_fields={
        "cut_path": str(cut), "cut_start": 10.0, "cut_end": 30.0, "cut_duration": 20.0,
        "hook_text": "IS THIS VICE CITY?", "voiceover_script": "Is this Vice City?",
        "voiceover_audio_path": str(vo), "keywords": '["VICE", "CITY", "GTA"]',
        # made by the hooks / voiceover stages for exactly this segment
        "hook_binding": binding.hook_record(seg, "IS THIS VICE CITY?", "Is this Vice City?",
                                            binding.transcript_text(CACHED_WORDS), "segment_check"),
        "voiceover_binding": binding.voiceover_record(seg, "Is this Vice City?", vo),
    }, db_path=tmp_db)
    render.store_cut_words("item_r", db.get_item("item_r", db_path=tmp_db), CACHED_WORDS,
                           KEY, db_path=tmp_db)

    fake = {"elapsed_a_s": 1.0, "elapsed_b_s": 1.0, "probe_a": {"video_duration": 20.0},
            "probe_b": {"video_duration": 23.0}, "preset": "P0"}
    with patch.object(render.config, "media_dir", side_effect=lambda sub="": tmp_path / sub), \
         patch.object(render, "verify_video_file", return_value=(True, 20.0)), \
         patch.object(render, "media_duration", return_value=2.0), \
         patch.object(render, "transcribe_words", return_value=[]) as tw, \
         patch.object(render, "render_item_variants", return_value=fake) as riv:
        ok, msg, _ = render.run_render_item("item_r", db_path=tmp_db)

    assert ok, msg
    assert [Path(c.args[0]) for c in tw.call_args_list] == [vo]      # VO only, never the cut
    assert riv.call_args.args[3] == CACHED_WORDS                      # speech words = the cache
