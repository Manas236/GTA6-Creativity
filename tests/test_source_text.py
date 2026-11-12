"""Fix 4 (2026-11-12): our captions / credit / progress bar vs text burned into the source.

GamingBolt's chapter titles and Hazard's lower third sit where zoom-to-fill puts our captions, and a
subscribe pop-up sat under our credit (11 Nov office preview). pipeline.textdetect finds such text on
frames framed exactly like the render; style.plan_layout moves our text off it, or the item goes to
needs_review (source_text_collision). Synthetic clips: a smooth moving gradient (no text) with
drawtext lines burned in."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from pipeline import config, db, edits, plan as planning, render, style, telegram_bot, textdetect
from pipeline.rendercore import escape_filter_path

CFG = config.get_config()
FONT = config.ROOT_DIR / "assets" / "fonts" / "Montserrat-Black.ttf"
WORDS = [{"word": w, "start": 0.5 + 0.4 * i, "end": 0.8 + 0.4 * i}
         for i, w in enumerate("Lucia drives through Vice City tonight".split())]
FILL = [edits.Segment(0.0, 4.0, "fill")]
W, H = 1080, 1920


def _clip(path: Path, lines: list[tuple[float, str]], seconds: float = 4.0, until: float | None = None) -> Path:
    """1280x720 moving gradient (no text) with `lines` [(y as a fraction of h, text)] burned in, centred
    (only for the first `until` seconds when given)."""
    bg = (f"nullsrc=s=1280x720:r=25:d={seconds},format=rgb24,"
          "geq=r='128+90*sin(2*PI*(X/420+T/3))':g='120+90*sin(2*PI*(Y/310+T/4))':b='128+60*sin(2*PI*((X+Y)/600+T/5))'")
    font = escape_filter_path(FONT)
    enable = f":enable='lt(t\\,{until})'" if until else ""
    draws = [f"drawtext=fontfile='{font}':text='{t}':x=(w-text_w)/2:y=h*{y}:fontsize=40:fontcolor=white:"
             f"borderw=3:bordercolor=black{enable}" for y, t in lines]
    vf = ",".join([bg, *draws, "format=yuv420p"])
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", vf,
                    "-c:v", "libx264", "-preset", "ultrafast", str(path)], check=True, capture_output=True,
                   cwd=config.ROOT_DIR)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("src_text")
    every = [(round(0.03 + 0.08 * i, 2), f"LINE {i} OF BURNED IN TEXT") for i in range(12)]
    return {
        "clean": _clip(d / "clean.mp4", []),
        "bottom": _clip(d / "bottom.mp4", [(0.87, "THE BEAUTY IN THE MUNDANE")]),
        "every": _clip(d / "every.mp4", every),
        "first": _clip(d / "first.mp4", [(0.85, "NOW 5 DOLLARS OR LESS")], until=0.5),
    }


def _detect(clip: Path) -> textdetect.Occupancy:
    occ = textdetect.detect_source_text(clip, 4.0, FILL, CFG)
    assert occ is not None and occ.frames == textdetect.settings(CFG)["samples"]
    return occ


def _plan(preset: str, occupancy=None, variant_b: bool = False, words=WORDS) -> planning.StylePlan:
    return planning.plan_from_words("IS LUCIA BACK IN VICE CITY?", 4.0, words, cfg=CFG, preset=preset,
                                    channel_name="GamingBolt", occupancy=occupancy,
                                    freeze_s=2.0 if variant_b else 0.0,
                                    vo_words=[{"word": "Look", "start": 0.1, "end": 0.5}] if variant_b else None)


def _ass(sp: planning.StylePlan) -> str:
    return render.build_ass_subtitles("IS LUCIA BACK IN VICE CITY?", sp.total_s, sp.words, cfg=CFG, sp=sp)


# ----------------------------------------------------------------------------- detection

def test_burned_in_text_at_the_bottom_is_found_in_the_caption_band(clips):
    occ = _detect(clips["bottom"])
    assert not occ.empty
    default = style.plan_layout(["fill"], 150, 2, render.render_settings(CFG), style.style_settings(CFG),
                                planning.cap_height_px, text_box_px=planning._text_box_px)
    assert occ.occupied(90, default.caption_ink[0], 990, default.caption_ink[1])


def test_a_clean_moving_clip_has_no_source_text(clips):
    assert _detect(clips["clean"]).empty


def test_text_line_boxes_on_a_drawn_line_and_on_a_plain_gradient():
    import cv2

    img = np.tile(np.linspace(40, 200, W, dtype=np.uint8), (300, 1))
    assert textdetect.text_line_boxes(img) == []
    cv2.putText(img, "PRE-ORDER NOW", (200, 180), cv2.FONT_HERSHEY_DUPLEX, 2.0, 255, 4)
    boxes = textdetect.text_line_boxes(img)
    assert boxes and any(b[0] < 260 and b[2] > 600 and 120 < b[3] <= 200 for b in boxes)


# ----------------------------------------------------------------------------- layout

def test_captions_relocate_off_burned_in_text_at_the_bottom(clips):
    occ = _detect(clips["bottom"])
    for preset in ("P0", "P2"):
        default, moved = _plan(preset), _plan(preset, occ)
        assert moved.layout.caption_center_y != default.layout.caption_center_y
        assert not occ.occupied(90, moved.layout.caption_ink[0], 990, moved.layout.caption_ink[1])
        hook0, hook1 = moved.layout.hook_ink
        assert moved.layout.caption_ink[0] >= hook1 or moved.layout.caption_ink[1] <= hook0   # not on the hook
        assert any("captions moved" in n for n in moved.layout.notes)
        assert f"\\pos(540,{moved.layout.caption_center_y})" in _ass(moved)
        assert 250 <= moved.layout.caption_ink[0] and moved.layout.caption_ink[1] <= 1440     # safe area
    p2 = _plan("P2", occ)
    cx0, cy0, cy1 = p2.layout.credit_x, p2.layout.credit_ink[0], p2.layout.credit_ink[1]
    assert not occ.occupied(cx0, cy0, cx0 + 300, cy1)                                       # credit clear too


def test_text_in_every_band_sends_the_item_to_review(clips):
    occ = _detect(clips["every"])
    with pytest.raises(style.SourceTextCollision, match="source_text_collision"):
        _plan("P2", occ)


def test_a_clean_clip_keeps_todays_layout_byte_identical(clips):
    occ = _detect(clips["clean"])
    for preset in ("P0", "P1", "P2", "P3"):
        for b in (False, True):
            assert _ass(_plan(preset, occ, variant_b=b)) == _ass(_plan(preset, None, variant_b=b)), (preset, b)


def test_text_on_the_first_frame_moves_variant_b_only(clips):
    """Hazard's store page: on screen for half a second in A, but B freezes that frame for the whole
    voiceover, so for B it is persistent (and framed 'fit', like the freeze)."""
    occ = _detect(clips["first"])
    assert occ.empty                                                        # one sample: not persistent for A
    occ_b = occ.with_freeze()
    assert not occ_b.empty
    assert _ass(_plan("P2", occ)) == _ass(_plan("P2"))                      # A unchanged
    b_default, b_moved = _plan("P2", variant_b=True), _plan("P2", occ_b, variant_b=True)
    assert (b_moved.layout.caption_center_y, b_moved.layout.credit_y) != \
           (b_default.layout.caption_center_y, b_default.layout.credit_y)
    assert not occ_b.occupied(90, b_moved.layout.caption_ink[0], 990, b_moved.layout.caption_ink[1])


def _occ(rows: list[tuple[int, int, int, int]]) -> textdetect.Occupancy:
    mask = np.zeros((H, W), dtype=bool)
    for x0, y0, x1, y1 in rows:
        mask[y0:y1, x0:x1] = True
    return textdetect.Occupancy(mask, 8, 2500, 10, [tuple(r) for r in rows])


def test_text_under_the_credit_only_moves_the_credit_not_the_captions():
    default = _plan("P2")
    x, (y0, y1) = default.layout.credit_x, default.layout.credit_ink
    occ = _occ([(x, y0, x + 280, y1)])                                   # a subscribe pop-up under "via ..."
    moved = _plan("P2", occ)
    assert moved.layout.caption_center_y == default.layout.caption_center_y
    assert moved.layout.credit_y != default.layout.credit_y
    assert not occ.occupied(x, moved.layout.credit_ink[0], x + 280, moved.layout.credit_ink[1])


def test_text_at_the_top_edge_moves_the_progress_bar_down():
    occ = _occ([(0, 0, W, 60)])
    sp = _plan("P2", occ)
    assert sp.layout.progress_y >= 60
    assert f"\\pos(0,{sp.layout.progress_y})" in _ass(sp)
    assert "\\pos(0,0)" in _ass(_plan("P2"))


def test_no_captions_means_the_caption_band_is_not_checked():
    occ = _occ([(0, 1200, W, 1400)])
    sp = _plan("P0", occ, words=[])
    assert sp.layout.caption_center_y == _plan("P0", words=[]).layout.caption_center_y


# ----------------------------------------------------------------------------- needs_review flow

def test_render_stage_sends_a_collision_to_review_and_telegram_can_render_it_anyway(tmp_db, tmp_path):
    from test_caption_binding import FakeTransport, _tap, make_item

    make_item(tmp_db, tmp_path, "item_s")
    collision = style.SourceTextCollision("source_text_collision: no clear caption band in the safe area",
                                          {"element": "captions", "boxes": [(0, 1267, 454, 1322)]})
    with patch.object(render, "verify_video_file", return_value=(True, 20.0)), \
         patch.object(render, "transcribe_words", return_value=[]), \
         patch.object(render, "media_duration", return_value=2.0), \
         patch.object(render, "render_item_variants", side_effect=collision):
        ok, msg, _ = render.run_render_item("item_s", cfg=CFG, db_path=tmp_db)
    assert not ok and msg.startswith("needs_review: source_text_collision")
    row = db.get_item("item_s", db_path=tmp_db)
    assert row["stage"] == "needs_review" and row["error_stage"] is None
    review = json.loads(row["hook_review"])
    assert review["kind"] == "source_text_collision" and review["details"]["element"] == "captions"

    cfg = {**CFG, "telegram": {"enabled": True, "poll_timeout_s": 1, "edit_throttle_gap_s": 0}}
    t = FakeTransport()
    bot = telegram_bot.ApprovalBot(transport=t, chat_id="4242", cfg=cfg, db_path=tmp_db, clock=lambda: 0.0)
    assert bot.ensure_card() == "item_s"
    (method, params), = t.calls
    assert "LAYOUT REVIEW" in params["text"] and "source_text_collision" in params["text"]
    buttons = [b for r in params["reply_markup"]["inline_keyboard"] for b in r]
    assert [b["callback_data"] for b in buttons] == ["a:hookok:item_s", "a:no:item_s"]
    assert buttons[0]["text"] == "✅ Render anyway"
    bot.handle_update(_tap("item_s", "hookok", t.mid))
    row = db.get_item("item_s", db_path=tmp_db)
    assert row["stage"] == "cut" and json.loads(row["hook_review"])["decision"] == "render_anyway"
    assert render.source_text_override(row) is True


def test_render_anyway_skips_the_detection(tmp_path):
    item = {"hook_review": json.dumps({"kind": "source_text_collision", "decision": "render_anyway"})}
    assert render.source_text_override(item) is True
    assert render.source_text_override({"hook_review": json.dumps({"reason": "FAIL"})}) is False
    assert render.source_text_override(None) is False
