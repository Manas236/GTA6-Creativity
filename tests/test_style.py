"""Zero-network unit tests for the style layer (presets, planning, ASS tags, audio math).

Nothing here touches the network and nothing runs ffmpeg: every decision the style layer
makes is reachable from plain data, which is the point of keeping planning
(pipeline.plan / edits / style) separate from the filtergraph (pipeline.render).
"""
from __future__ import annotations

import re

import pytest

from pipeline import audio_mix, config, edits, keywords as kwmod, overlay, plan as planning, render, style
from pipeline.rendercore import cap_height_px, render_settings

RS = render_settings()
SS = style.style_settings()
SA = RS["safe_area"]
GEO = style.fill_geometry(RS, SS)

WORDS = [
    {"word": "LOOK", "start": 0.5, "end": 0.9},
    {"word": "AT", "start": 1.0, "end": 1.2},
    {"word": "VICE", "start": 1.3, "end": 1.8},
    {"word": "CITY", "start": 1.9, "end": 2.4},
    {"word": "CARS", "start": 3.0, "end": 3.5},
    {"word": "MONEY", "start": 8.0, "end": 8.6},
]


def _plan(preset="P3", duration=12.0, words=None, **kw):
    return planning.plan_from_words(
        "VICE CITY LOOKS INSANE", duration, words if words is not None else WORDS,
        preset=preset, channel_name="GamingBolt",
        keyword_list=["VICE", "CITY", "CARS", "MONEY", "LEAK"], **kw,
    )


# ============================================================ presets and flags

def test_presets_are_nested_and_default_is_p0():
    flags = {p: style.resolve_preset(p) for p in ("P0", "P1", "P2", "P3")}
    assert style.default_preset() == "P0", "the pipeline default preset must stay P0"
    # P0 is the control: current renders + F1 only.
    assert [f for f, on in flags["P0"].items() if on] == ["zoom_fill"]
    for lo, hi in (("P0", "P1"), ("P1", "P2"), ("P2", "P3")):
        on_lo = {f for f, v in flags[lo].items() if v}
        on_hi = {f for f, v in flags[hi].items() if v}
        assert on_lo < on_hi, f"{hi} must be a strict superset of {lo}"
    assert {f for f, v in flags["P1"].items() if v} - {f for f, v in flags["P0"].items() if v} == {
        "keyword_color", "keyword_emoji", "progress_bar", "credit_tag",
        "caption_pop", "dead_air", "loop_end",
    }
    assert {f for f, v in flags["P2"].items() if v} - {f for f, v in flags["P1"].items() if v} == {
        "hook_pop", "punch_ins", "sfx",
    }
    assert {f for f, v in flags["P3"].items() if v} - {f for f, v in flags["P2"].items() if v} == {
        "slow_push", "music_bed",
    }
    assert set(flags["P3"]) == set(style.STYLE_FLAGS)


def test_unknown_preset_and_flag_are_rejected():
    with pytest.raises(KeyError):
        style.resolve_preset("P9")
    cfg = {**config.get_config(), "style": {"presets": {"X": {"base": None, "enable": ["nope"]}}}}
    with pytest.raises(KeyError):
        style.resolve_preset("X", cfg)


def test_every_number_comes_from_config():
    """Each flag has a settings block, and config.yaml overrides the code default."""
    cfg = config.get_config(reload=True)
    for flag in style.STYLE_FLAGS:
        assert flag in SS and isinstance(SS[flag], dict), flag
        assert flag in (cfg.get("style") or {}), f"style.{flag} missing from config.yaml"
    custom = {**cfg, "style": {**cfg["style"], "punch_ins": {**cfg["style"]["punch_ins"], "zoom": 1.5}}}
    assert style.style_settings(custom)["punch_ins"]["zoom"] == 1.5
    assert style.style_settings(custom)["punch_ins"]["min_gap_s"] == SS["punch_ins"]["min_gap_s"]


# ============================================================ F1: per-segment decision

def test_f1_segment_decision_static_vs_moving():
    """Low frame difference (a text card) keeps the fit; moving gameplay gets the fill."""
    thresh = SS["zoom_fill"]["motion_threshold"]
    motion = [(t / 5, 0.0004) for t in range(0, 40)]          # 0-8 s: static card
    motion += [(8 + t / 5, 0.09) for t in range(0, 70)]       # 8-22 s: moving
    segs = edits.plan_segments(22.0, [8.0, 12.0, 16.0], motion, SS)
    assert [s.mode for s in segs] == ["fit", "fill"], segs
    assert segs[0].start == 0.0 and segs[0].end == pytest.approx(8.0)
    assert segs[1].end == pytest.approx(22.0)
    assert segs[0].motion < thresh <= segs[1].motion
    # Adjacent same-mode segments merge, so three moving scenes stay one segment.
    assert len(edits.plan_segments(22.0, [8.0, 12.0, 16.0], [(t / 5, 0.09) for t in range(110)], SS)) == 1


