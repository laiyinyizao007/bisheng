"""F008 evaluation cases: one YAML file per case under ``cases/`` (design §6.1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
FIXTURES_DIR = HERE / "fixtures"
GROUPS = ("clear", "should_ask", "cannot_do", "modify")
_LISTS = ("must", "cannot_do", "assume", "walkthrough")
_MAPS = ("ask", "answers")


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    """All cases (or the ``only`` ids), validated; a malformed case fails loudly."""
    cases = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        case = yaml.safe_load(path.read_text(encoding="utf-8"))
        _validate(case, path)
        if not only or case["id"] in only:
            cases.append(case)
    if only and len(cases) != len(set(only)):
        missing = set(only) - {case["id"] for case in cases}
        raise ValueError(f"unknown case ids: {sorted(missing)}")
    return cases


def load_fixture(name: str) -> dict[str, Any]:
    """A fixture declaration (``__DEFAULT_MODEL__`` still in place)."""
    return yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def _validate(case: Any, path: Path) -> None:
    if not isinstance(case, dict):
        raise ValueError(f"{path.name}: not a mapping")
    for key in ("id", "group", "canvas", "request"):
        if not isinstance(case.get(key), str) or not case[key]:
            raise ValueError(f"{path.name}: missing {key}")
    if case["group"] not in GROUPS:
        raise ValueError(f"{path.name}: unknown group {case['group']}")
    if case["canvas"] != "empty" and not (FIXTURES_DIR / f"{case['canvas']}.yaml").is_file():
        raise ValueError(f"{path.name}: no fixture {case['canvas']}")
    for key in _LISTS:
        if not isinstance(case.setdefault(key, []), list):
            raise ValueError(f"{path.name}: {key} must be a list")
    for key in _MAPS:
        if not isinstance(case.setdefault(key, {}), dict):
            raise ValueError(f"{path.name}: {key} must be a mapping")
