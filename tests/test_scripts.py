"""Zero-network tests for the two laptop scripts (everything external is mocked).

scripts/fetch_assets.py and scripts/render_previews.py cannot be exercised for real from
the cloud sandbox (no YouTube, no OpenGameArt, no Kokoro/whisper weights), so their
orchestration is tested with mocks: what they call, in what order, what they write, and
what they do when a step fails.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod                       # dataclasses need the module registered
    spec.loader.exec_module(mod)
    return mod


fetch_assets = _load("fetch_assets_script", ROOT / "scripts" / "fetch_assets.py")
render_previews = _load("render_previews_script", ROOT / "scripts" / "render_previews.py")

from pipeline import config, style  # noqa: E402

SS = style.style_settings()


# ============================================================ fetch_assets.py

def _oga_page(file_url: str, licence: str = "CC0") -> bytes:
    """The shape of an OpenGameArt page's licence field, as served in Nov 2026."""
    return (
        '<div class="field field-name-field-art-licenses field-type-taxonomy-term-reference '
        'field-label-above"><div class="field-label">License(s):&nbsp;</div>'
        f'<div class="license-name">{licence}</div></div>'
        '<div class="field-label">Collections:&nbsp;</div>'
        f'<a href="{file_url}">track.mp3</a>'
    ).encode()


def test_fetch_assets_music_candidates_are_opengameart_pages_and_files():
    assert fetch_assets.MUSIC_CANDIDATES
    for page_url, file_url in fetch_assets.MUSIC_CANDIDATES:
        assert page_url.startswith("https://opengameart.org/content/")
        assert file_url.startswith("https://opengameart.org/sites/default/files/")
        assert file_url.endswith(".mp3")
    assert "CC0" in fetch_assets.MUSIC_LICENSE
    assert "CC0" in fetch_assets.KENNEY_LICENSE


def test_page_says_cc0_only_for_an_exact_cc0_licence_that_links_the_file():
    url = "https://opengameart.org/sites/default/files/a.mp3"
    assert fetch_assets.page_says_cc0(_oga_page(url).decode(), url)[0] is True
    assert fetch_assets.page_says_cc0(_oga_page(url, "CC-BY 4.0").decode(), url)[0] is False
    assert fetch_assets.page_says_cc0(_oga_page(url, "CC0 CC-BY 3.0").decode(), url)[0] is False
    assert fetch_assets.page_says_cc0(_oga_page(url).decode(), url + "x")[0] is False
    assert fetch_assets.page_says_cc0("<html>no licence</html>", url)[0] is False


def test_fetch_assets_is_idempotent_when_the_bed_already_verifies(tmp_path):
    dest = tmp_path / "bed.mp3"
    dest.write_bytes(b"x" * 200_000)
    with patch.object(fetch_assets, "audio_seconds", return_value=120.0),          patch.object(fetch_assets, "_get") as get:
        assert fetch_assets.fetch_music(dest) is True
        get.assert_not_called()                      # nothing downloaded a second time


def test_fetch_assets_downloads_the_first_candidate_that_verifies(tmp_path):
    dest = tmp_path / "music" / "bed.mp3"
    (page1, file1), (page2, file2) = fetch_assets.MUSIC_CANDIDATES[:2]
    calls: list[str] = []

    def fake_get(url, timeout=60):
        calls.append(url)
        if url == page1:
            return _oga_page(file1, "CC-BY 3.0")         # first candidate is not CC0
        if url == page2:
            return _oga_page(file2)
        return b"y" * 300_000

    with patch.object(fetch_assets, "_get", side_effect=fake_get),          patch.object(fetch_assets, "audio_seconds", return_value=95.0):
        assert fetch_assets.fetch_music(dest) is True
    assert calls == [page1, page2, file2]                # the non-CC0 file is never fetched
    assert dest.exists() and not dest.with_suffix(".mp3.part").exists()
    lic = (dest.parent / "LICENSES.md").read_text()
    assert "CC0" in lic and file2 in lic and page2 in lic and "bed.mp3" in lic


def test_fetch_assets_rejects_a_download_that_is_not_real_audio(tmp_path):
    dest = tmp_path / "bed.mp3"
    pages = {p: _oga_page(f) for p, f in fetch_assets.MUSIC_CANDIDATES}
    with patch.object(fetch_assets, "_get", side_effect=lambda u, timeout=60: pages.get(u, b"<html>404</html>")),          patch.object(fetch_assets, "audio_seconds", return_value=0.0):
        assert fetch_assets.fetch_music(dest) is False
    assert not dest.exists()
    assert not (dest.parent / "LICENSES.md").exists()