def test_f1_segments_split_only_on_real_scene_cuts():
    motion = [(t / 5, 0.0002 if t / 5 < 6 else 0.07) for t in range(0, 100)]
    segs = edits.plan_segments(20.0, [6.0, 6.3, 6.5], motion, SS)   # cuts closer than min_segment_s
    assert len(segs) == 2 and segs[0].end == pytest.approx(6.0)


def test_f1_geometry_fill_is_a_centre_crop_of_the_fit():
    """zoom 1.0 is the full-width fit, zoom fill_zoom fills the band with the sides cropped."""
    assert (GEO.out_w, GEO.fit_h, GEO.fill_h) == (1080, 608, 852)
    assert GEO.fill_h / GEO.fit_h == pytest.approx(SS["zoom_fill"]["fill_scale"], abs=0.005)
    assert GEO.fill_zoom == pytest.approx(GEO.canvas_h / GEO.content_h)
    assert GEO.fill_zoom == pytest.approx(GEO.fill_h / GEO.fit_h, abs=0.002)
    assert GEO.band("fill") == (534, 1386) and GEO.band("fit") == (656, 1264)
    # The fill crops the sides only: at fill_zoom the visible width is 1/1.4 of the frame.
    assert GEO.canvas_w / GEO.fill_zoom / GEO.canvas_w == pytest.approx(1 / GEO.fill_zoom)
    assert GEO.edges(["fill", "fit"]) == [534, 656, 1264, 1386]


def test_f1_zoom_program_crossfades_between_modes():
    segs = [edits.Segment(0.0, 8.0, "fit"), edits.Segment(8.0, 20.0, "fill")]
    prog = edits.build_zoom_program(segs, [], GEO.fill_zoom, SS, zoom_fill=True)
    cf = SS["zoom_fill"]["crossfade_s"]
    assert edits.zoom_value(prog, 4.0) == pytest.approx(1.0)
    assert edits.zoom_value(prog, 15.0) == pytest.approx(GEO.fill_zoom)
    mid = edits.zoom_value(prog, 8.0)
    assert 1.0 < mid < GEO.fill_zoom                      # mid cross-scale
    assert edits.zoom_value(prog, 8.0 - cf / 2 - 0.01) == pytest.approx(1.0, abs=1e-3)
    assert edits.zoom_value(prog, 8.0 + cf / 2 + 0.01) == pytest.approx(GEO.fill_zoom, abs=1e-3)
    assert not prog.is_constant
    assert "it" in edits.zoom_expr(prog) and "gte(it," in edits.zoom_expr(prog)


def test_f1_single_fill_segment_skips_zoompan():
    prog = edits.build_zoom_program([edits.Segment(0.0, 20.0, "fill")], [], GEO.fill_zoom, SS, zoom_fill=True)
    assert prog.is_constant and prog.constant_value == pytest.approx(GEO.fill_zoom)
    assert edits.zoom_expr(prog) == f"{GEO.fill_zoom:.6f}"


# ============================================================ F1: safe-area placement

def test_safe_area_placement_after_f1():
    """For every framing mode the clip uses, text stays in the safe area and off the edges."""
    clearance = SS["zoom_fill"]["edge_clearance_px"]
    for modes in (["fill"], ["fit"], ["fill", "fit"]):
        L = style.plan_layout(modes, 150, 2, RS, SS, cap_height_px)
        edges = GEO.edges(modes)
        assert L.video_edges == edges
        for name, (y0, y1) in (("caption", L.caption_ink), ("hook", L.hook_ink), ("credit", L.credit_ink)):
            assert SA["y_min"] <= y0 and y1 <= SA["y_max"], (modes, name, (y0, y1))
        # Captions and the hook never straddle a video edge.
        for name, (y0, y1) in (("caption", L.caption_ink), ("hook", L.hook_ink)):
            for e in edges:
                assert y1 <= e - clearance or y0 >= e + clearance, (modes, name, e, (y0, y1))
        # The hook stays above the highest video edge, captions below the hook.
        assert L.hook_ink[1] <= min(edges) - clearance
        assert L.caption_ink[0] > L.hook_ink[1]
        # C4 never overlaps the captions.
        gap = SS["credit_tag"]["gap_from_caption_px"]
        assert (L.credit_ink[0] >= L.caption_ink[1] + gap) or (L.credit_ink[1] <= L.caption_ink[0] - gap)
        assert L.credit_x >= SA["x_min"]


