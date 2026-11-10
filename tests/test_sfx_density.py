"""A1 SFX density: the hook boom first, then a whoosh only where it is min_gap_s clear,
max_per_clip in all, measured on the FINAL output timeline (after A3's dead-air remap and
the Variant B freeze). Pure planning: no ffmpeg, no network."""
from __future__ import annotations

import copy

import pytest

from pipeline import audio_mix, config, edits, plan as planning, style

CFG = config.get_config()
SS = style.style_settings(CFG)
LEAD = SS["sfx"]["whoosh_lead_s"]


def _punch(at: float, rank: int = 0) -> edits.Punch:
    return edits.Punch(at, 0.8, 0.12, 0.12, rank=rank)


def _analysis(dead_air: edits.Timeline | None = None) -> planning.ClipAnalysis:
    """A 20 s cut whose loudness peaks sit 3 s apart at 3, 6, 9, 12 s (M3 takes all four,
    min_gap 3.0), with an optional A3 timeline."""
    loud = [(t / 10.0, -30.0) for t in range(0, 201)]
    for peak in (3.0, 6.0, 9.0, 12.0):
        loud[int(peak * 10)] = (peak, -8.0)
    tl = dead_air or edits.Timeline.identity(20.0)
    return planning.ClipAnalysis(
        cut_path="<memory>", duration=20.0, end_s=20.0, content_s=round(tl.total, 6), timeline=tl,
        segments=[edits.Segment(0.0, 20.0, "fill")], scene_cuts=[], loudness=loud, motion=[],
        speech_peak_db=-3.0,
    )


def _plans(analysis: planning.ClipAnalysis, cfg=CFG, freeze_s: float = 2.0):
    a = planning.build_style_plan(analysis, "HOOK", variant="A", freeze_s=0.0, cfg=cfg, preset="P2")
    b = planning.build_style_plan(analysis, "HOOK", variant="B", freeze_s=freeze_s, cfg=cfg, preset="P2",
                                  vo_words=[{"word": "VO", "start": 0.0, "end": 1.5}])
    return a, b


def test_four_punch_ins_three_seconds_apart_give_the_boom_and_at_most_one_whoosh():
    cues = audio_mix.plan_sfx_cues([_punch(2.0), _punch(5.0, 1), _punch(8.0, 2), _punch(11.0, 3)], SS,
                                   hook_at=0.0, duration=20.0)
    kinds = [c.kind for c in cues]
    assert kinds[0] == "boom" and kinds.count("whoosh") <= 1 and len(cues) <= SS["sfx"]["max_per_clip"]
    assert audio_mix.sfx_spacing_problems(cues, SS) == []


@pytest.mark.parametrize("with_dead_air", [False, True])
def test_spacing_holds_in_output_time_on_a_and_b(with_dead_air):
    tl = None
    if with_dead_air:
        # A3 dropped 1.0 s between the 2nd and 3rd peaks: in output time they are 2 s apart.
        tl = edits.Timeline(kept=[(0.0, 7.0), (8.0, 20.0)], source_duration=20.0, dropped=[(7.0, 8.0)])
    a, b = _plans(_analysis(tl))
    assert len(a.punches) == 4 and len(b.punches) == 4             # every punch-in still zooms
    for sp in (a, b):
        assert audio_mix.sfx_spacing_problems(sp.sfx_cues, SS) == [], sp.sfx_cues
        kinds = [c.kind for c in sp.sfx_cues]
        assert kinds[0] == "boom" and sp.sfx_cues[0].at == 0.0 and kinds.count("whoosh") <= 1
        # every whoosh belongs to a punch-in of THIS variant's output timeline
        for c in sp.sfx_cues[1:]:
            assert any(c.at == pytest.approx(max(0.0, p.at - LEAD)) for p in sp.punches)
    # B is A shifted by the freeze; the rule was applied on B's own timeline
    assert [p.at for p in b.punches] == pytest.approx([p.at + b.freeze_s for p in a.punches])
    ats = [p.at for p in a.punches]
    if with_dead_air:
        assert 11.0 in ats                                        # the 12.0 s peak, remapped by A3
    else:
        assert ats == [3.0, 6.0, 9.0, 12.0]


def test_the_whoosh_goes_to_the_strongest_clear_punch_in():
    # rank 0 at 5.5 s is the loudest; rank 1 at 12 s is also clear. The loudest wins.
    cues = audio_mix.plan_sfx_cues([_punch(12.0, 1), _punch(5.5, 0)], SS, hook_at=0.0, duration=20.0)
    assert [(c.kind, c.at) for c in cues] == [("boom", 0.0), ("whoosh", round(5.5 - LEAD, 3))]
    # a strong punch-in too close to the boom is skipped for the next clear one
    cues = audio_mix.plan_sfx_cues([_punch(3.0, 0), _punch(12.0, 1)], SS, hook_at=0.0, duration=20.0)
    assert [(c.kind, c.at) for c in cues] == [("boom", 0.0), ("whoosh", round(12.0 - LEAD, 3))]


def test_the_cap_and_the_gap_are_config():
    punches = [_punch(t, i) for i, t in enumerate((6.0, 12.0, 18.0))]
    ss1 = copy.deepcopy(SS)
    ss1["sfx"]["max_per_clip"] = 1
    assert [c.kind for c in audio_mix.plan_sfx_cues(punches, ss1, duration=20.0)] == ["boom"]

    ss4 = copy.deepcopy(SS)
    ss4["sfx"].update(max_per_clip=4, min_gap_s=5.0)
    cues = audio_mix.plan_sfx_cues(punches, ss4, duration=20.0)
    assert [c.kind for c in cues] == ["boom", "whoosh", "whoosh", "whoosh"]
    assert audio_mix.sfx_spacing_problems(cues, ss4) == []

    ss_gap = copy.deepcopy(SS)
    ss_gap["sfx"].update(max_per_clip=4, min_gap_s=7.0)
    cues = audio_mix.plan_sfx_cues(punches, ss_gap, duration=20.0)
    assert [c.at for c in cues] == [0.0, round(12.0 - LEAD, 3)]   # 5.94 and 17.94 are within 7 s


def test_config_yaml_carries_the_rule():
    raw = config.get_config()["style"]["sfx"]
    assert raw["min_gap_s"] == 5.0 and raw["max_per_clip"] == 2


def test_sfx_spacing_problems_reports_violations():
    def cue(at):
        return audio_mix.SfxCue(at, "whoosh", "x")
    assert audio_mix.sfx_spacing_problems([cue(0.0), cue(3.0)], SS) == ["cues at 0.000s and 3.000s are 3.000s apart (< 5.0s)"]
    assert "3 cues > max_per_clip 2" in audio_mix.sfx_spacing_problems([cue(0.0), cue(6.0), cue(12.0)], SS)