def test_fetch_assets_rejects_a_bed_that_is_too_short(tmp_path):
    dest = tmp_path / "bed.mp3"
    dest.write_bytes(b"x" * 200_000)
    with patch.object(fetch_assets, "audio_seconds", return_value=5.0):
        ok, why = fetch_assets.verify_music(dest)
    assert ok is False and "5.0s" in why


def test_fetch_assets_synthesizes_missing_sfx(tmp_path):
    ss = {**SS, "sfx": {**SS["sfx"], "dir": str(tmp_path / "sfx")}}
    with patch("pipeline.audio_mix.ensure_sfx_assets") as ensure:
        fetch_assets.fetch_sfx(ss)
        ensure.assert_called_once()


def test_fetch_assets_kenney_pack_needs_a_license_file(tmp_path):
    import io
    import zipfile

    def make_zip(names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, b"RIFF0000WAVE")
        return buf.getvalue()

    files = {"boom": tmp_path / "boom.wav", "whoosh": tmp_path / "whoosh.wav"}
    with patch.object(fetch_assets, "_get", return_value=make_zip(["pack/impact1.wav", "pack/swoosh1.wav"])):
        assert fetch_assets.install_kenney_sfx(files) is False      # no license file
    with patch.object(fetch_assets, "_get", return_value=make_zip(["pack/License.txt", "pack/music.wav"])):
        assert fetch_assets.install_kenney_sfx(files) is False      # no impact/whoosh samples


def test_fetch_assets_verifies_every_mapped_emoji_is_committed():
    assert fetch_assets.verify_emoji(SS) is True


# ============================================================ render_previews.py

def test_render_previews_targets_the_three_cp2_items():
    assert render_previews.CP2_ITEM_IDS == [
        "item_a35aead652d3", "item_763c2e191a1f", "item_4622278cd0fc",
    ]
    assert render_previews.PRESETS == ["P0", "P1", "P2", "P3"]
    assert (render_previews.TILE_W, render_previews.TILE_H) == (540, 960)


def test_render_previews_grid_is_a_silent_labelled_2x2_xstack(tmp_path):
    tiles = [(p, tmp_path / f"{p}.mp4", d) for p, d in
             (("P0", 20.0), ("P1", 19.2), ("P2", 19.2), ("P3", 19.2))]
    cmd = render_previews.build_grid_cmd(tiles, tmp_path / "grid_A.mp4")
    assert cmd[:2] == ["ffmpeg", "-y"] and "-an" in cmd            # silent
    assert cmd.count("-i") == 4
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0" in fc
    assert fc.count("scale=540:960") == 4
    for label in ("P0", "P1", "P2", "P3"):
        assert f"text='{label}'" in fc
    # Shorter presets are clone-padded to the longest so none is cut off.
    assert fc.count("tpad=stop=-1:stop_mode=clone") == 3
    assert "stop_duration=0.800" in fc
    assert str(tmp_path / "grid_A.mp4") == cmd[-1]


def test_render_previews_grid_needs_at_least_two_tiles(tmp_path):
    assert render_previews.build_grid([("P0", tmp_path / "a.mp4", 1.0)], tmp_path / "g.mp4") == (False, 0.0)


def test_render_previews_keywords_prefer_gemini_when_the_key_is_set(tmp_path):
    item = {"id": "item_x", "original_title": "GTA 6 trailer"}
    words = [{"word": "VICE", "start": 0.0, "end": 0.3}]
    cfg = config.get_config()
    with patch.object(render_previews.config, "secret", return_value="KEY"), \
         patch.object(render_previews.hooks, "generate_hook_and_voiceover",
                      return_value={"keywords": ["lucia", "VICE", "map"]}) as gen, \
         patch.object(render_previews.kwmod, "store_keywords") as store:
        kws, src = render_previews.resolve_keywords(item, words, tmp_path / "cut.mp4", cfg)
    assert src == "gemini" and kws == ["LUCIA", "VICE", "MAP"]
    gen.assert_called_once()
    store.assert_called_once()


def test_render_previews_keywords_fall_back_to_the_rules_without_a_key(tmp_path):
    item = {"id": "item_x", "original_title": "GTA 6"}
    words = [{"word": w, "start": i, "end": i + 0.3} for i, w in enumerate(["THE", "LUCIA", "MAP", "SHOWS", "2026"])]
    cfg = config.get_config()
    with patch.object(render_previews.config, "secret", return_value=""), \
         patch.dict("os.environ", {}, clear=True), \
         patch.object(render_previews.hooks, "generate_hook_and_voiceover") as gen, \
         patch.object(render_previews.kwmod, "store_keywords"):
        kws, src = render_previews.resolve_keywords(item, words, tmp_path / "cut.mp4", cfg)
    gen.assert_not_called()
    assert src == "rules" and "LUCIA" in kws and "2026" in kws and "SHOWS" not in kws