def test_safe_area_prefers_the_configured_caption_y_when_it_is_valid():
    fill_only = style.plan_layout(["fill"], 150, 2, RS, SS, cap_height_px)
    assert fill_only.caption_center_y == RS["caption"]["center_y"] == 1300
    mixed = style.plan_layout(["fill", "fit"], 150, 2, RS, SS, cap_height_px)
    assert mixed.caption_center_y != 1300 and any("captions moved" in n for n in mixed.notes)
    # It is the NEAREST valid centre, so it stays as low in frame as the edges allow.
    assert mixed.caption_center_y == 1183


def test_hook_lines_have_enough_spacing_that_outlines_cannot_touch():
    """M/F1: line pitch must exceed one line's ink height, or the outlines collide."""
    size = RS["hook"]["font_size"]
    cap = cap_height_px(size, RS["font_path"])
    pitch = cap * RS["hook"]["line_spacing"]
    ink = 2 * style.ink_half_height(cap, RS["hook"]["outline"], RS["hook"]["shadow"])
    assert pitch - ink >= 20, f"only {pitch - ink:.1f}px between the hook lines' outlines"
    assert RS["hook"]["line_spacing"] >= 1.7


# ============================================================ C1 keyword colour

def test_c1_keyword_colour_green_keywords_yellow_spoken_word():
    sp = _plan("P1")
    ass = render.build_ass_subtitles("VICE CITY LOOKS INSANE", sp.total_s, sp.words, sp=sp)
    green = SS["keyword_color"]["color"]
    yellow = RS["caption"]["highlight_color"]
    white = RS["caption"]["color"]
    assert green != yellow and green == "&H0000FF00"
    caps = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    assert caps
    # The spoken word is yellow even when it is a keyword; other keywords in the same
    # chunk are green; non-keywords keep the base colour (no override emitted).
    vice_active = [l for l in caps if re.search(r"\{\\c" + re.escape(yellow) + r"&\}VICE", l)]
    vice_passive = [l for l in caps if re.search(r"\{\\c" + re.escape(green) + r"&\}VICE", l)]
    assert vice_active, "spoken VICE must be yellow"
    assert vice_passive, "non-spoken keyword VICE must be green"
    assert f"{{\\c{green}&}}AT" not in ass and f"{{\\c{yellow}&}}AT" in ass
    assert white in ass


def test_c1_keyword_colour_off_in_p0():
    sp = _plan("P0")
    ass = render.build_ass_subtitles("HOOK", sp.total_s, sp.words, sp=sp)
    assert SS["keyword_color"]["color"] not in ass
    assert RS["caption"]["highlight_color"] in ass        # yellow spoken word still there


def test_c1_rule_fallback_picks_numbers_and_gta_terms():
    words = [{"word": w, "start": i, "end": i + 0.3} for i, w in
             enumerate(["THE", "LUCIA", "TRAILER", "SHOWS", "2026", "AND", "A", "BIG", "MAP", "OF", "VICE"])]
    found = kwmod.rule_keywords(words)
    assert "LUCIA" in found and "2026" in found and "MAP" in found and "VICE" in found
    assert "SHOWS" not in found and "THE" not in found
    assert len(found) <= SS["keyword_color"]["max_keywords"]
    assert kwmod.rule_keywords("gta 6 leak in vice city")[:3] == ["GTA", "6", "LEAK"]


def test_c1_gemini_keywords_are_cleaned_and_capped():
    out = kwmod.clean_keywords(["vice", "City!", "vice", "a b", "", None])
    assert out[:4] == ["VICE", "CITY", "A", "B"]
    assert len(kwmod.clean_keywords([f"w{i}" for i in range(20)])) == SS["keyword_color"]["max_keywords"]


# ============================================================ C2 emoji rate limit

def test_c2_emoji_rate_limit_one_per_min_gap():
    gap = SS["keyword_emoji"]["min_gap_s"]
    assert gap == 4.0
    words = [{"word": "CAR", "start": t, "end": t + 0.4} for t in (0.5, 1.0, 2.0, 3.9, 4.6, 9.0, 9.5)]
    cues = overlay.plan_emoji_cues(words, ["CAR"], SS, duration=20.0)
    assert [c.at for c in cues] == [0.5, 4.6, 9.0]
    for a, b in zip(cues, cues[1:]):
        assert b.at - a.at >= gap
    assert all(c.codepoint == "1f697" for c in cues)


def test_c2_only_mapped_keywords_with_a_committed_png():
    words = [{"word": w, "start": i * 5.0, "end": i * 5.0 + 0.4} for i, w in enumerate(["CARS", "SOFA", "MONEY"])]
    cues = overlay.plan_emoji_cues(words, ["CARS", "SOFA", "MONEY"], SS, duration=30.0)
    assert [c.keyword for c in cues] == ["CARS", "MONEY"]      # SOFA is not in the map
    for cue in cues:
        assert overlay.emoji_path(cue.codepoint, SS).exists(), cue
    # Every codepoint in the config map must be committed (and nothing else needed).
    for code in set(overlay.emoji_map(SS).values()):
        assert overlay.emoji_path(code, SS).exists(), code


