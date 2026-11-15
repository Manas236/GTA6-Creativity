"""Optional engagement data. Bad or unavailable replay metadata is ignored."""
import json
import logging
import math
from collections import Counter
from contextlib import closing

log = logging.getLogger(__name__)


def spans(raw):
    out = []
    for part in raw if isinstance(raw, (list, tuple)) else ():
        try:
            start, end, value = (float(part[key]) for key in ("start_time", "end_time", "value"))
            if all(map(math.isfinite, (start, end, value))) and 0 <= start < end and 0 <= value <= 1:
                out.append((start, end, value))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def heatmap(raw):
    try:
        return spans(json.loads(raw or "null"))
    except (TypeError, ValueError):
        return []


def chat_spikes(url, duration, offset=6, multiplier=3):
    """Ten-second message counts against the VOD's mean rate, shifted to action time."""
    try:
        from chat_downloader import ChatDownloader

        counts = Counter()
        with closing(ChatDownloader()) as downloader:
            for message in downloader.get_chat(url, timeout=45, inactivity_timeout=15,
                                               message_groups=["messages"], max_attempts=1):
                seconds = float(message.get("time_in_seconds", -1))
                if 0 <= seconds < duration:
                    counts[int(seconds // 10)] += 1
        baseline = sum(counts.values()) / max(1, math.ceil(duration / 10))
        if not baseline:
            return []
        return [(max(0, bucket * 10 - offset), max(0, bucket * 10 + 10 - offset),
                 min(1, count / (baseline * multiplier * 2)))
                for bucket, count in sorted(counts.items())
                if count >= max(3, baseline * multiplier)]
    except Exception:
        log.warning("Twitch chat replay unavailable; using audio and scene scoring", exc_info=True)
        return []
