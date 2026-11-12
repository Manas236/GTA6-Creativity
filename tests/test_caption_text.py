"""Polish (2026-11-12): caption text formatting and the glossary.

whisper writes some tokens as separate words, so the burned-in captions read "PRE -DOWNLOAD" (and
GamingBolt's "to -do" became "TO -DO") and numbers kept their spaces ("1 001 / 004"). Only the
CAPTION text is normalised; the stored transcript stays exactly as whisper wrote it."""
from __future__ import annotations

import copy
import json

from pipeline import config, db, plan as planning, render, transcribe
from pipeline.rendercore import normalize_caption_words

CFG = config.get_config()


def _w(*tokens: str) -> list[dict]:
    return [{"word": t, "start": round(0.5 + 0.3 * i, 3), "end": round(0.75 + 0.3 * i, 3), "prob": 0.9}
            for i, t in enumerate(tokens)]


def _captions(words) -> str:
    return render.build_ass_subtitles("HOOK", 6.0, words, cfg=CFG)


def test_pre_download_is_one_caption_word():
    words = _w("the", "pre", "-download", "is", "live.")
    out = normalize_caption_words(words)
    assert [w["word"] for w in out] == ["the", "PRE-DOWNLOAD", "is", "live."]
    joined = out[1]
    assert (joined["start"], joined["end"]) == (words[1]["start"], words[2]["end"])
    ass = _captions(words)
    assert "PRE-DOWNLOAD" in ass and "PRE -DOWNLOAD" not in ass
    assert [w["word"] for w in normalize_caption_words(_w("our", "to", "-do", "list"))][1] == "TO-DO"


def test_numbers_lose_the_spacing_artefacts():
    out = normalize_caption_words(_w("patch", "1", "001", "/", "004", "is", "out."))
    assert [w["word"] for w in out] == ["patch", "1,001/004", "is", "out."]
    ass = _captions(_w("patch", "1", "001", "/", "004", "is", "out."))
    assert "1,001/004" in ass and "1 001" not in ass and "/ 004" not in ass


def test_ordinary_words_are_left_alone_and_it_is_idempotent():
    words = _w("GTA", "6", "drops", "in", "2026", "-", "x-ray", "Lucia's", "car,", "200", "miles")
    out = normalize_caption_words(words)
    assert out == [dict(w) for w in words]
    tricky = _w("pre", "-download", "1", "001", "/", "004")
    once = normalize_caption_words(tricky)
    assert normalize_caption_words(once) == once


def test_the_stored_transcript_is_never_normalised(tmp_db, tmp_path):
    from test_caption_binding import make_item

    words = _w("the", "pre", "-download", "is", "live.")
    item = make_item(tmp_db, tmp_path, "item_c", words=words)
    before = copy.deepcopy(words)
    sp = planning.plan_from_words("HOOK?", 6.0, render.cached_cut_words(item, transcribe.cache_key(CFG)), cfg=CFG)
    assert "PRE-DOWNLOAD" in [w["word"] for w in sp.words]                 # the caption side
    assert words == before                                                 # the caller's list is untouched
    stored = json.loads(db.get_item("item_c", db_path=tmp_db)["transcript_words"])["words"]
    assert [w["word"] for w in stored] == ["the", "pre", "-download", "is", "live."]


def test_glossary_has_caminos_and_strauss_zelnick():
    terms = transcribe.settings(CFG)["glossary"]
    assert "Caminos" in terms and "Strauss Zelnick" in terms
    assert "Caminos" in transcribe.prompt_kwargs(transcribe.settings(CFG))["hotwords"]