def test_c2_emoji_sits_just_above_the_caption_and_pops_in():
    sp = _plan("P1")
    cues = overlay.plan_emoji_cues(sp.words, sp.keyword_list, SS, sp.total_s)
    assert cues
    inputs, chain = overlay.emoji_filter(cues, sp.layout, SS, RS, "vass", "vov")
    size, rise = SS["keyword_emoji"]["size_px"], SS["keyword_emoji"]["rise_px"]
    y_rest = sp.layout.caption_ink[0] - SS["keyword_emoji"]["gap_above_caption_px"] - size
    assert len(inputs) == 2 * len(cues) and chain.endswith("[vov]")
    assert f"scale={size}:{size}" in chain and f"y='{y_rest}+{rise}" in chain
    assert y_rest > RS["safe_area"]["y_min"]
    assert y_rest + size <= sp.layout.caption_ink[0]
    for cue in cues:
        assert f"enable='between(t,{cue.at:.3f},{cue.at + cue.hold:.3f})'" in chain


# ============================================================ C3 progress bar

def test_c3_progress_bar_fills_over_the_clip_at_the_top_edge():
    sp = _plan("P1", duration=20.0)
    ass = render.build_ass_subtitles("HOOK", sp.total_s, sp.words, sp=sp)
    bar = [l for l in ass.splitlines() if l.startswith("Dialogue: 3,")]
    assert len(bar) == 1
    line = bar[0]
    h, w = SS["progress_bar"]["height_px"], RS["width"]
    assert "Style: Bar," in ass
    assert "\\an7\\pos(0,0)" in line                                   # the very top edge
    assert f"m 0 0 l {w} 0 {w} {h} 0 {h}" in line                      # 1080 x 10 rectangle
    assert "\\fscx0" in line and f"\\t(0,{int(round(sp.total_s * 1000))},\\fscx100)" in line
    assert line.startswith(f"Dialogue: 3,0:00:00.00,{render.format_ass_timestamp(sp.total_s)}")
    assert h == 10
    # Off in P0.
    assert "Dialogue: 3," not in render.build_ass_subtitles("HOOK", 20.0, WORDS, sp=_plan("P0", 20.0))


# ============================================================ C4 credit tag

def test_c4_credit_tag_text_size_and_opacity():
    sp = _plan("P1")
    ass = render.build_ass_subtitles("HOOK", sp.total_s, sp.words, sp=sp)
    ev = [l for l in ass.splitlines() if l.startswith("Dialogue: 2,")]
    assert len(ev) == 1 and "via GamingBolt" in ev[0]
    assert "\\alpha&H33&" in ev[0]                                     # ~80% opacity
    assert f"\\an1\\pos({sp.layout.credit_x},{sp.layout.credit_y})" in ev[0]
    style_line = next(l for l in ass.splitlines() if l.startswith("Style: Credit,"))
    size = float(style_line.split(",")[2])
    assert cap_height_px(size, RS["font_path"]) == pytest.approx(32, abs=1.5)
    assert "Dialogue: 2," not in render.build_ass_subtitles("HOOK", 12.0, WORDS, sp=_plan("P0"))


# ============================================================ M1 / M2 motion tags

def test_m1_caption_pop_tags():
    sp = _plan("P1")
    ass = render.build_ass_subtitles("HOOK", sp.total_s, sp.words, sp=sp)
    cp = SS["caption_pop"]
    assert (cp["from_scale"], cp["peak_scale"], cp["peak_ms"], cp["settle_ms"]) == (80, 115, 60, 160)
    expect = r"\fscx80\fscy80\t(0,60,\fscx115\fscy115)\t(60,160,\fscx100\fscy100)"
    assert expect in ass
    caps = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    # The pop fires once per caption chunk (on its first word), not on every word.
    chunks = len(render.chunk_caption_words(sp.words, RS))
    assert sum(1 for l in caps if expect in l) == chunks < len(caps)
    assert expect not in render.build_ass_subtitles("HOOK", 12.0, WORDS, sp=_plan("P0"))