def test_render_previews_keywords_fall_back_when_gemini_raises(tmp_path):
    item = {"id": "item_x", "original_title": "GTA 6 map leak"}
    words = [{"word": "MAP", "start": 0.0, "end": 0.3}]
    with patch.object(render_previews.config, "secret", return_value="KEY"), \
         patch.object(render_previews.hooks, "generate_hook_and_voiceover", side_effect=RuntimeError("429")), \
         patch.object(render_previews.kwmod, "store_keywords"):
        kws, src = render_previews.resolve_keywords(item, words, tmp_path / "cut.mp4", config.get_config())
    assert src == "rules" and kws == ["MAP"]


def test_render_previews_cached_keywords_skip_both_paths(tmp_path):
    item = {"id": "item_x", "keywords": '["VICE", "CITY"]'}
    with patch.object(render_previews.hooks, "generate_hook_and_voiceover") as gen:
        kws, src = render_previews.resolve_keywords(item, [], tmp_path / "cut.mp4", config.get_config())
    gen.assert_not_called()
    assert kws == ["VICE", "CITY"] and src == "cached (db)"


def _verified_payload(cut_start=5.0, cut_end=25.0, words=None, ending="sentence"):
    import json
    key = render_previews.transcribe.cache_key()
    return json.dumps({"cut_start": cut_start, "cut_end": cut_end, "key": key,
                       "words": words or [], "ending": ending, "ending_rule": render_previews.endings.RULE_VERSION, "source": "cut"})


def _bound(item: dict, words=None) -> dict:
    """The hook / voiceover records the hooks and voiceover stages write for the item's segment
    (without them the preview script regenerates the hook instead of reusing it)."""
    from pipeline import binding
    item.setdefault("cut_start", 5.0)
    item.setdefault("cut_end", 25.0)
    script = item.get("voiceover_script") or ""
    item["hook_binding"] = binding.hook_record(item, item["hook_text"], script,
                                               binding.transcript_text(words), "segment_check")
    item["voiceover_binding"] = binding.voiceover_record(item, script, item["voiceover_audio_path"])
    return item


def test_render_previews_reuses_an_existing_download_and_a_current_cut(tmp_path):
    src, cut = tmp_path / "src.mp4", tmp_path / "cut.mp4"
    src.write_bytes(b"0")
    cut.write_bytes(b"0")
    item = {"id": "i", "download_path": str(src), "cut_path": str(cut),
            "cut_start": 5.0, "cut_end": 25.0, "transcript_words": _verified_payload()}
    with patch.object(render_previews.download, "download_single_item") as dl, \
         patch.object(render_previews.cutmod, "run_cut_item") as rc:
        assert render_previews.ensure_source(item, {}) == src
        assert render_previews.ensure_cut(item, config.get_config()) == cut
    dl.assert_not_called()
    rc.assert_not_called()


def test_render_previews_recuts_a_cut_that_predates_the_sentence_end_check(tmp_path):
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(b"0")
    old = {"id": "i", "cut_path": str(cut), "cut_start": 5.0, "cut_end": 25.0,
           "transcript_words": _verified_payload(ending=None)}           # hooks-era cache: no verdict
    new = {**old, "transcript_words": _verified_payload()}
    with patch.object(render_previews.cutmod, "run_cut_item", return_value=(True, "ok")) as rc, \
         patch.object(render_previews.db, "get_item", return_value=new):
        assert render_previews.ensure_cut(old, {}) == cut
    rc.assert_called_once()


def test_render_previews_swaps_in_the_next_real_item_on_no_clean_ending(tmp_path):
    def fake_render_item(item_id, *a, **k):
        r = render_previews.ItemResult(item_id=item_id)
        if item_id == "item_b":
            r.message, r.no_clean_ending = "cut failed: no_clean_ending: speech runs through", True
        else:
            r.ok, r.message = True, "1/1 presets rendered"
        return r

    with patch.object(render_previews.db, "init_db"), \
         patch.object(render_previews, "default_items", return_value=["item_a", "item_b", "item_c"]), \
         patch.object(render_previews, "candidate_items", return_value=["item_a", "item_b", "item_c", "item_d"]), \
         patch.object(render_previews, "render_item", side_effect=fake_render_item) as ri:
        rc = render_previews.main(["--presets", "P0", "--out", str(tmp_path)])
    assert rc == 0
    assert [c.args[0] for c in ri.call_args_list] == ["item_a", "item_b", "item_c", "item_d"]
    text = (tmp_path / "README.txt").read_text()
    assert "item_d  (swapped in for item_b: no_clean_ending)" in text


