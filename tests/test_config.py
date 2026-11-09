"""Unit tests for config loader and formula evaluator."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import config


def test_config_loads_and_has_required_sections() -> None:
    cfg = config.get_config(reload=True)
    assert "sources" in cfg
    assert "youtube" in cfg["sources"]
    assert "reddit" in cfg["sources"]
    assert "discovery" in cfg
    assert "score_formula" in cfg["discovery"]
    assert "buffer" in cfg
    assert "dedupe" in cfg
    assert cfg["app"]["dry_run"] is True


def test_env_example_contains_all_keys() -> None:
    env_example = config.project_root() / ".env.example"
    assert env_example.exists()
    content = env_example.read_text(encoding="utf-8")
    expected_keys = [
        "GEMINI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "IG_USER_ID",
        "IG_ACCESS_TOKEN",
        "FB_PAGE_ID",
        "HEALTHCHECK_URL",
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "REDDIT_USER_AGENT",
    ]
    for k in expected_keys:
        assert f"{k}=" in content, f"Missing key in .env.example: {k}"


def test_evaluate_formula_valid_math() -> None:
    formula = "percentile_rank * (0.5 ** (age_hours / half_life_hours)) * source_weight"
    ctx = {
        "percentile_rank": 0.8,
        "age_hours": 36.0,
        "half_life_hours": 36.0,
        "source_weight": 1.25,
    }
    result = config.evaluate_formula(formula, ctx)
    # 0.8 * (0.5 ** 1.0) * 1.25 = 0.8 * 0.5 * 1.25 = 0.5
    assert pytest.approx(result, 0.001) == 0.5


def test_evaluate_formula_functions_and_division() -> None:
    formula = "max(views / max(age_hours, 1.0), 0.0)"
    ctx = {"views": 100, "age_hours": 10.0}
    assert pytest.approx(config.evaluate_formula(formula, ctx), 0.001) == 10.0


def test_evaluate_formula_blocks_unsafe_ast() -> None:
    with pytest.raises(ValueError):
        config.evaluate_formula("__import__('os').system('ls')", {})