def test_m2_hook_pop_and_shake_tags():
    sp = _plan("P2")
    ass = render.build_ass_subtitles("VICE CITY LOOKS INSANE", sp.total_s, sp.words, sp=sp)
    hp = SS["hook_pop"]
    hooks = [l for l in ass.splitlines() if l.startswith("Dialogue: 0,")]
    assert hooks
    line = hooks[0]
    assert f"\\fscx{hp['from_scale']}\\fscy{hp['from_scale']}" in line
    assert f"\\t(0,{hp['scale_ms']},\\fscx100\\fscy100)" in line       # scales in within 0.25 s
    assert hp["scale_ms"] <= 250 and hp["shake_ms"] <= 400
    # A decaying shake that returns to 0 inside shake_ms.
    shakes = re.findall(r"\\t\((\d+),(\d+),\\frz(-?[\d.]+)\)", line)
    assert len(shakes) == hp["shake_steps"]
    assert int(shakes[-1][1]) == hp["shake_ms"] and float(shakes[-1][2]) == 0.0
    amps = [abs(float(a)) for _, _, a in shakes]
    assert max(amps) <= hp["shake_deg"] and amps == sorted(amps, reverse=True)
    assert [float(a) > 0 for _, _, a in shakes][:2] == [True, False]    # alternating
    assert f"\\org(540,{sp.layout.hook_center_y})" in line             # both lines rotate together
    assert "\\frz" not in render.build_ass_subtitles("HOOK", 12.0, WORDS, sp=_plan("P1"))


# ============================================================ M3 punch-ins

def test_m3_punch_ins_spacing_and_envelope():
    loud = [(t / 10, -40.0) for t in range(0, 200)]
    for peak in (3.0, 6.0, 9.5, 10.0, 15.0):
        for i in range(5):
            loud[int((peak + i / 10) * 10)] = (peak + i / 10, -8.0)
    punches = edits.plan_punch_ins(20.0, loud, [4.0], SS)
    pz = SS["punch_ins"]
    assert punches and len(punches) <= pz["max_count"]
    for a, b in zip(punches, punches[1:]):
        assert b.at - a.at >= pz["min_gap_s"], punches
    for p in punches:
        assert p.at >= pz["first_at_least_s"] and p.end <= 20.0
        assert (p.ease_in, p.hold, p.ease_out) == (0.12, 0.8, 0.12)
        assert 0.6 <= p.hold <= 1.0
    prog = edits.build_zoom_program([edits.Segment(0.0, 20.0, "fill")], punches, GEO.fill_zoom, SS,
                                    zoom_fill=True, punch_ins=True)
    p = punches[0]
    base = GEO.fill_zoom
    assert edits.zoom_value(prog, p.at - 0.05) == pytest.approx(base)
    assert edits.zoom_value(prog, p.at + p.ease_in / 2) == pytest.approx(base * 1.06, abs=0.01)
    assert edits.zoom_value(prog, p.at + p.ease_in + 0.4) == pytest.approx(base * pz["zoom"])
    assert edits.zoom_value(prog, p.end + 0.05) == pytest.approx(base)
    assert "cos(PI*(it-" in edits.zoom_expr(prog)                      # eased, not a step


def test_m3_no_punch_in_a_clip_too_short_to_hold_one():
    assert edits.plan_punch_ins(1.6, [(0.0, -10.0)], [], SS) == []


# ============================================================ M4 slow push

def test_m4_slow_push_across_static_segments_and_the_b_freeze():
    segs = [edits.Segment(0.0, 3.0, "fit"), edits.Segment(3.0, 20.0, "fill")]
    prog = edits.build_zoom_program(segs, [], GEO.fill_zoom, SS, zoom_fill=True, slow_push=True)
    to = SS["slow_push"]["zoom_to"]
    assert to == 1.06
    assert edits.zoom_value(prog, 0.0) == pytest.approx(1.0, abs=0.01)
    assert edits.zoom_value(prog, 2.8) == pytest.approx(1.0 + 0.06 * 2.8 / 3.0, abs=0.01)
    assert edits.zoom_value(prog, 10.0) == pytest.approx(GEO.fill_zoom)   # fill is unaffected
    # The Variant B freeze is a static segment, so it gets the push too.
    sp = planning.plan_from_words("HOOK", 10.0, WORDS, preset="P3", freeze_s=2.0,
                                  vo_words=[{"word": "VO", "start": 0.0, "end": 1.5}])
    assert sp.segments[0].mode == "fit" and sp.segments[0].end == pytest.approx(2.0)
    assert edits.zoom_value(sp.zoom, 1.9) > edits.zoom_value(sp.zoom, 0.0)


# ============================================================ A1 SFX

def test_a1_sfx_assets_exist_and_are_peak_normalized():
    files = audio_mix.sfx_files(SS)
    for name, p in files.items():
        assert p.exists(), f"{name} SFX missing ({p})"
    assert (audio_mix.sfx_dir(SS) / "LICENSES.md").exists()