def test_render_previews_download_failure_is_reported_not_swallowed(tmp_path):
    item = {"id": "i", "source_url": "https://example.invalid/v"}
    with patch.object(render_previews.download, "download_single_item", return_value=(False, "blocked")), \
         patch.object(render_previews.db, "get_item", return_value={}):
        with pytest.raises(RuntimeError, match="blocked"):
            render_previews.ensure_source(item, {})


def test_render_previews_missing_item_is_a_clean_failure(tmp_path):
    with patch.object(render_previews.db, "get_item", return_value=None):
        r = render_previews.render_item("item_nope", config.get_config(), ["P0"], tmp_path)
    assert r.ok is False and "pipeline.db" in r.message


def test_render_previews_renders_every_preset_and_writes_the_readme(tmp_path):
    cut, vo = tmp_path / "cut.mp4", tmp_path / "vo.wav"
    cut.write_bytes(b"0")
    vo.write_bytes(b"0")
    item = _bound({"id": "item_a35aead652d3", "download_path": str(cut), "cut_path": str(cut),
                   "voiceover_audio_path": str(vo), "hook_text": "IS THIS REAL?",
                   "author_name": "GamingBolt", "keywords": '["VICE","MAP"]', "voiceover_script": "hi"})

    def fake_variants(*a, **k):
        name = k["name"]
        Path(a[5]).write_bytes(b"A")
        Path(a[6]).write_bytes(b"B")
        subs = Path(a[7])
        subs.mkdir(parents=True, exist_ok=True)
        (subs / f"{name}_A.ass").write_text("x")
        (subs / f"{name}_B.ass").write_text("x")
        return {
            "probe_a": {"video_duration": 19.2}, "probe_b": {"video_duration": 22.7},
            "elapsed_a_s": 12.0, "elapsed_b_s": 14.0,
            "ass_a": str(subs / f"{name}_A.ass"), "ass_b": str(subs / f"{name}_B.ass"),
            "style_a": {"preset": k["preset"]},
        }

    with patch.object(render_previews.db, "get_item", return_value=item), \
         patch.object(render_previews, "ensure_cut", return_value=cut), \
         patch.object(render_previews.render, "transcribe_words", return_value=[{"word": "VICE", "start": 0.0, "end": 0.4}]), \
         patch.object(render_previews.render, "render_item_variants", side_effect=fake_variants) as rv, \
         patch.object(render_previews, "build_grid", return_value=(True, 3.0)) as grid:
        r = render_previews.render_item("item_a35aead652d3", config.get_config(),
                                        ["P0", "P1", "P2", "P3"], tmp_path)

    assert r.ok and len(r.renders) == 4 and all(v["ok"] for v in r.renders.values())
    assert rv.call_count == 4
    assert [c.kwargs["preset"] for c in rv.call_args_list] == ["P0", "P1", "P2", "P3"]
    for c in rv.call_args_list:
        assert c.kwargs["channel_name"] == "GamingBolt"
        assert c.kwargs["keyword_list"] == ["VICE", "MAP"]
    for preset in ("P0", "P1", "P2", "P3"):
        assert (tmp_path / "item_a35aead652d3" / f"{preset}_A.mp4").exists()
        assert (tmp_path / "item_a35aead652d3" / f"{preset}_B.mp4").exists()
    assert grid.call_count == 2 and set(r.grids) == {"A", "B"}

    readme = render_previews.write_readme(tmp_path, [r], ["P0", "P1", "P2", "P3"])
    text = readme.read_text()
    assert readme.name == "README.txt"
    for preset in ("P0 Control", "P1 Clean", "P2 Punchy", "P3 Full hype"):
        assert preset in text
    assert "item_a35aead652d3" in text and "GamingBolt" in text and "VICE, MAP" in text
    assert "grid_A.mp4" in text and "12.0s to render" in text
    assert "style.preset" in text


