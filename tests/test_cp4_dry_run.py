"""Smoke test: scripts/cp4_dry_run.py over a short window passes every invariant, zero network,
and leaves no global patched behind."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from pipeline import clock, config, db, runner, telegram_bot

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("cp4_dry_run_script", ROOT / "scripts" / "cp4_dry_run.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cp4_dry_run_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_a_short_dry_run_passes_every_invariant_and_restores_the_globals(capsys):
    before = (requests.Session.request, config.secret, db.DEFAULT_DB_PATH, telegram_bot.send_alert,
              runner.healthcheck_transport, config.media_root())
    mod = _load()
    assert mod.main(["--days", "3.5", "--seed", "11", "--skip-real-check"]) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "[FAIL]" not in out
    assert "sim-02" in out and "sim-04" in out                  # the crash and the lost reply both happened
    after = (requests.Session.request, config.secret, db.DEFAULT_DB_PATH, telegram_bot.send_alert,
             runner.healthcheck_transport, config.media_root())
    assert after == before
    real_now = datetime.now(timezone.utc)
    assert abs((clock.utcnow() - real_now).total_seconds()) < 60     # the fake clock is gone