def test_a1_sfx_sits_at_least_6db_under_the_speech():
    headroom = SS["sfx"]["headroom_db"]
    assert headroom >= 6.0
    gain = audio_mix.sfx_gain_db(speech_peak_db=-2.0, sfx_peak_db=-1.0, ss=SS)
    assert gain == pytest.approx(-2.0 - headroom - (-1.0))
    assert (-1.0 + gain) == pytest.approx(-2.0 - headroom)      # resulting SFX peak


def test_a1_boom_on_the_hook_then_a_whoosh_only_where_it_is_clear():
    punches = [edits.Punch(4.0, 0.8, 0.12, 0.12), edits.Punch(9.0, 0.8, 0.12, 0.12, rank=1)]
    cues = audio_mix.plan_sfx_cues(punches, SS, hook_at=0.0, duration=20.0)
    lead = SS["sfx"]["whoosh_lead_s"]
    # 3.94 s is under 5.0 s from the boom: that punch-in zooms silently
    assert [(c.kind, c.at) for c in cues] == [("boom", 0.0), ("whoosh", round(9.0 - lead, 3))]
    _, chain = audio_mix.sfx_chain(cues, {"boom": -10.0, "whoosh": -12.0}, 48000, "a_main", "a_sfx")
    assert "adelay=0:all=1" in chain and f"adelay={int(round((9.0 - lead) * 1000))}:all=1" in chain
    assert "amix=inputs=3:normalize=0:duration=first" in chain
    assert "sidechaincompress" not in chain


# ============================================================ A2 music bed

def test_a2_music_is_exactly_the_clip_length():
    sr = 48000
    for total in (19.3, 22.7, 15.0):
        inputs, chain = audio_mix.music_chain(total, [], SS, sr, "a_main", "a_mus",
                                              path=audio_mix.sfx_files(SS)["boom"])
        ns = int(round(total * sr))
        assert inputs and f"apad=whole_len={ns}" in chain and f"atrim=end_sample={ns}" in chain
        assert chain.index("apad") < chain.index("atrim")        # pad THEN trim = exact
        assert "sidechaincompress" not in chain
        assert ns == audio_mix.music_sample_count(total, sr)


def test_a2_music_ducks_under_the_words():
    spans = audio_mix.speech_spans(WORDS, SS)
    assert spans == [(0.5, 2.4), (3.0, 3.5), (8.0, 8.6)]        # gaps < 0.3 s merged
    mb = SS["music_bed"]
    assert (mb["level_db"], mb["duck_db"]) == (-24.0, -8.0)
    import math

    quiet = audio_mix.duck_gain(1.5, spans, SS)                 # mid-speech
    loud = audio_mix.duck_gain(6.0, spans, SS)                  # no speech
    assert 20 * math.log10(quiet / loud) == pytest.approx(mb["duck_db"], abs=0.01)
    assert 20 * math.log10(loud) == pytest.approx(mb["level_db"], abs=0.01)
    # Ramped, not switched.
    ramp = mb["duck_ramp_s"]
    assert loud > audio_mix.duck_gain(0.5 - ramp / 2, spans, SS) > quiet
    # The ffmpeg expression mirrors duck_gain at every probe point.
    expr = audio_mix.duck_expr(spans, SS)
    assert "sidechaincompress" not in expr and "eval=frame" not in expr
    for t in (0.0, 0.46, 1.5, 2.4, 2.6, 3.2, 6.0, 8.3, 9.0):
        assert _eval_ffmpeg_expr(expr, t) == pytest.approx(audio_mix.duck_gain(t, spans, SS), abs=1e-6)


def _eval_ffmpeg_expr(expr: str, t: float) -> float:
    """Evaluate the subset of ffmpeg expression syntax duck_expr emits."""
    py = re.sub(r"\bgte\(t,([-\d.]+)\)", r"(1.0 if t>=\1 else 0.0)", expr)
    py = re.sub(r"\blt\(t,([-\d.]+)\)", r"(1.0 if t<\1 else 0.0)", py)
    py = py.replace("min(1,", "min(1.0,")
    return float(eval(py, {"__builtins__": {}, "min": min}, {"t": float(t)}))


def test_a2_missing_bed_is_skipped_not_fatal(tmp_path):
    inputs, chain = audio_mix.music_chain(10.0, [], SS, 48000, "a_main", "a_mus",
                                          path=tmp_path / "nope.mp3")
    assert inputs == [] and chain == "[a_main]anull[a_mus]"
    sp = _plan("P3")
    assert sp.music_file is None or audio_mix.music_path(SS).exists()


# ============================================================ A3 dead-air removal