def test_render_previews_a_failed_preset_does_not_stop_the_others(tmp_path):
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(b"0")
    item = _bound({"id": "i", "download_path": str(cut), "cut_path": str(cut),
                   "voiceover_audio_path": str(cut), "hook_text": "H", "author_name": "C"})
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if k["preset"] == "P1":
            raise render_previews.render.RenderVerificationError("guard failed")
        subs = Path(a[7])
        subs.mkdir(parents=True, exist_ok=True)
        for v in "AB":
            (subs / f"{k['name']}_{v}.ass").write_text("x")
        return {"probe_a": {"video_duration": 1.0}, "probe_b": {"video_duration": 1.0},
                "elapsed_a_s": 1.0, "elapsed_b_s": 1.0,
                "ass_a": str(subs / f"{k['name']}_A.ass"), "ass_b": str(subs / f"{k['name']}_B.ass"),
                "style_a": {}}

    with patch.object(render_previews.db, "get_item", return_value=item), \
         patch.object(render_previews, "ensure_cut", return_value=cut), \
         patch.object(render_previews.render, "transcribe_words", return_value=[]), \
         patch.object(render_previews.render, "render_item_variants", side_effect=flaky), \
         patch.object(render_previews, "build_grid", return_value=(True, 1.0)):
        r = render_previews.render_item("i", config.get_config(), ["P0", "P1", "P2"], tmp_path)
    assert calls["n"] == 3 and r.ok is True
    assert r.renders["P1"]["ok"] is False and r.renders["P2"]["ok"] is True
    assert "guard failed" in render_previews.write_readme(tmp_path, [r], ["P0", "P1", "P2"]).read_text()


def test_render_previews_reuses_the_hooks_stage_cut_words(tmp_path):
    """Cached cut words (matching window + model) are used; only the VO is transcribed."""
    import json
    cut, vo = tmp_path / "cut.mp4", tmp_path / "vo.wav"
    cut.write_bytes(b"0")
    vo.write_bytes(b"0")
    words = [{"word": "VICE", "start": 0.1, "end": 0.5}]
    item = _bound({"id": "i", "download_path": str(cut), "cut_path": str(cut), "voiceover_audio_path": str(vo),
                   "hook_text": "H", "author_name": "C", "keywords": '["VICE","CITY","MAP"]',
                   "cut_start": 5.0, "cut_end": 25.0, "transcript_words": _verified_payload(words=words)}, words)

    def ok_variants(*a, **k):
        subs = Path(a[7])
        subs.mkdir(parents=True, exist_ok=True)
        for v in "AB":
            (subs / f"{k['name']}_{v}.ass").write_text("x")
        return {"probe_a": {"video_duration": 1.0}, "probe_b": {"video_duration": 1.0},
                "elapsed_a_s": 1.0, "elapsed_b_s": 1.0,
                "ass_a": str(subs / f"{k['name']}_A.ass"), "ass_b": str(subs / f"{k['name']}_B.ass"),
                "style_a": {}}

    with patch.object(render_previews.db, "get_item", return_value=item), \
         patch.object(render_previews.render, "transcribe_words", return_value=[{"word": "HI", "start": 0, "end": 1}]) as tw, \
         patch.object(render_previews.render, "render_item_variants", side_effect=ok_variants) as rv, \
         patch.object(render_previews, "build_grid", return_value=(True, 1.0)):
        r = render_previews.render_item("i", config.get_config(), ["P0"], tmp_path)
    assert r.ok
    assert [Path(c.args[0]) for c in tw.call_args_list] == [vo]
    assert rv.call_args.args[3] == words
    assert rv.call_args.kwargs["clean_cut_end"] is True        # the cut stage verified this end


def test_render_previews_rejects_an_unknown_preset_before_rendering(tmp_path):
    with patch.object(render_previews.db, "init_db"), \
         patch.object(render_previews, "render_item") as ri:
        with pytest.raises(KeyError):
            render_previews.main(["--presets", "P9", "--out", str(tmp_path)])
    ri.assert_not_called()


def test_render_previews_lists_every_caption_word_per_variant(tmp_path):
    r = render_previews.ItemResult(item_id="item_x", ok=True)
    words_a = ["get", "rid", "of", "your", "wanted", "level."]
    r.renders = {
        "P0": {"ok": True, "a": "a", "b": "b", "a_s": 1.0, "b_s": 1.0, "elapsed_a_s": 1.0, "elapsed_b_s": 1.0,
               "captions_a": words_a, "captions_b": ["VO"] + words_a},
        "P1": {"ok": True, "a": "a", "b": "b", "a_s": 1.0, "b_s": 1.0, "elapsed_a_s": 1.0, "elapsed_b_s": 1.0,
               "captions_a": words_a[:-2], "captions_b": ["VO"] + words_a},
    }
    text = render_previews.write_readme(tmp_path, [r], ["P0", "P1"]).read_text(encoding="utf-8")
    assert "captions A (P0, 6 words):" in text and "get rid of your wanted level." in text
    assert "captions A (P1, 4 words):" in text                       # a preset that ended earlier
    assert "captions B (P0/P1, 7 words):" in text and "VO get rid of your wanted level." in text
