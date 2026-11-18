"""A1/A2 audio: SFX cues and the ducked music bed.

  A1  ensure_sfx_assets  boom / whoosh, synthesized with ffmpeg when no CC0 download is
                         reachable (see assets/sfx/LICENSES.md)
      plan_sfx_cues      choose unique files by trigger priority and enforce cue spacing
      sfx_chain          one delayed, gain-staged input per cue, summed with amix
  A2  music_chain        CC0 bed at level_db, trimmed/padded to exactly the clip length
      duck_expr          whisper word timestamps as volume automation (no sidechaincompress)

Gain staging: every SFX peak is placed `headroom_db` below the clip's speech peak, so the
voice always sits on top.
"""
from __future__ import annotations

import logging
import random
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config

log = logging.getLogger(__name__)

SFX_SPECS: dict[str, dict[str, Any]] = {
    "boom": {
        "duration": 0.70,
        "expr": "0.95*exp(-5.5*t)*sin(2*PI*(110-70*t)*t)+0.22*exp(-45*t)*(2*random(0)-1)",
        "post": "lowpass=f=320",
        "note": "low sine thump with a pitch drop and a short noise transient",
    },
    "whoosh": {
        "duration": 0.45,
        "expr": (
            "(2*random(0)-1)*0.8*pow(sin(PI*min(1,t/0.45)),2)"
            "+0.25*exp(-5*t)*sin(2*PI*(300+4200*t)*t)"
        ),
        "post": "highpass=f=320,lowpass=f=7000",
        "note": "band-limited noise sweep with a rising chirp",
    },
}

PEAK_TARGET_DB = -1.0
SFX_CATEGORIES = ("impact", "whoosh", "riser", "glitch", "pop", "cash", "siren",
                  "notification", "record_scratch")


# --------------------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------------------

