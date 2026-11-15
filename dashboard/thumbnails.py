"""Bounded, on-demand thumbnail generation for Library rows."""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile
import threading
import time

from pipeline import db, media_paths
from .store import transaction

log = logging.getLogger(__name__)


class Thumbnails:
    def __init__(self, db_path, root):
        self.db_path = db_path
        self.root = root
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumbnail")
        self.lock = threading.Lock()
        self.pending = set()
        self.retry_at = {}

    def sources(self, item):
        if not item or item.get("rejection_reason") == "deleted from dashboard":
            return ()
        return tuple(dict.fromkeys(
            source for column in ("render_a_path", "cut_path")
            if (source := media_paths.video_path(item.get(column), self.root))
        ))

    def get(self, item_id):
        sources = self.sources(db.get_item(item_id, self.db_path))
        destination = media_paths.thumbnail_path(item_id, self.root)
        if not sources or destination is None:
            return None
        try:
            if destination.is_file() and destination.stat().st_mtime_ns >= max(
                source.stat().st_mtime_ns for source in sources
            ):
                return destination
        except OSError:
            return None
        with self.lock:
            now = time.monotonic()
            self.retry_at = {key: deadline for key, deadline in self.retry_at.items() if deadline > now}
            if item_id not in self.pending and item_id not in self.retry_at and len(self.pending) < 16:
                self.pending.add(item_id)
                self.pool.submit(self.generate, item_id, sources)
        return None

    def generate(self, item_id, sources):
        failed = False
        temporary = None
        try:
            destination = media_paths.thumbnail_path(item_id, self.root)
            if destination is None:
                return
            destination.parent.mkdir(exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, suffix=".jpg", delete=False) as file:
                temporary = Path(file.name)
            for source in sources:
                try:
                    result = subprocess.run(
                        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                         "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm",
                         "-i", str(source), "-frames:v", "1", "-vf", "scale=160:-2", str(temporary)],
                        stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                    )
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0 and temporary.stat().st_size:
                    with transaction(self.db_path):
                        current = self.sources(db.get_item(item_id, self.db_path))
                        if current == sources and media_paths.thumbnail_path(item_id, self.root) == destination:
                            temporary.replace(destination)
                    return
            failed = True
        except (OSError, sqlite3.OperationalError):
            failed = True
            log.debug("Thumbnail failed for %s", item_id, exc_info=True)
        finally:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            finally:
                with self.lock:
                    self.pending.discard(item_id)
                    if failed:
                        self.retry_at[item_id] = time.monotonic() + 60
