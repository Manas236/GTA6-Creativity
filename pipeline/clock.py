"""One replaceable source of "now" (UTC) for the whole pipeline.

Every timestamp the pipeline writes or compares goes through `utcnow()`, so the CP4 dry run and the
tests can install a fake clock and walk through days of scheduling, analytics and cleanup in
milliseconds. Production never calls `set_clock`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

ISO = "%Y-%m-%dT%H:%M:%SZ"

_clock: Callable[[], datetime] | None = None


def utcnow() -> datetime:
    """Timezone-aware current UTC time (the fake clock's time when one is installed)."""
    if _clock is not None:
        now = _clock()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Install a fake clock (None restores the real one)."""
    global _clock
    _clock = fn


def iso(dt: datetime) -> str:
    """A datetime as this database stores times: UTC, second precision, 'Z' suffix."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO)


def parse(ts: str) -> datetime:
    """The inverse of `iso` (also accepts +00:00 offsets)."""
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def now_iso() -> str:
    return iso(utcnow())


def epoch() -> float:
    return utcnow().timestamp()


class FakeClock:
    """A settable clock for tests and the dry run: `clock.set(...)`, `clock.advance(hours=1)`."""

    def __init__(self, start: datetime | str):
        self.now = parse(start) if isinstance(start, str) else start

    def __call__(self) -> datetime:
        return self.now

    def set(self, when: datetime | str) -> None:
        self.now = parse(when) if isinstance(when, str) else when

    def advance(self, **delta: float) -> datetime:
        from datetime import timedelta

        self.now = self.now + timedelta(**delta)
        return self.now

    # time.time()/time.sleep() stand-ins for the Meta client
    def time(self) -> float:
        return self.now.timestamp()

    def sleep(self, seconds: float) -> None:
        self.advance(seconds=max(float(seconds), 0.0))
