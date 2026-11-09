"""A1/A2 audio: SFX cues and the ducked music bed.

  A1  ensure_sfx_assets  boom / whoosh, synthesized with ffmpeg when no CC0 download is
                         reachable (see assets/sfx/LICENSES.md)
      plan_sfx_cues      boom on the hook's first frame, whoosh on every punch-in
      sfx_chain          one delayed, gain-staged input per cue, summed with amix
  A2  music_chain        CC0 bed at level_db, trimmed/padded to exactly the clip length
      duck_expr          whisper word timestamps as volume automation (no sidechaincompress)

Gain staging: every SFX peak is placed `headroom_db` below the clip's speech peak, so the
voice always sits on top.
"""
from __future__ import annotations

import logging
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
    kind: str          # "boom" | "whoosh"
    path: str


def plan_sfx_cues(
    punches: Sequence[Any],
    ss: dict[str, Any],
    hook_at: float = 0.0,
    duration: float | None = None,
) -> list[SfxCue]:
    """Boom on the hook's first frame, whoosh just before each punch-in."""
    files = sfx_files(ss)
    s = ss["sfx"]
    cues: list[SfxCue] = []
    boom = files["boom"]
    if boom.exists():
        cues.append(SfxCue(round(max(0.0, float(hook_at) + float(s["boom_at_s"])), 3), "boom", str(boom)))
    whoosh = files["whoosh"]
    if whoosh.exists():
        lead = float(s["whoosh_lead_s"])
        for p in punches:
            at = max(0.0, float(p.at) - lead)
            if duration is not None and at >= float(duration):
                continue
            cues.append(SfxCue(round(at, 3), "whoosh", str(whoosh)))
    cues.sort(key=lambda c: c.at)
    return cues


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
        g = float(gains_db.get(cue.kind, -12.0))
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
