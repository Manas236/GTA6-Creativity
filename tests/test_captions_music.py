"""Fix 3 (2026-11-12): captions mistimed under music.

The Trailer 2 item (item_9eb56ea0f951, 11 Nov office preview): whisper put "I'm" 9.5 s and "here"
4 s before they were said, stretched across the score; the captions ran early. Now every word is
checked against Silero's speech probability (pipeline/transcribe.py), caption lines far from speech
are dropped, and a music-heavy cut gets no word captions at all (hook only).

The synthetic clip is a music bed (a chord with a beat and noise hits) with ONE short spoken phrase
(the pipeline's own Kokoro voice) starting at 12 s. Tests that need Kokoro or the whisper model skip
when those files are not on the machine."""
from __future__ import annotations

import copy
import re
import subprocess
from pathlib import Path

import numpy as np
import pytest

from pipeline import config, plan as planning, render, rendercore, transcribe

CFG = config.get_config()
ROOT = config.ROOT_DIR
PHRASE_AT = 12.0
PHRASE = "Get in the car, we are leaving Vice City tonight."
KOKORO = ROOT / "assets" / "models" / "kokoro-v0_19.onnx"
WHISPER_CACHE = Path.home() / ".cache" / "huggingface" / "hub" / "models--Systran--faster-whisper-small.en"

needs_kokoro = pytest.mark.skipif(not KOKORO.exists(), reason="Kokoro model not on this machine")


def _music_bed(path: Path, seconds: float = 16.0) -> Path:
    chord = ("(0.12*sin(2*PI*220*t)+0.10*sin(2*PI*277.18*t)+0.08*sin(2*PI*329.63*t)+0.06*sin(2*PI*110*t))"
             "*(0.65+0.35*sin(2*PI*2*t))")
    hits = "0.35*(random(0)-0.5)*exp(-28*mod(t\\,0.5))+0.3*sin(2*PI*55*t)*exp(-12*mod(t\\,1))"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"aevalsrc=exprs='{chord}+{hits}':s=16000:d={seconds}", "-ac", "1", str(path)],
                   check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def music_clip(tmp_path_factory) -> tuple[Path, float]:
    """(16 s mono 16 kHz WAV: music + the phrase at 12 s, measured onset of the phrase in seconds)."""
    if not KOKORO.exists():
        pytest.skip("Kokoro model not on this machine")
    from pipeline import voiceover
    import soundfile as sf

    d = tmp_path_factory.mktemp("music")
    phrase = d / "phrase.wav"
    voiceover.synthesize_voiceover(PHRASE, phrase)
    samples, sr = sf.read(str(phrase))
    loud = np.flatnonzero(np.abs(samples) > 10 ** (-40 / 20))
    lead = float(loud[0]) / sr if len(loud) else 0.0
    bed = _music_bed(d / "bed.wav")
    out = d / "mix.wav"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(bed), "-i", str(phrase),
                    "-filter_complex",
                    f"[1:a]aresample=16000,adelay={int(PHRASE_AT * 1000)}:all=1[s];"
                    "[0:a][s]amix=inputs=2:duration=first:normalize=0",
                    "-ac", "1", "-ar", "16000", str(out)], check=True, capture_output=True)
    return out, PHRASE_AT + lead


def _first_caption_start(words, cfg=CFG) -> float | None:
    ass = render.build_ass_subtitles("HOOK", 16.0, words, cfg=cfg)
    starts = [m.group(1) for m in re.finditer(r"Dialogue: 1,(\d+:\d\d:\d\d\.\d\d),", ass)]
    if not starts:
        return None
    h, m, s = starts[0].split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


# ----------------------------------------------------------------------------- the word check

def test_whisper_runs_with_its_vad_filter_and_the_configured_vad_settings(monkeypatch, tmp_path):
    calls = []

    class Fake:
        def transcribe(self, path, **kw):
            calls.append(kw)
            return [], None

    monkeypatch.setattr(transcribe, "get_model", lambda name: Fake())
    clip = _music_bed(tmp_path / "bed.wav", 1.0)
    transcribe.transcribe_words(clip, CFG)
    assert calls[0]["vad_filter"] is True
    assert calls[0]["vad_parameters"] == transcribe.settings(CFG)["vad"]


def test_the_cache_key_covers_the_vad_settings_and_the_speech_check():
    base = transcribe.cache_key(CFG)
    for section, key, value in (("vad", "threshold", 0.6), ("vad", "speech_pad_ms", 200),
                                ("speech_check", "word_min_prob", 0.5), ("speech_check", "enabled", False)):
        cfg = copy.deepcopy(CFG)
        cfg["transcribe"].setdefault(section, {})[key] = value
        assert transcribe.cache_key(cfg) != base, (section, key)


