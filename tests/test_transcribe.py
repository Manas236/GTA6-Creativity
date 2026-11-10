"""Caption transcription: the GTA glossary reaches every faster-whisper call, the model is
configurable, and the cached transcript is keyed on model + glossary. No real whisper: a fake
model records what it was asked."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline import config, cut, db, hooks, render, transcribe

ROOT = Path(__file__).resolve().parent.parent
CFG = config.get_config()


class FakeModel:
    def __init__(self, name: str = "fake"):
        self.name = name
        self.calls: list[dict] = []

    def transcribe(self, path, **kwargs):
        self.calls.append({"path": str(path), **kwargs})
        word = SimpleNamespace(word=" wanted", start=0.1, end=0.4, probability=0.9)
        level = SimpleNamespace(word=" level.", start=0.4, end=0.8, probability=0.9)
        return [SimpleNamespace(words=[word, level])], None


@pytest.fixture
def fake_models(monkeypatch):
    models: dict[str, FakeModel] = {}

    def get_model(name):
        return models.setdefault(name, FakeModel(name))
    monkeypatch.setattr(transcribe, "get_model", get_model)
    return models


def _tone(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                    "-ac", "1", "-ar", "16000", str(path)], capture_output=True, check=True)
    return path


def _assert_glossary(models: dict[str, FakeModel], expected_calls: int):
    ts = transcribe.settings(CFG)
    calls = [c for m in models.values() for c in m.calls]
    assert len(calls) == expected_calls
    assert set(models) == {ts["model"]}                                # the configured model, every time
    for c in calls:
        prompt = " ".join(str(c.get(k, "")) for k in ("hotwords", "initial_prompt"))
        for term in ("Leonida", "Lucia", "Vice City", "Port Gellhorn", "wanted level", "GTA 6", "Franklin"):
            assert term in prompt, (term, c)


# ----------------------------------------------------------------------------- config

def test_config_carries_the_model_and_the_glossary():
    ts = transcribe.settings(CFG)
    assert ts["model"] in ("base.en", "small.en")
    for term in ("Leonida", "Lucia", "Jason", "Vice City", "Port Gellhorn", "Ambrosia", "wanted level",
                 "Rockstar", "GTA 6", "GTA VI", "Trevor", "Michael", "Franklin", "Grassrivers", "Mount Kalaga"):
        assert term in ts["glossary"], term
    assert transcribe.prompt_kwargs(ts)                                # the glossary is passed at all


def test_the_older_render_whisper_model_key_still_works_and_transcribe_model_wins():
    assert transcribe.settings({"render": {"whisper_model": "tiny.en"}})["model"] == "tiny.en"
    assert transcribe.settings({"render": {"whisper_model": "tiny.en"},
                                "transcribe": {"model": "small.en"}})["model"] == "small.en"


@pytest.mark.parametrize("mode,keys", [("hotwords", {"hotwords"}), ("initial_prompt", {"initial_prompt"}),
                                       ("both", {"hotwords", "initial_prompt"}), ("none", set())])
def test_glossary_as_selects_how_the_glossary_is_passed(mode, keys):
    ts = {**transcribe.DEFAULTS, "glossary": ["Leonida", "wanted level"], "glossary_as": mode}
    kw = transcribe.prompt_kwargs(ts)
    assert set(kw) == keys
    for v in kw.values():
        assert "Leonida" in v and "wanted level" in v


# ----------------------------------------------------------------------------- cache key

def test_the_cache_key_changes_with_model_glossary_and_how_it_is_passed():
    base = copy.deepcopy(CFG)
    k = transcribe.cache_key(base)
    assert transcribe.cache_key(copy.deepcopy(base)) == k                # stable
    other_model = copy.deepcopy(base)
    other_model["transcribe"]["model"] = "tiny.en"
    more_terms = copy.deepcopy(base)
    more_terms["transcribe"]["glossary"] = base["transcribe"]["glossary"] + ["Kortz Center"]
    other_mode = copy.deepcopy(base)
    other_mode["transcribe"]["glossary_as"] = "hotwords" if base["transcribe"]["glossary_as"] != "hotwords" else "both"
    keys = {k, transcribe.cache_key(other_model), transcribe.cache_key(more_terms), transcribe.cache_key(other_mode)}
    assert len(keys) == 4


def test_a_cached_transcript_from_another_model_or_glossary_is_not_reused():
    item = {"cut_start": 10.0, "cut_end": 30.0}
    words = [{"word": "wanna", "start": 0.0, "end": 0.2}, {"word": "love.", "start": 0.2, "end": 0.5}]
    old = copy.deepcopy(CFG)
    old["transcribe"]["glossary"] = []                                     # before the glossary existed
    item["transcript_words"] = json.dumps({"cut_start": 10.0, "cut_end": 30.0, "model": "base.en",
                                           "key": transcribe.cache_key(old), "words": words})
    assert render.cached_cut_words(item, transcribe.cache_key(old)) == words
    assert render.cached_cut_words(item, transcribe.cache_key(CFG)) is None
    legacy = {**item, "transcript_words": json.dumps({"cut_start": 10.0, "cut_end": 30.0, "model": "base.en",
                                                      "words": words})}    # pre-key payloads never count
    assert render.cached_cut_words(legacy, transcribe.cache_key(CFG)) is None


def test_the_cut_stage_stores_its_transcript_under_the_current_key():
    choice = cut.EndChoice(1.0, 20.0, "sentence", [{"word": "x", "start": 0.0, "end": 0.2}])
    payload = json.loads(cut.transcript_payload(choice, transcribe.cache_key(CFG)))
    assert payload["key"] == transcribe.cache_key(CFG) and payload["model"] == transcribe.model_name(CFG)


# ----------------------------------------------------------------------------- every call site

def test_cut_stage_span_transcription_passes_the_glossary(tmp_path, fake_models):
    src = _tone(tmp_path / "src.wav", 2.0)
    words = cut.transcribe_source_span(src, 0.5, 1.5, cfg=CFG)
    assert [w["word"] for w in words] == ["wanted", "level."] and words[0]["start"] == pytest.approx(0.6)
    _assert_glossary(fake_models, 1)


def test_hooks_transcription_passes_the_glossary(tmp_db, tmp_path, fake_models):
    clip = _tone(tmp_path / "cut.wav")
    item = {"id": "x", "cut_start": 1.0, "cut_end": 21.0}
    assert hooks._cut_transcript(item, clip, CFG, tmp_db)
    _assert_glossary(fake_models, 1)


def test_render_cut_fallback_and_vo_pass_the_glossary(tmp_path, fake_models):
    clip, vo = _tone(tmp_path / "cut.wav"), _tone(tmp_path / "vo.wav")
    assert render.cut_speech_words({"id": "x"}, clip, transcribe.cache_key(CFG), CFG)     # no cache: transcribe
    assert render.transcribe_words(vo, cfg=CFG)
    assert render.transcribe_words(vo)                                                    # default cfg too
    _assert_glossary(fake_models, 3)


def test_run_render_item_transcribes_the_vo_with_the_glossary(tmp_db, tmp_path, fake_models):
    cut_file, vo = tmp_path / "cut.mp4", _tone(tmp_path / "vo.wav")
    cut_file.write_bytes(b"0")
    db.insert_discovered_item({"id": "item_r", "source": "youtube", "source_id": "vid_r",
                               "source_url": "https://example.invalid/r", "author_name": "Creator",
                               "raw_velocity": 1.0}, db_path=tmp_db)
    db.transition_stage("item_r", "cut", extra_fields={
        "cut_path": str(cut_file), "cut_start": 10.0, "cut_end": 30.0, "cut_duration": 20.0,
        "hook_text": "IS THIS VICE CITY?", "voiceover_script": "Is this Vice City?",
        "voiceover_audio_path": str(vo), "keywords": '["VICE"]',
    }, db_path=tmp_db)
    render.store_cut_words("item_r", db.get_item("item_r", db_path=tmp_db),
                           [{"word": "VICE", "start": 0.5, "end": 0.9}], transcribe.cache_key(CFG), db_path=tmp_db)
    fake = {"elapsed_a_s": 1.0, "elapsed_b_s": 1.0, "probe_a": {"video_duration": 20.0},
            "probe_b": {"video_duration": 23.0}, "preset": "P0"}
    with patch.object(render.config, "media_dir", side_effect=lambda sub="": tmp_path / sub), \
         patch.object(render, "verify_video_file", return_value=(True, 20.0)), \
         patch.object(render, "render_item_variants", return_value=fake):
        ok, msg, _ = render.run_render_item("item_r", cfg=CFG, db_path=tmp_db)
    assert ok, msg
    _assert_glossary(fake_models, 1)                      # the VO only: the cut words came from the cache
    assert fake_models[transcribe.model_name(CFG)].calls[0]["path"] == str(vo)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_t", ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_t"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_preview_and_verify_scripts_transcribe_with_the_glossary(tmp_path, fake_models):
    verify = _load_script("verify_previews")
    clip = _tone(tmp_path / "P0_A.wav", 4.0)
    dur, words = verify.tail_words(clip, CFG)
    assert words
    _assert_glossary(fake_models, 1)


# ----------------------------------------------------------------------------- loading

def test_each_model_is_loaded_once_and_a_second_model_really_is_a_second_model(monkeypatch):
    loaded: list[str] = []

    class FakeWhisper:
        def __init__(self, name, **kw):
            loaded.append(name)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisper))
    monkeypatch.setattr(transcribe, "_MODELS", {})
    monkeypatch.setattr(transcribe, "download_root", lambda: None)
    a1, a2, b = transcribe.get_model("base.en"), transcribe.get_model("base.en"), transcribe.get_model("small.en")
    assert a1 is a2 and a1 is not b and loaded == ["base.en", "small.en"]