def _profiles(duration, silent_spans, still_spans, step=0.1):
    """Profiles shaped like the real probes: the loudness sample at t describes the
    window ENDING at t (ebur128), so it is shifted later by loudness_lag_s; plan_dead_air
    re-centres it."""
    lag = SS["dead_air"]["loudness_lag_s"]
    n = int(duration / step)
    loud = [(i * step, -70.0 if any(a + lag <= i * step < b + lag for a, b in silent_spans) else -18.0)
            for i in range(n)]
    motion = [(i * step, 0.0 if any(a <= i * step < b for a, b in still_spans) else 0.08) for i in range(n)]
    return loud, motion


def test_a3_cuts_only_quiet_and_motionless_stretches():
    loud, motion = _profiles(20.0, [(5.0, 6.2), (12.0, 13.0)], [(0.0, 8.0)])
    tl = edits.plan_dead_air(20.0, loud, motion, SS)
    assert len(tl.dropped) == 1, tl.dropped                     # 12-13 s is quiet but MOVING
    a, b = tl.dropped[0]
    assert 5.0 <= a < b <= 6.3 and (b - a) >= SS["dead_air"]["min_silence_s"]
    assert tl.total == pytest.approx(20.0 - (b - a), abs=0.01)
    assert tl.kept[0][0] == 0.0 and tl.kept[-1][1] == pytest.approx(20.0)


def test_a3_short_silences_are_kept():
    loud, motion = _profiles(20.0, [(5.0, 5.25)], [(0.0, 20.0)])
    assert edits.plan_dead_air(20.0, loud, motion, SS).dropped == []


def test_a3_never_drops_below_the_15s_floor():
    floor = SS["dead_air"]["min_output_s"]
    assert floor == 15.0
    # 8 s of dead air inside a 20 s clip: only ~5 s may go.
    loud, motion = _profiles(20.0, [(2.0, 6.0), (8.0, 12.0)], [(0.0, 20.0)])
    tl = edits.plan_dead_air(20.0, loud, motion, SS)
    assert tl.total >= floor
    assert len(tl.dropped) == 1                                 # the second span would breach the floor
    # An already-short clip is never shortened at all.
    loud, motion = _profiles(16.0, [(2.0, 8.0)], [(0.0, 16.0)])
    assert edits.plan_dead_air(16.0, loud, motion, SS).total >= floor


def test_a3_remap_keeps_captions_in_sync():
    tl = edits.Timeline(kept=[(0.0, 5.0), (6.0, 20.0)], source_duration=20.0, dropped=[(5.0, 6.0)])
    assert tl.total == pytest.approx(19.0)
    assert tl.remap(0.0) == (0.0, True)
    assert tl.remap(4.5)[0] == pytest.approx(4.5)                # before the cut: unchanged
    assert tl.remap(6.0)[0] == pytest.approx(5.0)                # after the cut: shifted back 1 s
    assert tl.remap(19.0)[0] == pytest.approx(18.0)
    assert tl.remap(5.5)[1] is False                             # inside the cut
    words = [{"word": "A", "start": 1.0, "end": 1.4},
             {"word": "GONE", "start": 5.2, "end": 5.6},
             {"word": "B", "start": 7.0, "end": 7.5},
             {"word": "C", "start": 19.0, "end": 19.4}]
    out = tl.remap_words(words)
    assert [w["word"] for w in out] == ["A", "B", "C"]           # the word inside the cut is dropped
    assert out[0]["start"] == pytest.approx(1.0)
    assert (out[1]["start"], out[1]["end"]) == (pytest.approx(6.0), pytest.approx(6.5))
    assert out[2]["start"] == pytest.approx(18.0)
    # Word durations survive the remap, so captions stay on their audio.
    for src, dst in zip([words[0], words[2], words[3]], out):
        assert dst["end"] - dst["start"] == pytest.approx(src["end"] - src["start"], abs=0.01)


def test_a3_plan_remaps_captions_and_the_b_freeze_offset_together():
    tl = edits.Timeline(kept=[(0.0, 4.0), (5.0, 18.0)], source_duration=18.0, dropped=[(4.0, 5.0)])
    sp = planning.plan_from_words(
        "HOOK", 18.0, [{"word": "LATE", "start": 8.0, "end": 8.4}], preset="P1",
        timeline=tl, freeze_s=2.0, vo_words=[{"word": "VO", "start": 0.0, "end": 1.0}],
    )
    assert sp.content_s == pytest.approx(17.0) and sp.total_s == pytest.approx(19.0)
    late = next(w for w in sp.words if w["word"] == "LATE")
    assert late["start"] == pytest.approx(8.0 - 1.0 + 2.0)       # -1 s dead air, +2 s freeze


# ============================================================ A4 loop-friendly end