@needs_kokoro
def test_words_whisper_placed_in_the_music_are_dropped_and_captions_start_at_the_phrase(music_clip):
    """The Trailer 2 pattern, with the timings given: two words placed seconds early in the music,
    one stretched from the music into the phrase, the rest on the phrase."""
    clip, onset = music_clip
    words = [{"word": "Get", "start": 2.6, "end": 3.1}, {"word": "in", "start": 7.4, "end": 7.9},
             {"word": "the", "start": onset - 0.6, "end": onset + 0.15},
             {"word": "car,", "start": onset + 0.15, "end": onset + 0.5},
             {"word": "we", "start": onset + 0.6, "end": onset + 0.75},
             {"word": "are", "start": onset + 0.75, "end": onset + 0.9},
             {"word": "leaving", "start": onset + 0.9, "end": onset + 1.3}]
    profile = transcribe.speech_profile(clip, CFG)
    assert profile is not None and profile.regions
    assert abs(profile.regions[0][0] - onset) < 0.3                     # Silero hears the phrase, not the bed
    kept, dropped = transcribe.check_words(words, profile, transcribe.settings(CFG)["speech_check"])
    assert [w["word"] for w in dropped] == ["Get", "in"]
    first = _first_caption_start(kept)
    assert first is not None and abs(first - onset) <= 0.3
    assert all(w["start"] >= onset - 0.3 for w in kept)


@needs_kokoro
@pytest.mark.skipif(not WHISPER_CACHE.exists(), reason="faster-whisper small.en not cached on this machine")
def test_real_whisper_on_music_plus_one_phrase_captions_start_within_0_3_s(music_clip):
    clip, onset = music_clip
    words = transcribe.transcribe_words(clip, CFG, raise_errors=True)
    assert words, "whisper heard nothing"
    assert all(float(w["start"]) >= onset - 0.3 for w in words), words    # none before the phrase
    first = _first_caption_start(words)
    assert first is not None and abs(first - onset) <= 0.3, (first, onset, words[:3])


# ----------------------------------------------------------------------------- the render-side gates

def _analysis(words, end_s=16.0) -> planning.ClipAnalysis:
    from pipeline import edits

    return planning.ClipAnalysis(cut_path="x.mp4", duration=end_s, end_s=end_s, content_s=end_s,
                                 timeline=edits.Timeline.identity(end_s), segments=[], words=list(words))


def _profile(regions, duration=16.0) -> transcribe.SpeechProfile:
    hop = 0.032
    probs = np.zeros(int(duration / hop) + 1)
    for s, e in regions:
        probs[int(s / hop):int(e / hop) + 1] = 0.9
    return transcribe.SpeechProfile(probs, hop, duration, list(regions))


def test_a_cut_below_the_speech_ratio_gets_no_word_captions_and_says_why(caplog):
    caplog.set_level("INFO")
    words = [{"word": "Lucia", "start": 12.1, "end": 12.5}, {"word": "Camino's.", "start": 12.5, "end": 13.1}]
    gated = planning.gate_caption_words(_analysis(words), _profile([(12.0, 13.5)]), CFG)
    assert gated.words == []
    assert any("speech ratio" in n and "hook only" in n for n in gated.notes)
    assert "speech ratio" in caplog.text


def test_caption_lines_more_than_1_s_from_speech_are_dropped():
    regions = [(0.0, 6.0), (10.0, 16.0)]
    words = [{"word": "kept", "start": 1.0, "end": 1.3},
             {"word": "edge", "start": 6.8, "end": 6.95},         # 0.8 s after speech: kept
             {"word": "far", "start": 8.1, "end": 8.3},           # 1.7 s from any speech: dropped
             {"word": "also", "start": 12.0, "end": 12.3}]
    cfg = copy.deepcopy(CFG)
    cfg["render"]["caption"]["max_words"] = 1                       # one word per caption line
    gated = planning.gate_caption_words(_analysis(words), _profile(regions), cfg)
    assert [w["word"] for w in gated.words] == ["kept", "edge", "also"]


def test_without_a_speech_measurement_nothing_is_gated():
    words = [{"word": "x", "start": 1.0, "end": 1.2}]
    assert planning.gate_caption_words(_analysis(words), None, CFG).words == words


@needs_kokoro
def test_the_synthetic_music_clip_is_gated_at_the_default_ratio_and_timed_right_below_it(music_clip):
    clip, onset = music_clip
    words = [{"word": "Get", "start": onset + 0.02, "end": onset + 0.2},
             {"word": "in", "start": onset + 0.2, "end": onset + 0.3}]
    profile = transcribe.speech_profile(clip, CFG)
    assert profile.ratio() < float(CFG["render"]["caption"]["min_speech_ratio"])
    assert planning.gate_caption_words(_analysis(words), profile, CFG).words == []        # hook only
    cfg = copy.deepcopy(CFG)
    cfg["render"]["caption"]["min_speech_ratio"] = 0.05
    kept = planning.gate_caption_words(_analysis(words), profile, cfg).words
    assert [w["word"] for w in kept] == ["Get", "in"]
    assert abs(_first_caption_start(kept, cfg) - onset) <= 0.3


def test_rendercore_chunks_are_what_the_line_gate_judges():
    """The gate uses the same chunking as the burned-in captions."""
    words = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate("a b c d".split())]
    rs = render.render_settings(CFG)
    assert planning.caption_lines(words, CFG) == rendercore.chunk_caption_words(words, rs)