def file_peak_db(path: Path | str, default: float = -6.0) -> float:
    """Peak level in dBFS via ffmpeg volumedetect (`default` when it cannot be measured)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        log.warning("volumedetect failed for %s: %s", path, e)
        return default
    m = re.search(r"max_volume:\s*(-?\d+\.?\d*)\s*dB", res.stderr)
    return float(m.group(1)) if m else default


def sfx_gain_db(speech_peak_db: float, sfx_peak_db: float, ss: dict[str, Any]) -> float:
    """Gain that puts an SFX peak `headroom_db` below the speech peak."""
    headroom = float(ss["sfx"]["headroom_db"])
    return round(float(speech_peak_db) - headroom - float(sfx_peak_db), 3)


# --------------------------------------------------------------------------------------
# A1 SFX assets
# --------------------------------------------------------------------------------------

def sfx_dir(ss: dict[str, Any]) -> Path:
    d = Path(str(ss["sfx"]["dir"]))
    return d if d.is_absolute() else config.project_root() / d


def sfx_files(ss: dict[str, Any]) -> dict[str, Path]:
    s = ss["sfx"]
    return {"boom": sfx_dir(ss) / str(s["boom_file"]), "whoosh": sfx_dir(ss) / str(s["whoosh_file"])}


def synthesize_sfx(name: str, out_path: Path, sample_rate: int = 48000) -> bool:
    """Render one SFX with ffmpeg and peak-normalize it to PEAK_TARGET_DB."""
    spec = SFX_SPECS[name]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".raw.wav")
    base = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-f", "lavfi", "-i", f"aevalsrc='{spec['expr']}':s={sample_rate}:d={spec['duration']}",
        "-af", spec["post"], "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(tmp),
    ]
    if subprocess.run(base, capture_output=True).returncode != 0:
        log.error("Could not synthesize %s SFX", name)
        tmp.unlink(missing_ok=True)
        return False
    gain = PEAK_TARGET_DB - file_peak_db(tmp, default=PEAK_TARGET_DB)
    norm = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(tmp),
        "-af", f"volume={gain:.3f}dB", "-ac", "1", "-ar", str(sample_rate),
        "-c:a", "pcm_s16le", str(out_path),
    ]
    ok = subprocess.run(norm, capture_output=True).returncode == 0
    tmp.unlink(missing_ok=True)
    if ok:
        log.info("Synthesized %s -> %s (peak %.1f dBFS)", name, out_path, PEAK_TARGET_DB)
    return ok


def ensure_sfx_assets(ss: dict[str, Any], regenerate: bool = False) -> dict[str, Path]:
    """Make sure boom/whoosh exist on disk; synthesize whatever is missing."""
    out: dict[str, Path] = {}
    for name, path in sfx_files(ss).items():
        if regenerate or not path.exists():
            synthesize_sfx(name, path)
        if path.exists():
            out[name] = path
    return out


# --------------------------------------------------------------------------------------
# A1 cues and chain
# --------------------------------------------------------------------------------------

@dataclass
class SfxCue:
    at: float
    kind: str
    path: str
    trigger: str = ""
    gain_offset_db: float = 0.0

    @property
    def category(self) -> str:
        return "impact" if self.kind == "boom" else self.kind


def _emoji_category(keyword: str) -> str:
    if keyword.upper() in {"MONEY", "CASH", "RICH", "$"}:
        return "cash"
    if keyword.upper() in {"POLICE", "COPS", "WANTED", "ARRESTED"}:
        return "siren"
    return "pop"


def plan_sfx_cues(
    punches: Sequence[Any],
    ss: dict[str, Any],
    hook_at: float | None = 0.0,
    duration: float | None = None,
    *,
    item_id: str = "",
    preset: str = "",
    emoji_cues: Sequence[Any] = (),
    caption_at: float | None = None,
) -> list[SfxCue]:
    """Pick unique effects in trigger priority order on the final output timeline."""
    settings = ss["sfx"]
    enabled = set(settings.get("enabled_categories", SFX_CATEGORIES))
    min_gap = float(settings.get("min_gap_s", 5.0))
    cap = int(settings.get("max_per_clip", 2))
    rng = random.Random(f"{item_id}:{preset}")
    cues: list[SfxCue] = []
    used: set[Path] = set()
    files = sfx_files(ss)
    triggers = []
    if hook_at is not None:
        triggers.append((hook_at + float(settings.get("boom_at_s", 0)), "impact", "hook", 0.0))
    emoji_triggers = [(e.at, _emoji_category(e.keyword), "emoji", 0.0) for e in emoji_cues]
    triggers.extend(sorted(emoji_triggers, key=lambda t: (t[1] == "pop", t[0])))
    lead = float(settings["whoosh_lead_s"])
    ranked = sorted(punches, key=lambda p: (int(getattr(p, "rank", 0)), float(p.at)))
    triggers.extend((p.at - lead, "whoosh", "punch_in", 0.0) for p in ranked)
    if caption_at is not None:
        triggers.append((caption_at, "pop", "caption", -6.0))

    for at, category, trigger, gain_offset in triggers:
        if len(cues) >= cap:
            break
        at = round(max(0.0, at), 3)
        if category not in enabled or (duration is not None and at >= duration):
            continue
        if any(abs(at - c.at) < min_gap - 1e-6 for c in cues):
            continue
        library = sorted(p for p in (sfx_dir(ss) / category).glob("*.wav") if p.is_file())
        kind = category
        if not library and category in {"impact", "whoosh"}:
            kind = "boom" if category == "impact" else category
            path = files[kind]
            library = [path] if path.is_file() else []
        available = [p for p in library if p.resolve() not in used]
        if available:
            selected = rng.choice(available)
            used.add(selected.resolve())
            cues.append(SfxCue(at, kind, str(selected), trigger, gain_offset))
    return sorted(cues, key=lambda c: c.at)


def sfx_spacing_problems(cues: Sequence[SfxCue], ss: dict[str, Any]) -> list[str]:
    """What breaks the density rule in a cue list (empty = fine). Used by the tests and the
    real-clip verification; it checks, it does not decide."""
    s = ss["sfx"]
    min_gap = float(s.get("min_gap_s", 5.0))
    cap = int(s.get("max_per_clip", 2))
    ats = sorted(float(c.at) for c in cues)
    out = [f"{len(ats)} cues > max_per_clip {cap}"] if len(ats) > cap else []
    out += [f"cues at {a:.3f}s and {b:.3f}s are {b - a:.3f}s apart (< {min_gap}s)"
            for a, b in zip(ats, ats[1:]) if b - a < min_gap - 1e-6]
    return out


def sfx_chain(
    cues: Sequence[SfxCue],
    gains_db: dict[str, float],
    sample_rate: int,
    in_label: str,
    out_label: str,
) -> tuple[list[str], str]:
    """(input args, filter chain) summing every cue under the main audio."""
    if not cues:
        return [], f"[{in_label}]anull[{out_label}]"
    inputs: list[str] = []
    parts: list[str] = []
    labels: list[str] = []
    for i, cue in enumerate(cues):
        inputs += ["-i", cue.path]
        g = float(gains_db.get(cue.path, gains_db.get(cue.kind, -12.0))) + cue.gain_offset_db
        delay_ms = int(round(cue.at * 1000))
        parts.append(
            f"[{{IDX{i}}}]aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo,"
            f"volume={g:.3f}dB,adelay={delay_ms}:all=1[sfx{i}]"
        )
        labels.append(f"[sfx{i}]")
    parts.append(
        f"[{in_label}]{''.join(labels)}amix=inputs={len(cues) + 1}:normalize=0:"
        f"duration=first:dropout_transition=0[{out_label}]"
    )
    return inputs, ";".join(parts)


# --------------------------------------------------------------------------------------
# A2 music bed
# --------------------------------------------------------------------------------------

def music_path(ss: dict[str, Any]) -> Path:
    p = Path(str(ss["music_bed"]["file"]))
    return p if p.is_absolute() else config.project_root() / p


def pick_music(ss: dict[str, Any], item_id: str) -> Path | None:
    settings = ss["music_bed"]
    directory = Path(settings.get("library_dir", "assets/music"))
    if not directory.is_absolute():
        directory = config.project_root() / directory
    tracks = sorted(p for p in directory.glob("*.wav") if p.is_file())
    if tracks:
        return random.Random(item_id).choice(tracks)
    fallback = music_path(ss)
    return fallback if fallback.is_file() else None


def speech_spans(words: Iterable[dict[str, Any]], ss: dict[str, Any]) -> list[tuple[float, float]]:
    """Merge word timings into speech spans (gaps under speech_merge_gap_s are bridged)."""
    merge = float(ss["music_bed"]["speech_merge_gap_s"])
    ws = sorted(({"start": float(w["start"]), "end": float(w["end"])} for w in words), key=lambda w: w["start"])
    spans: list[tuple[float, float]] = []
    for w in ws:
        if spans and w["start"] - spans[-1][1] <= merge:
            spans[-1] = (spans[-1][0], max(spans[-1][1], w["end"]))
        else:
            spans.append((w["start"], w["end"]))
    return [(round(s, 3), round(e, 3)) for s, e in spans]


def _db_to_lin(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def duck_amount(t: float, spans: Sequence[tuple[float, float]], ss: dict[str, Any]) -> float:
    """0 outside speech, 1 inside, linear over duck_ramp_s at each edge."""
    r = max(1e-6, float(ss["music_bed"]["duck_ramp_s"]))
    total = 0.0
    for s, e in spans:
        if s - r <= t < s:
            total += (t - (s - r)) / r
        elif s <= t < e:
            total += 1.0
        elif e <= t < e + r:
            total += 1.0 - (t - e) / r
    return min(1.0, total)


def duck_gain(t: float, spans: Sequence[tuple[float, float]], ss: dict[str, Any]) -> float:
    """Linear music gain at time `t`: level_db, a further duck_db under speech."""
    mb = ss["music_bed"]
    level = _db_to_lin(mb["level_db"])
    duck = _db_to_lin(mb["duck_db"])
    return level * (1.0 + (duck - 1.0) * duck_amount(t, spans, ss))


def duck_expr(spans: Sequence[tuple[float, float]], ss: dict[str, Any]) -> str:
    """The ffmpeg `volume` expression mirroring duck_gain (evaluated per frame)."""
    mb = ss["music_bed"]
    level = _db_to_lin(mb["level_db"])
    duck = _db_to_lin(mb["duck_db"])
    if not spans:
        return f"{level:.6f}"
    r = max(1e-6, float(mb["duck_ramp_s"]))
    terms = []
    for s, e in spans:
        terms.append(
            f"gte(t,{s - r:.3f})*lt(t,{s:.3f})*((t-{s - r:.3f})/{r:.3f})"
            f"+gte(t,{s:.3f})*lt(t,{e:.3f})"
            f"+gte(t,{e:.3f})*lt(t,{e + r:.3f})*(1-(t-{e:.3f})/{r:.3f})"
        )
    env = "+".join(f"({t})" for t in terms)
    return f"{level:.6f}*(1{duck - 1.0:+.6f}*min(1,{env}))"


def music_chain(
    total_s: float,
    spans: Sequence[tuple[float, float]],
    ss: dict[str, Any],
    sample_rate: int,
    in_label: str,
    out_label: str,
    path: Path | str | None = None,
) -> tuple[list[str], str]:
    """(input args, filter chain) mixing the bed under the clip, exactly total_s long.

    The bed is padded or trimmed to the clip's exact sample count; there is no
    sidechaincompress anywhere - the ducking is volume automation from word timestamps.
    """
    p = Path(path) if path else music_path(ss)
    if not p.exists():
        log.warning("Music bed %s is missing; rendering without A2 (run scripts/fetch_assets.py)", p)
        return [], f"[{in_label}]anull[{out_label}]"
    ns = int(round(float(total_s) * sample_rate))
    chain = (
        f"[{{IDX0}}]aformat=sample_fmts=fltp:sample_rates={sample_rate}:channel_layouts=stereo,"
        f"apad=whole_len={ns},atrim=end_sample={ns},asetpts=PTS-STARTPTS,"
        f"volume='{duck_expr(spans, ss)}':eval=frame[bed];"
        f"[{in_label}][bed]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0[{out_label}]"
    )
    return ["-i", str(p)], chain


def music_sample_count(total_s: float, sample_rate: int) -> int:
    return int(round(float(total_s) * sample_rate))
