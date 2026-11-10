"""Healthchecks.io dead-man switch: ping after each successful cycle, never fail the cycle."""
from __future__ import annotations

import logging
from pathlib import Path

from pipeline import healthcheck, runner

URL = "https://hc-ping.com/00000000-0000-0000-0000-00000000c0de"
OK = {"status": "success", "errors": {}}


def recorder(code: int = 200, exc: Exception | None = None):
    calls: list[tuple[str, float]] = []

    def transport(url: str, timeout: float) -> int:
        calls.append((url, timeout))
        if exc:
            raise exc
        return code
    return transport, calls


def test_unset_url_is_skipped_without_any_request():
    transport, calls = recorder()
    assert healthcheck.ping(OK, transport=transport, url="").startswith("skipped")
    assert calls == []


def test_a_successful_cycle_pings_once_with_a_short_timeout():
    transport, calls = recorder()
    assert healthcheck.ping(OK, cfg={"healthcheck": {"timeout_seconds": 5}}, transport=transport, url=URL) == "sent"
    assert calls == [(URL, 5.0)]


def test_a_cycle_with_a_failed_stage_does_not_ping():
    transport, calls = recorder()
    out = healthcheck.ping({"status": "partial", "errors": {"discover": "boom"}}, transport=transport, url=URL)
    assert out.startswith("skipped") and "discover" in out and calls == []


def test_a_failed_ping_never_raises_and_never_logs_the_url(caplog):
    caplog.set_level(logging.INFO)
    transport, _ = recorder(exc=ConnectionError(f"cannot reach {URL}"))
    assert healthcheck.ping(OK, transport=transport, url=URL) == "failed: ConnectionError"
    transport, _ = recorder(code=503)
    assert healthcheck.ping(OK, transport=transport, url=URL) == "failed: HTTP 503"
    assert "c0de" not in caplog.text


def test_the_loop_pings_after_each_successful_cycle_only_and_survives_a_dead_ping(tmp_db: Path, monkeypatch):
    monkeypatch.setattr(runner.telegram_bot, "send_alert", lambda *a, **k: True)
    monkeypatch.setattr(healthcheck, "ping_url", lambda cfg=None: URL)
    outcomes = iter([None, RuntimeError("stage down"), None, None])

    def stage(cfg, db_path):
        err = next(outcomes)
        if err:
            raise err
        return "ok"

    pings: list[str] = []

    def transport(url: str, timeout: float) -> int:
        pings.append(url)
        if len(pings) == 2:
            raise TimeoutError("healthchecks.io is down")       # must not stop the loop
        return 200

    monkeypatch.setattr(runner, "STAGES", [("only", stage)])
    monkeypatch.setattr(runner, "healthcheck_transport", transport)
    assert runner.run_loop(cfg={}, db_path=tmp_db, interval_s=0, max_cycles=4, sleep=lambda s: None) == 4
    assert len(pings) == 3                                      # cycles 1, 3, 4; not the failed cycle 2


def test_the_cycle_command_pings_too(tmp_db: Path, monkeypatch):
    monkeypatch.setattr(healthcheck, "ping_url", lambda cfg=None: URL)
    transport, calls = recorder()
    monkeypatch.setattr(runner, "STAGES", [("only", lambda cfg, db_path: "ok")])
    monkeypatch.setattr(runner, "healthcheck_transport", transport)
    assert runner.run_single_cycle(cfg={}, db_path=tmp_db)["healthcheck"] == "sent"
    assert len(calls) == 1