def test_a4_loop_end_picks_the_frame_closest_to_frame_1():
    ref = 0x0F0F0F0F0F0F0F0F
    hashes = [(0.0, ref)]
    hashes += [(t / 5, ref ^ 0xFFFF) for t in range(1, 90)]      # 0.2-17.8 s: far from frame 1
    hashes.append((18.0, ref ^ 0x3))                             # 2 bits off  <- best match
    hashes.append((18.2, ref ^ 0xFF))
    hashes.append((19.0, ref ^ 0xF))                             # 4 bits off
    le = edits.choose_loop_end(hashes, 18.0, 19.5, [], SS, proposed_end=19.0)
    assert le is not None and le.end_s == pytest.approx(18.0) and le.distance == 2


def test_a4_loop_end_prefers_a_speech_pause():
    ref = 0
    hashes = [(0.0, ref), (18.0, 0b11111), (18.4, 0b1111111), (19.0, 0b111111111)]
    words = [{"word": "X", "start": 17.8, "end": 18.1}]          # 18.0 s is mid-word
    bonus = SS["loop_end"]["pause_bonus_bits"]
    assert bonus == 6
    le = edits.choose_loop_end(hashes, 18.0, 19.5, words, SS, proposed_end=18.0)
    assert le.end_s == pytest.approx(18.4) and le.in_pause is True   # 7-6 < 5-0
    # Without the pause penalty the nearest-hash frame wins.
    assert edits.choose_loop_end(hashes, 18.0, 19.5, [], SS, proposed_end=18.0).end_s == pytest.approx(18.0)


def test_a4_audio_fade_is_capped_and_there_is_no_video_fade():
    assert SS["loop_end"]["audio_fade_s"] <= 0.15
    assert _plan("P1").audio_fade_s == pytest.approx(0.15)
    assert _plan("P0").audio_fade_s == pytest.approx(RS["end_fade_s"])
    tail = render._audio_tail(RS, -14.0, 19.0, True, _plan("P1").audio_fade_s, "a_main")
    assert "afade=t=out:st=18.850:d=0.150" in tail
    assert "fade" not in render._fg_chain(RS, _plan("P1"), "vs2", "fg")


# ============================================================ graph assembly

def test_dead_air_spans_chain_is_frame_and_sample_exact():
    spans = [(0.0, 5.0), (6.0, 19.0)]
    v = render._spans_chain(spans, "vsrc", "vkept", audio=False)
    a = render._spans_chain(spans, "a_in", "a_kept", audio=True)
    assert "split=2" in v and v.count("trim=start=") == 2 and "concat=n=2:v=1:a=0" in v
    assert "asplit=2" in a and a.count("atrim=start=") == 2 and "concat=n=2:v=0:a=1" in a
    for ch in (v, a):
        assert "start=0.0000:end=5.0000" in ch and "start=6.0000:end=19.0000" in ch
    assert render._spans_chain([(0.0, 19.0)], "x", "y", audio=False) == "[x]null[y]"


def test_fg_chain_uses_crop_for_a_constant_zoom_and_zoompan_otherwise():
    const = render._fg_chain(RS, _plan("P0", 12.0, words=[]), "vs2", "fg")
    assert "zoompan" not in const and "crop=" in const and f"scale=1080:{GEO.fill_h}" in const
    assert "format=yuva420p" in const and "color=black@0" in const
    segs = [edits.Segment(0.0, 4.0, "fit"), edits.Segment(4.0, 12.0, "fill")]
    sp = planning.plan_from_words("HOOK", 12.0, WORDS, preset="P0", segments=segs)
    anim = render._fg_chain(RS, sp, "vs2", "fg")
    assert "zoompan=z='" in anim and "x='iw/2-(iw/zoom/2)'" in anim and "d=1" in anim
    assert f"s=1080x{GEO.fill_h}" in anim
    # Without F1 the foreground is the plain CP2 full-width fit.
    assert render._fg_chain(RS, None, "vs2", "fg") == "[vs2]scale=1080:608:force_original_aspect_ratio=decrease[fg]"


def test_video_chain_order_dead_air_then_freeze_then_overlays():
    tl = edits.Timeline(kept=[(0.0, 4.0), (5.0, 18.0)], source_duration=18.0, dropped=[(4.0, 5.0)])
    sp = planning.plan_from_words("HOOK", 18.0, WORDS, preset="P1", timeline=tl, freeze_s=1.5)
    chain = render._video_chain(RS, "f.ass", 510, 45, sp, "[vass][em0]overlay=x=1:y=2[vov]")
    assert chain.index("[vkept]") < chain.index("tpad=start=45")
    assert chain.index("ass=f.ass") < chain.index("[em0]overlay")
    assert chain.index("[em0]overlay") < chain.index("format=yuv420p")
    assert "trim=end_frame=555" in chain and chain.endswith("[v]")


def test_plan_summary_is_json_serializable():
    import json as _json
    _json.dumps(_plan("P3").summary(), default=str)
