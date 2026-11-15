"""Paths shared by media serving and retention."""

import hashlib
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv"}


def video_path(value, root):
    if not value:
        return None
    try:
        video = Path(value).resolve()
        if video.is_relative_to(root) and video.suffix.lower() in VIDEO_SUFFIXES and video.is_file():
            return video
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def thumbnail_path(item_id, root):
    """Use a fixed cache directory and never follow symlink redirects."""
    folder = root / "thumbs"
    name = hashlib.sha256(item_id.encode()).hexdigest() + ".jpg"
    destination = folder / name
    try:
        if folder.resolve() == folder and destination.resolve() == destination:
            return destination
    except (OSError, RuntimeError, ValueError):
        pass
    return None
