"""Turn a cut clip plus a style preset into one concrete StylePlan.

The plan is computed once per variant and is the single source of truth for every
downstream decision, which is what keeps A/V and captions in sync: dead-air removal
(A3) and the Variant B freeze both change the timeline, and *everything* that carries a
timestamp - captions, punch-ins, SFX, emojis, music ducking, the progress bar - is read
off the same `Timeline.remap` plus the same freeze offset.

pipeline.render consumes a StylePlan; it never re-derives a time.
"""
from __future__ import annotations

import dataclasses
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import audio_mix, config, edits, keywords as kwmod, overlay, style
from .cut import audio_envelope, extract_audio_loudness_profile, extract_scene_cuts, snap_end_to_pause
from . import endings
from .rendercore import (ascent_px, cap_height_px, descent_px, layout_hook, media_duration, normalize_caption_words,
                         render_settings, text_width_px)

log = logging.getLogger(__name__)


@dataclass
class ClipAnalysis:
    """Everything probed once from the cut, shared by Variant A and Variant B."""
    cut_path: str
    duration: float
    end_s: float                                      # content length on the cut timeline
    content_s: float                                  # content length after dead-air removal
    timeline: edits.Timeline
    segments: list[edits.Segment]                     # cut timeline
    scene_cuts: list[float] = field(default_factory=list)
    loudness: list[tuple[float, float]] = field(default_factory=list)
    motion: list[tuple[float, float]] = field(default_factory=list)
    speech_peak_db: float = -6.0
    loop_end: edits.LoopEnd | None = None
    tail_quiet_s: float | None = None                 # measured quiet before the end (speech clips)
    words: list[dict[str, Any]] = field(default_factory=list)   # cut timeline, trimmed to end_s
    notes: list[str] = field(default_factory=list)


@dataclass
class StylePlan:
    """One variant's resolved plan. `flags` decides what render.py switches on."""
    preset: str
    flags: dict[str, bool]
    ss: dict[str, Any]
    geo: style.FillGeometry
    layout: style.Layout
    timeline: edits.Timeline
    segments: list[edits.Segment]                     # output timeline
    zoom: edits.ZoomProgram
    punches: list[edits.Punch] = field(default_factory=list)
    emoji_cues: list[overlay.EmojiCue] = field(default_factory=list)
    sfx_cues: list[audio_mix.SfxCue] = field(default_factory=list)
    sfx_gains: dict[str, float] = field(default_factory=dict)
    music_file: str | None = None
    speech_spans: list[tuple[float, float]] = field(default_factory=list)
    keyword_list: list[str] = field(default_factory=list)
    words: list[dict[str, Any]] = field(default_factory=list)   # output timeline
    content_s: float = 0.0                            # cut part of the output
    freeze_s: float = 0.0
    total_s: float = 0.0
    audio_fade_s: float = 0.25
    hook_lines: list[str] = field(default_factory=list)
    hook_font_size: int = 150
    channel_name: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def modes(self) -> list[str]:
        return edits.segment_modes(self.segments)

    def summary(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "flags": {k: v for k, v in self.flags.items() if v},
            "modes": self.modes,
            "segments": [(s.start, s.end, s.mode, s.motion) for s in self.segments],
            "zoom_constant": self.zoom.is_constant,
            "zoom_values": {t: round(edits.zoom_value(self.zoom, t), 4)
                            for t in (0.0, round(self.total_s / 2, 2), round(max(0.0, self.total_s - 0.1), 2))},
            "punches": [(p.at, p.reason) for p in self.punches],
            "emoji": [(c.at, c.keyword, c.codepoint) for c in self.emoji_cues],
            "sfx": [(c.at, c.kind) for c in self.sfx_cues],
            "sfx_gains_db": self.sfx_gains,
            "music": self.music_file,
            "keywords": self.keyword_list,
            "dead_air_dropped": self.timeline.dropped,
            "content_s": self.content_s,
            "freeze_s": self.freeze_s,
            "total_s": self.total_s,
            "audio_fade_s": self.audio_fade_s,
            "layout": {
                "caption_center_y": self.layout.caption_center_y,
                "caption_ink": self.layout.caption_ink,
                "hook_center_y": self.layout.hook_center_y,
                "hook_ink": self.layout.hook_ink,
                "credit": (self.layout.credit_x, self.layout.credit_y),
                "credit_ink": self.layout.credit_ink,
                "progress_y": self.layout.progress_y,
                "video_edges": self.layout.video_edges,
            },
            "notes": self.notes + self.layout.notes,
        }


