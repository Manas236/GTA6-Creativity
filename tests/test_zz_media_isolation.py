"""Runs last (file name sorts after every other test module): the suite must not have written,
changed or deleted anything in the project's real media/ folder."""
from __future__ import annotations

from pathlib import Path

from pipeline import config

REAL_MEDIA_ROOT = config.ROOT_DIR / "media"


def test_media_dir_points_into_tmp_path_during_tests(tmp_path: Path) -> None:
    downloads = config.media_dir("downloads")
    assert downloads == tmp_path / "media" / "downloads"
    assert REAL_MEDIA_ROOT not in downloads.parents


def test_media_root_can_be_overridden_by_env(monkeypatch, tmp_path: Path) -> None:
    config.set_media_root(None)
    monkeypatch.setenv("GTA6_MEDIA_DIR", str(tmp_path / "elsewhere"))
    assert config.media_dir("cuts") == tmp_path / "elsewhere" / "cuts"
    monkeypatch.delenv("GTA6_MEDIA_DIR")
    assert config.media_root() == REAL_MEDIA_ROOT


def test_the_suite_left_nothing_in_the_real_media_folder(request) -> None:
    before = request.config._real_media_before
    after = request.config._media_snapshot()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    assert not (added or removed or changed), (
        f"tests touched the real media/ folder - added {added[:10]}, removed {removed[:10]}, "
        f"changed {changed[:10]}. Use config.media_dir() (redirected to tmp_path) or tmp_path."
    )
    if not request.config._real_media_existed:
        assert not REAL_MEDIA_ROOT.exists(), "tests created the real media/ folder"
