"""Replay intensity and delayed Twitch chat activity on the source timeline."""

from collections import Counter
import logging
import math

log = logging.getLogger(__name__)


def heatmap(values):
    points = []
    for row in values or ():
        try:
            start, end, value = (float(row[k]) for k in ("start_time", "end_time", "value"))
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start < end and all(map(math.isfinite, (start, end, value))):
            points.append((start, end, min(1.0, max(0.0, value))))
    return points


def mean(points, start, end):
    return sum(max(0.0, min(end, b) - max(start, a)) * value
               for a, b, value in points) / max(0.001, end - start)


def chat_heatmap(url, duration, cfg):
    """Replay only. Fail open if comments are unavailable."""
    try:
        from chat_downloader import ChatDownloader
        client = ChatDownloader()
        try:
            messages = client.get_chat(url, timeout=90, inactivity_timeout=15, max_attempts=2)
            bins = Counter(int(float(m["time_in_seconds"]) // 10) for m in messages
                           if m.get("time_in_seconds") is not None and 0 <= m["time_in_seconds"] < duration)
        finally:
            client.close()
        baseline = sum(bins.values()) / max(1, math.ceil(duration / 10))
        offset = float(cfg.get("cut", {}).get("chat_offset_seconds", 5))
        return [(max(0, i * 10 - offset), (i + 1) * 10 - offset, min(1, n / (6 * baseline)))
                for i, n in bins.items() if n >= max(3, 3 * baseline) and (i + 1) * 10 > offset]
    except Exception:
        log.warning("Twitch chat replay unavailable; using audio and scene scoring", exc_info=True)
        return []