def _text_box_px(font_size: float, font_path) -> float:
    """Full ascender-to-descender height of mixed-case text at this size."""
    return ascent_px(font_size, font_path) + descent_px(font_size, font_path)


def _snap(t: float, fps: int) -> float:
    return round(round(float(t) * fps) / fps, 6)


def _remap_profile(
    profile: Sequence[tuple[float, float]],
    timeline: edits.Timeline,
    limit: float,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for t, v in profile:
        if t > limit:
            continue
        nt, inside = timeline.remap(t)
        if inside:
            out.append((nt, v))
    return out


def analyze_clip(
    cut_path: Path | str,
    words: Sequence[dict[str, Any]] | None = None,
    cfg: dict[str, Any] | None = None,
    preset: str | None = None,
    duration: float | None = None,
    clean_cut_end: bool = False,
) -> ClipAnalysis:
    """Probe the cut once: scene cuts, motion, loudness, frame hashes; then decide the
    content end (A4 or the speech-safe snap), the dead-air timeline (A3) and the
    framing segments (F1). Probes are only run for the flags the preset enables.

    With speech, the end always follows a completed sentence (pipeline.endings); A4 may
    only choose among those ends. `clean_cut_end` says the cut stage verified the cut's
    own end (it saw the audio after the cut). Raises endings.NoCleanEnding when no end is
    speech-safe."""
    cfg = cfg if cfg is not None else config.get_config()
    rs = render_settings(cfg)
    ss = style.style_settings(cfg)
    flags = style.resolve_preset(preset, cfg)
    fps = int(rs["fps"])
    cut_cfg = cfg.get("cut", {}) or {}
    words = list(words or [])
    dur = float(duration) if duration else media_duration(cut_path)
    notes: list[str] = []

    need_scene = flags["zoom_fill"] or flags["punch_ins"]
    need_motion = flags["zoom_fill"] or flags["dead_air"]
    need_loud = flags["punch_ins"] or flags["dead_air"] or not words
    scene_cuts = extract_scene_cuts(cut_path, threshold=float(cut_cfg.get("scene_threshold", 0.3))) if need_scene else []
    motion = edits.motion_profile(cut_path, float(ss["zoom_fill"]["motion_step_s"])) if need_motion else []
    loudness = extract_audio_loudness_profile(cut_path) if need_loud else []

    # --- content end: speech-safe (pipeline.endings), then A4 among the safe ends -----
    es = endings.ending_settings(cfg)
    min_dur = float(cut_cfg.get("min_duration", 18.0))
    max_dur = min(float(cut_cfg.get("max_duration", 22.0)), dur)
    spans: list[endings.SafeSpan] | None = None
    env = None
    if words:
        env = audio_envelope(cut_path, 0.0, dur) if Path(str(cut_path)).exists() else None
        proposed, spans = endings.content_end(
            words, dur, min_dur, float(cut_cfg.get("max_duration", 22.0)), es,
            trusted_end=dur if clean_cut_end else None, env=env,
        )
    else:
        proposed = snap_end_to_pause(
            0.0, dur,
            loudness=loudness,
            min_dur=min_dur, max_dur=float(cut_cfg.get("max_duration", 22.0)),
            hard_max_end=dur,
        )
    end_s = proposed
    loop = None
    if flags["loop_end"] and dur > min_dur:
        hashes = edits.gray_hashes(cut_path, fps=1.0 / max(0.05, float(ss["loop_end"]["search_step_s"])))
        loop = edits.choose_loop_end(hashes, min_dur, max_dur, words, ss, proposed_end=proposed,
                                     safe_spans=spans, es=es)
        if loop:
            end_s = loop.end_s
            notes.append(
                f"A4 end {loop.end_s:.2f}s (dHash distance {loop.distance} bits from frame 1, "
                f"{'in' if loop.in_pause else 'not in'} a speech pause, {loop.candidates} candidates)"
            )
    if spans:
        span = endings.span_at(end_s, spans)
        end_s = min(endings.snap_inside(end_s, span, 1.0 / fps) if span else _snap(end_s, fps), dur)
        # A measured pause (punct_audio) is where whisper's NEXT word claims to have started
        # already; the audio says it has not, so that one check is the audio's.
        if span is None or (span.reason != "punct_audio" and endings.speaks_through(end_s, words)):
            raise endings.NoCleanEnding(f"no_clean_ending: end {end_s:.3f}s is not after a completed sentence")
        words = [w for w in words if float(w["end"]) <= end_s + 0.02]
    else:
        end_s = min(_snap(end_s, fps), dur)
    tail_quiet = endings.quiet_tail_s(end_s, env, es) if spans else None

    # --- A3 dead air, measured on the content region only ---------------------------
    if flags["dead_air"]:
        timeline = edits.plan_dead_air(end_s, loudness, motion, ss, words=words, es=es)
        timeline = edits.Timeline(
            kept=[(_snap(s, fps), _snap(e, fps)) for s, e in timeline.kept],
            source_duration=end_s,
            dropped=timeline.dropped,
        )
        if timeline.dropped:
            notes.append(
                f"A3 dropped {len(timeline.dropped)} dead-air span(s) "
                f"({sum(e - s for s, e in timeline.dropped):.2f}s): {timeline.dropped}"
            )
    else:
        timeline = edits.Timeline.identity(end_s)
    content_s = _snap(timeline.total, fps)

    segments = edits.plan_segments(end_s, scene_cuts, motion, ss) if flags["zoom_fill"] else [
        edits.Segment(0.0, end_s, "fit")
    ]
    speech_peak = audio_mix.file_peak_db(cut_path) if flags["sfx"] else -6.0

    return ClipAnalysis(
        cut_path=str(cut_path),
        duration=round(dur, 3),
        end_s=end_s,
        content_s=content_s,
        timeline=timeline,
        segments=segments,
        scene_cuts=scene_cuts,
        loudness=loudness,
        motion=motion,
        speech_peak_db=round(speech_peak, 2),
        loop_end=loop,
        tail_quiet_s=tail_quiet,
        words=[w for w in words if float(w["start"]) < end_s],
        notes=notes,
    )


def caption_lines(words: Sequence[dict[str, Any]], cfg: dict[str, Any] | None = None) -> list[list[dict[str, Any]]]:
    """The caption lines (1-3 word chunks) these words would be burned in as."""
    from .rendercore import chunk_caption_words

    return chunk_caption_words(list(words), render_settings(cfg))


def gate_caption_words(
    analysis: ClipAnalysis,
    profile: Any,
    cfg: dict[str, Any] | None = None,
) -> ClipAnalysis:
    """Captions under music (PLAN.md calls #181-#182). `profile` is transcribe.speech_profile of the
    cut (cut timeline); None = not measurable, nothing is gated.

    * Speech under `render.caption.min_speech_ratio` of the content: NO word captions (hook only),
      with a note and a log line saying why: whisper's word times are not trustworthy there.
    * Otherwise a caption line with any word further than `max_outside_speech_s` from a speech region
      is dropped whole.
    Only the caption words change; the ending was decided before this and is not touched."""
    if profile is None or not analysis.words:
        return analysis
    cap = render_settings(cfg)["caption"]
    min_ratio = float(cap.get("min_speech_ratio", 0.25))
    max_out = float(cap.get("max_outside_speech_s", 1.0))
    ratio = profile.ratio(0.0, analysis.end_s)
    notes = list(analysis.notes)
    if ratio < min_ratio:
        note = (f"captions off: speech ratio {ratio:.2f} < {min_ratio:.2f} (Silero speech {profile.speech_s(0.0, analysis.end_s):.1f} s "
                f"of {analysis.end_s:.1f} s; music-heavy, whisper word times unreliable): hook only")
        log.info("%s: %s", analysis.cut_path, note)
        notes.append(note)
        return dataclasses.replace(analysis, words=[], notes=notes)
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for line in caption_lines(analysis.words, cfg):
        far = max(profile.distance(float(w["start"]), float(w["end"])) for w in line)
        if far > max_out:
            dropped.append(" ".join(str(w["word"]) for w in line) + f" @{float(line[0]['start']):.2f}s")
        else:
            kept.extend(line)
    if dropped:
        note = f"caption lines dropped (> {max_out:.1f} s from speech): {'; '.join(dropped)[:300]}"
        log.info("%s: %s", analysis.cut_path, note)
        notes.append(note)
    return dataclasses.replace(analysis, words=kept, notes=notes)


def build_style_plan(
    analysis: ClipAnalysis,
    hook_text: str,
    *,
    variant: str = "A",
    freeze_s: float = 0.0,
    vo_words: Sequence[dict[str, Any]] | None = None,
    channel_name: str = "",
    keyword_list: Sequence[str] | None = None,
    cfg: dict[str, Any] | None = None,
    preset: str | None = None,
    occupancy: Any = None,
) -> StylePlan:
    """Resolve one variant's plan on the OUTPUT timeline (freeze offset included).

    `occupancy`: text burned into the source (pipeline.textdetect), so the layout keeps our captions,
    credit and progress bar off it. Raises style.SourceTextCollision when there is no clear place."""
    cfg = cfg if cfg is not None else config.get_config()
    rs = render_settings(cfg)
    ss = style.style_settings(cfg)
    flags = style.resolve_preset(preset, cfg)
    preset_name = preset or style.default_preset(cfg)
    fps = int(rs["fps"])
    geo = style.fill_geometry(rs, ss)

    freeze_s = _snap(max(0.0, float(freeze_s)), fps)
    content_s = analysis.content_s
    total_s = _snap(freeze_s + content_s, fps)

    # --- captions on the output timeline --------------------------------------------
    speech_out = analysis.timeline.remap_words(analysis.words)
    speech_out = [{**w, "start": w["start"] + freeze_s, "end": min(w["end"] + freeze_s, total_s)} for w in speech_out]
    words_out: list[dict[str, Any]] = []
    for w in vo_words or []:
        if float(w["start"]) < freeze_s:
            words_out.append({**w, "end": min(float(w["end"]), freeze_s)})
    words_out += [w for w in speech_out if w["start"] < total_s - 0.02]
    words_out = normalize_caption_words(words_out)     # caption text only; analysis.words stay as whisper wrote them

    # --- F1 segments (plus the B freeze as a static segment) ------------------------
    segments = edits.shift_segments(analysis.segments, analysis.timeline, offset=freeze_s)
    if freeze_s > 0:
        # The B freeze is a held frame: static, so F1 keeps the fit and M4 pushes across it.
        # Merge it into a following static segment so the push runs once instead of resetting.
        if segments and segments[0].mode == "fit":
            segments[0] = edits.Segment(0.0, segments[0].end, "fit", 0.0)
        else:
            segments = [edits.Segment(0.0, freeze_s, "fit", 0.0)] + segments
    if segments:
        segments[-1].end = max(segments[-1].end, total_s)

    # --- M3 punch-ins on the output timeline ----------------------------------------
    punches: list[edits.Punch] = []
    if flags["punch_ins"]:
        punches = edits.plan_punch_ins(
            content_s,
            _remap_profile(analysis.loudness, analysis.timeline, analysis.end_s),
            [analysis.timeline.remap(c)[0] for c in analysis.scene_cuts
             if c <= analysis.end_s and analysis.timeline.remap(c)[1]],
            ss,
            offset=freeze_s,
        )

    zoom = edits.build_zoom_program(
        segments, punches, geo.fill_zoom, ss,
        zoom_fill=flags["zoom_fill"], slow_push=flags["slow_push"],
        punch_ins=flags["punch_ins"], total_s=total_s,
    )

    # --- text placement after F1 -----------------------------------------------------
    hook_lines, hook_size = layout_hook(hook_text, rs)
    caption_scale = (float(ss["caption_pop"]["peak_scale"]) / 100.0) if flags["caption_pop"] else 1.0
    credit_w = None
    if occupancy is not None and flags["credit_tag"]:
        ct = ss["credit_tag"]
        credit_w = (text_width_px(overlay.credit_text(channel_name, ss), float(ct["font_size"]), rs["font_path"])
                    + 2.0 * float(ct["outline"]) + float(ct["shadow"]))
    layout = style.plan_layout(
        edits.segment_modes(segments) if flags["zoom_fill"] else ["fit"],
        hook_size, max(1, len(hook_lines)), rs, ss, cap_height_px,
        caption_scale=caption_scale, text_box_px=_text_box_px,
        occupancy=occupancy, captions=bool(words_out), credit=flags["credit_tag"],
        progress=flags["progress_bar"], credit_width_px=credit_w,
    )

    # --- C1 / C2 ---------------------------------------------------------------------
    kw_list = [kwmod.normalize(k) for k in (keyword_list or []) if kwmod.normalize(k)]
    if flags["keyword_color"] and not kw_list:
        kw_list = kwmod.rule_keywords(words_out, cfg)
    emoji_cues = overlay.plan_emoji_cues(words_out, kw_list, ss, total_s) if flags["keyword_emoji"] else []

    # --- A1 / A2 ---------------------------------------------------------------------
    sfx_cues: list[audio_mix.SfxCue] = []
    sfx_gains: dict[str, float] = {}
    if flags["sfx"]:
        sfx_cues = audio_mix.plan_sfx_cues(punches, ss, hook_at=0.0, duration=total_s)
        for kind, path in audio_mix.sfx_files(ss).items():
            if path.exists():
                sfx_gains[kind] = audio_mix.sfx_gain_db(
                    analysis.speech_peak_db, audio_mix.file_peak_db(path, default=-1.0), ss
                )
    music_file = None
    spans: list[tuple[float, float]] = []
    if flags["music_bed"]:
        mp = audio_mix.music_path(ss)
        spans = audio_mix.speech_spans(words_out, ss)
        if mp.exists():
            music_file = str(mp)
        else:
            log.warning("A2 music bed missing (%s); run scripts/fetch_assets.py on the laptop", mp)

    fade = float(ss["loop_end"]["audio_fade_s"]) if flags["loop_end"] else float(rs["end_fade_s"])
    if speech_out:
        # The end sits in silence after the last word; never let the fade reach back into it -
        # by whisper's word end, and by the measured quiet before the end (whisper's word ends
        # can be late or early by ~0.1 s).
        room = total_s - max(float(w["end"]) for w in speech_out)
        if analysis.tail_quiet_s is not None and analysis.tail_quiet_s > 0:
            room = min(room, analysis.tail_quiet_s)
        fade = round(min(fade, max(0.03, room)), 3)

    notes = list(analysis.notes)
    if flags["zoom_fill"]:
        notes.append(
            f"F1 {len(segments)} segment(s) {[s.mode for s in segments]}, fill_zoom={geo.fill_zoom:.4f}, "
            f"bands fill={geo.band('fill')} fit={geo.band('fit')}"
        )
    if flags["music_bed"] and not music_file:
        notes.append("A2 skipped: no CC0 music bed on disk")

    return StylePlan(
        preset=preset_name,
        flags=flags,
        ss=ss,
        geo=geo,
        layout=layout,
        timeline=analysis.timeline,
        segments=segments,
        zoom=zoom,
        punches=punches,
        emoji_cues=emoji_cues,
        sfx_cues=sfx_cues,
        sfx_gains=sfx_gains,
        music_file=music_file,
        speech_spans=spans,
        keyword_list=kw_list,
        words=words_out,
        content_s=content_s,
        freeze_s=freeze_s,
        total_s=total_s,
        audio_fade_s=fade,
        hook_lines=hook_lines,
        hook_font_size=hook_size,
        channel_name=channel_name,
        notes=notes,
    )


def plan_from_words(
    hook_text: str,
    duration: float,
    words: Sequence[dict[str, Any]] | None = None,
    *,
    cfg: dict[str, Any] | None = None,
    preset: str | None = None,
    segments: Sequence[edits.Segment] | None = None,
    timeline: edits.Timeline | None = None,
    punches: Sequence[edits.Punch] | None = None,
    freeze_s: float = 0.0,
    vo_words: Sequence[dict[str, Any]] | None = None,
    channel_name: str = "",
    keyword_list: Sequence[str] | None = None,
    speech_peak_db: float = -6.0,
    occupancy: Any = None,
) -> StylePlan:
    """Build a StylePlan without touching media - for tests, previews and contact sheets.

    The caller supplies what probing would have produced (segments, dead-air timeline,
    punch-ins); everything else is resolved exactly as in the real render path.
    """
    cfg = cfg if cfg is not None else config.get_config()
    ss = style.style_settings(cfg)
    rs = render_settings(cfg)
    fps = int(rs["fps"])
    words = list(words or [])
    tl = timeline or edits.Timeline.identity(float(duration))
    segs = list(segments) if segments is not None else [edits.Segment(0.0, float(duration), "fill")]
    analysis = ClipAnalysis(
        cut_path="<memory>",
        duration=float(duration),
        end_s=float(duration),
        content_s=_snap(tl.total, fps),
        timeline=tl,
        segments=segs,
        speech_peak_db=speech_peak_db,
        words=words,
    )
    sp = build_style_plan(
        analysis, hook_text, freeze_s=freeze_s, vo_words=vo_words, cfg=cfg, preset=preset,
        channel_name=channel_name, keyword_list=keyword_list, occupancy=occupancy,
    )
    if punches is not None:
        sp.punches = list(punches)
        sp.zoom = edits.build_zoom_program(
            sp.segments, sp.punches, sp.geo.fill_zoom, ss,
            zoom_fill=sp.flags["zoom_fill"], slow_push=sp.flags["slow_push"],
            punch_ins=sp.flags["punch_ins"], total_s=sp.total_s,
        )
        if sp.flags["sfx"]:
            sp.sfx_cues = audio_mix.plan_sfx_cues(sp.punches, ss, hook_at=0.0, duration=sp.total_s)
    return sp


def floor_respected(analysis: ClipAnalysis, ss: dict[str, Any]) -> bool:
    """A3 guard: the output never falls below style.dead_air.min_output_s."""
    floor = float(ss["dead_air"]["min_output_s"])
    return analysis.content_s >= min(floor, analysis.end_s) - 1e-6


def frames(duration_s: float, fps: int) -> int:
    return max(1, int(math.floor(float(duration_s) * fps + 1e-6)))
