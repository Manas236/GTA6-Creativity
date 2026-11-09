"""Configuration loader, environment manager, and safe formula evaluation."""
from __future__ import annotations

import ast
import math
import os
from pathlib import Path
from typing import Any

import yaml

_CONFIG_CACHE: dict[str, Any] | None = None
_ENV_CACHE: dict[str, str] | None = None

ROOT_DIR = Path(__file__).resolve().parent.parent


def project_root() -> Path:
    return ROOT_DIR


def load_env(env_path: Path | None = None) -> dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is not None and env_path is None:
        return _ENV_CACHE

    path = env_path or (ROOT_DIR / ".env")
    env_vars: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                env_vars[key.strip()] = val.strip().strip("'\"")

    # Also overlay any existing OS environment variables
    for k, v in os.environ.items():
        if k not in env_vars:
            env_vars[k] = v

    if env_path is None:
        _ENV_CACHE = env_vars
    return env_vars


def secret(key: str, default: str = "") -> str:
    """Retrieve a secret from .env or os.environ."""
    env = load_env()
    return env.get(key, default)


def get_config(reload: bool = False) -> dict[str, Any]:
    """Retrieve parsed config.yaml."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not reload:
        return _CONFIG_CACHE

    config_path = ROOT_DIR / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    _CONFIG_CACHE = data
    return _CONFIG_CACHE


def media_dir(subdir: str = "") -> Path:
    """Get path to media directory, ensuring it exists."""
    p = ROOT_DIR / "media"
    if subdir:
        p = p / subdir
    p.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Safe Math AST Evaluator for Config-Driven Discovery Formula
# ---------------------------------------------------------------------------

_SAFE_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b if b != 0 else 0.0,
    ast.Pow: lambda a, b: a ** b,
    ast.USub: lambda a: -a,
    ast.UAdd: lambda a: a,
}

_SAFE_FUNCTIONS = {
    "max": max,
    "min": min,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
}


def _eval_node(node: ast.AST, context: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, context)
    elif isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        if node.id in _SAFE_FUNCTIONS:
            return _SAFE_FUNCTIONS[node.id]
        raise ValueError(f"Undefined variable in formula: {node.id}")
    elif isinstance(node, ast.BinOp):
        op = type(node.op)
        if op not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported operator in formula: {op.__name__}")
        left = _eval_node(node.left, context)
        right = _eval_node(node.right, context)
        return _SAFE_OPERATORS[op](left, right)
    elif isinstance(node, ast.UnaryOp):
        op = type(node.op)
        if op not in _SAFE_OPERATORS:
            raise ValueError(f"Unsupported unary operator: {op.__name__}")
        operand = _eval_node(node.operand, context)
        return _SAFE_OPERATORS[op](operand)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _SAFE_FUNCTIONS:
            raise ValueError(f"Unsupported function call in formula: {ast.dump(node.func)}")
        func = _SAFE_FUNCTIONS[node.func.id]
        args = [_eval_node(a, context) for a in node.args]
        return func(*args)
    else:
        raise ValueError(f"Unsupported AST node in formula: {type(node).__name__}")


def evaluate_formula(formula: str, context: dict[str, Any]) -> float:
    """Safely evaluate an arithmetic formula string with context parameters."""
    clean_formula = formula.strip().replace("\n", " ")
    parsed = ast.parse(clean_formula, mode="eval")
    result = _eval_node(parsed, context)
    return float(result)
