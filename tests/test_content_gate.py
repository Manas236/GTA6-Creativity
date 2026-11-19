"""pipeline.content_gate: real motion (after removing a global zoom/pan) + a CLIP gameplay check, judged
per window. The CLIP model is faked here except in the one test that skips when it is not installed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from pipeline import content_gate as cg

pytestmark = pytest.mark.content_gate

GAME = [i for i, (g, _, _) in enumerate(cg.PROMPTS) if g == "game"]
TWEET = next(i for i, (_, label, _) in enumerate(cg.PROMPTS) if label.startswith("tweet"))


class FakeClip:
    """P(game) = `game` for every crop; the rest of the mass on the tweet prompt."""

    def __init__(self, game: float):
        self.game = game

    def probs(self, frames):
        p = np.zeros((len(frames), len(cg.PROMPTS)), dtype=np.float32)
        p[:, GAME[0]] = self.game
        p[:, TWEET] = 1.0 - self.game
        return p


def _texture(seed=0, h=90, w=160):
    rng = np.random.default_rng(seed)
    import cv2
    img = rng.integers(0, 255, (h // 6, w // 6)).astype(np.float32)
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_CUBIC)


# ----------------------------------------------------------------------------- motion

def test_still_frame_has_no_motion():
    a = _texture()
    assert cg.residual_motion(a, a.copy(), 6) == 0.0


def test_ken_burns_zoom_of_a_still_is_not_motion():
    """A still slowly zoomed (a slideshow's Ken Burns effect) changes every pixel, but a global
    affine warp explains it: residual motion stays near zero."""
    import cv2
    a = _texture(1)
    m = cv2.getRotationMatrix2D((80, 45), 0, 1.03)
    b = cv2.warpAffine(a, m, (160, 90), borderMode=cv2.BORDER_REPLICATE)
    raw = float(np.mean(np.abs(b - a) > 6))
    assert raw > 0.3
    assert cg.residual_motion(a, b, 6) < 0.05


def test_independent_motion_is_motion():
    """Part of the picture moving on its own (a character, a car) is not one global warp."""
    a = _texture(2)
    b = a.copy()
    b[20:70, 30:110] = _texture(3)[20:70, 30:110]
    assert cg.residual_motion(a, b, 6) > 0.2


# ----------------------------------------------------------------------------- verdicts

def _timeline(motion, game, start=0.0, fps=2.0):
    samples = []
    for i, (m, g) in enumerate(zip(motion, game)):
        samples.append(cg.Sample(start + i / fps, None if i == 0 else m, g, "gameplay" if (g or 0) >= .5 else "tweet / social post"))
    return cg.Timeline(start, start + len(samples) / fps, samples)


def test_moving_gameplay_window_passes():
    v = cg.verdict(_timeline([0.3] * 40, [0.9, None] * 20), cfg={})
    assert v["ok"] and v["motion_share"] == 1.0 and v["game_share"] == 1.0


def test_static_window_fails_with_a_static_reason():
    v = cg.verdict(_timeline([0.0] * 40, [0.9, None] * 20), cfg={})
    assert not v["ok"] and v["reason"].startswith("static")


def test_tweet_window_fails_as_not_gameplay_and_names_what_it_is():
    v = cg.verdict(_timeline([0.3] * 40, [0.1, None] * 20), cfg={})
    assert not v["ok"] and "not_gameplay" in v["reason"] and "tweet" in v["reason"]


def test_a_window_is_judged_on_its_own_samples():
    """Gameplay for the first 20 s, tweets after: the same source passes one window, fails the next."""
    tl = _timeline([0.3] * 80, ([0.9, None] * 20) + ([0.1, None] * 20))
    assert cg.verdict(tl, 0, 20, cfg={})["ok"]
    assert not cg.verdict(tl, 20, 40, cfg={})["ok"]


def test_source_scope_uses_the_looser_floors():
    tl = _timeline([0.3] * 40, ([0.9, None] * 4) + ([0.1, None] * 16))   # 20 % gameplay
    assert not cg.verdict(tl, cfg={})["ok"]
    assert cg.verdict(tl, cfg={}, scope="source")["ok"]


# ----------------------------------------------------------------------------- scan on real video

@pytest.fixture
def moving_video(tmp_path: Path) -> Path:
    out = tmp_path / "moving.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "mandelbrot=s=640x360:rate=30",
                    "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", str(out)], check=True)
    return out


def test_scan_samples_every_half_second_and_clips_every_second(moving_video: Path):
    tl = cg.scan(moving_video, cfg={"content_gate": {"fps": 2.0, "clip_every_s": 1.0}}, model=FakeClip(0.9))
    assert 11 <= len(tl.samples) <= 13
    assert sum(s.game is not None for s in tl.samples) == len(tl.samples[::2])
    assert tl.samples[0].motion is None and all(s.motion is not None for s in tl.samples[1:])


def test_scan_of_a_span_uses_source_times(moving_video: Path):
    tl = cg.scan(moving_video, start=2.0, end=4.0, model=FakeClip(0.9))
    assert tl.samples[0].t == 2.0 and tl.samples[-1].t < 4.0


def test_crops_cover_the_long_side():
    wide = np.zeros((224, 398, 3), np.uint8)
    tall = np.zeros((398, 224, 3), np.uint8)
    assert [c.shape for c in cg._crops(wide)] == [(224, 224, 3)] * 3
    assert [c.shape for c in cg._crops(tall)] == [(224, 224, 3)] * 3


@pytest.mark.skipif(not (cg.model_dir({}) / "onnx" / "vision_model.onnx").is_file(),
                    reason="CLIP model not downloaded on this machine (assets/models/clip)")
def test_real_clip_reads_a_text_card_as_not_gameplay(tmp_path: Path):
    import shutil
    shutil.copy(Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Montserrat-Black.ttf",
                tmp_path / "font.ttf")
    out = tmp_path / "card.mp4"
    lines = ["New alleged GTA 6 Online details", "- 64-player lobbies instead of 32", "- New Fluid Access technology",
             "- Free mode activities across sessions"]
    draw = ",".join(f"drawtext=fontfile=font.ttf:text='{t}':fontsize=40:fontcolor=white:x=40:y={200 + 70 * i}"
                    for i, t in enumerate(lines))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=3",
                    "-vf", draw, "-c:v", "libx264", "-preset", "ultrafast", "card.mp4"], check=True, cwd=tmp_path)
    v = cg.judge_span(out)
    assert not v["ok"] and v["game_share"] == 0.0
