"""Dead-man switch: ping Healthchecks.io after each successful runner cycle (PLAN.md section 1.8).

* Skipped entirely when HEALTHCHECK_URL is unset (no request, no warning spam).
* Only a cycle whose every stage succeeded pings. A cycle with a failed stage sends nothing,
  so Healthchecks.io raises its own alarm if failures persist past the check's grace time;
  the failing stage itself is already alerted on Telegram by the runner.
* A failed ping is logged and swallowed: it can never fail, slow or stop the cycle (one short
  timeout, no retries).
* The URL embeds the check's secret UUID, so it is never logged.

The HTTP call goes through an injectable transport, like posting/telegram/twitch, so the test
suite stays zero-network.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import config

log = logging.getLogger(__name__)

# (url, timeout_s) -> HTTP status code. The default uses requests.
Transport = Callable[[str, float], int]


def _requests_transport(url: str, timeout_s: float) -> int:
    import requests

    return int(requests.get(url, timeout=timeout_s).status_code)


def ping_url(cfg: dict[str, Any] | None = None) -> str:
    return config.secret("HEALTHCHECK_URL").strip()


def ping(
    result: dict[str, Any] | None = None,
    cfg: dict[str, Any] | None = None,
    transport: Transport | None = None,
    url: str | None = None,
) -> str:
    """Ping after a cycle. Returns what happened: "skipped: ...", "sent", or "failed: ...".
    Never raises."""
    try:
        url = (url if url is not None else ping_url(cfg)).strip()
        if not url:
            return "skipped: HEALTHCHECK_URL is not set"
        if result is not None and result.get("status") != "success":
            failed = sorted((result.get("errors") or {}).keys())
            return f"skipped: the cycle was not successful (failed stages: {failed})"
        timeout = float((((cfg or {}).get("healthcheck") or {}).get("timeout_seconds", 10.0)))
        code = (transport or _requests_transport)(url, timeout)
        if 200 <= int(code) < 300:
            log.info("healthcheck ping sent")
            return "sent"
        log.warning("healthcheck ping answered HTTP %s (the cycle is unaffected)", code)
        return f"failed: HTTP {code}"
    except Exception as e:  # noqa: BLE001 - a ping must never fail the cycle
        log.warning("healthcheck ping failed: %s (the cycle is unaffected)", type(e).__name__)
        return f"failed: {type(e).__name__}"
