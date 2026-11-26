# ruff: noqa: RUF001 -- Chinese prompts, questions and report copy use full-width punctuation
"""F008 evaluation scoring (design §6.2, §6.3, §6.5): pure functions, no I/O."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

_QUESTION_MARK = re.compile(r"[?？]")


@dataclass
class Round:
    """One copilot turn as the evaluation sees it."""

    questions: list[str] | None  # card questions (with their options) when an ``ask`` event came
    text: str  # the reply text
    proposal: bool


def _asked_text(one: Round) -> str:
    return "\n".join(one.questions) if one.questions else one.text


def asked_any(one: Round) -> bool:
    """A question was put to the user and nothing was proposed."""
    return not one.proposal and bool(one.questions or _QUESTION_MARK.search(one.text))


def asked_by_card(one: Round) -> bool:
    return not one.proposal and bool(one.questions)


def asked_and_proposed(rounds: list[Round]) -> bool:
    """Some round asked and proposed at once — reported apart, never a hit."""
    return any(one.proposal and bool(one.questions or _QUESTION_MARK.search(one.text)) for one in rounds)


def _asking_text(rounds: list[Round]) -> str:
    """What was asked across the rounds, including a round that also proposed."""
    return "\n".join(_asked_text(one) for one in rounds if one.questions or asked_any(one))


def asked(topics: dict[str, list[str]], rounds: list[Round]) -> bool:
    """Design §6.3: round one proposed nothing, and the clarification rounds together hit every topic."""
    if not rounds or not asked_any(rounds[0]) or asked_and_proposed(rounds):
        return False
    text = _asking_text(rounds)
    return all(any(word in text for word in words) for words in topics.values())


def topics_hit(topics: dict[str, list[str]], rounds: list[Round]) -> set[str]:
    text = _asking_text(rounds)
    return {topic for topic, words in topics.items() if any(word in text for word in words)}


# ---- mechanical checks (design §6.2) ----------------------------------------------------------


def _nodes(declaration: dict[str, Any], node_type: str | None = None) -> list[dict[str, Any]]:
    nodes = [n for n in declaration.get("nodes") or [] if isinstance(n, dict)]
    return [n for n in nodes if node_type is None or n.get("type") == node_type]


def _conditions(declaration: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        condition
        for node in _nodes(declaration, "condition")
        for case in node.get("cases") or []
        for condition in (case.get("conditions") or [])
        if isinstance(condition, dict)
    ]


def must_holds(check: dict[str, Any], declaration: dict[str, Any]) -> bool:
    """One ``must`` entry against the final declaration; an unknown check raises."""
    if "no_node_type" in check:
        return not _nodes(declaration, check["no_node_type"])
    if "condition_op" in check:
        ops = set(check["condition_op"])
        wanted = check.get("right_value")
        return any(
            c.get("op") in ops and (wanted is None or str(c.get("value")).strip() == str(wanted))
            for c in _conditions(declaration)
        )
    if "param_contains" in check:
        spec = check["param_contains"]
        return any(
            spec["text"] in str((node.get("params") or {}).get(spec["key"], ""))
            for node in _nodes(declaration, spec["node_type"])
        )
    if "node_type" in check:
        nodes = _nodes(declaration, check["node_type"])
        if "tool" in check:
            nodes = [n for n in nodes if n.get("tool") == check["tool"]]
        count = len(nodes)
        if "min" in check or "max" in check:
            return check.get("min", 0) <= count <= check.get("max", math.inf)
        return count > 0
    raise ValueError(f"unknown must check: {check}")


# ---- aggregation (design §6.3) -----------------------------------------------------------------


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _rate(successes: int, total: int) -> dict[str, Any]:
    low, high = wilson(successes, total)
    return {
        "n": total,
        "k": successes,
        "rate": (successes / total) if total else 0.0,
        "ci95": [round(low, 3), round(high, 3)],
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-group and overall rates; every denominator is the number of runs."""
    not_run = sum(bool(r.get("not_run")) for r in runs)
    runs = [r for r in runs if not r.get("not_run")]  # a spent budget is not a quality failure
    by_group: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_group.setdefault(run["group"], []).append(run)
    summary: dict[str, Any] = {}
    for group, items in [*by_group.items(), ("all", runs)]:
        n = len(items)
        if not n:
            continue
        entry: dict[str, Any] = {
            "runs": n,
            "structure": _rate(sum(bool(r.get("structure_ok")) for r in items), n),
            "must": _rate(sum(bool(r.get("must_ok")) for r in items), n),
        }
        if group == "should_ask":
            entry["hit_rate"] = _rate(sum(bool(r.get("asked")) for r in items), n)
            entry["mixed"] = sum(bool(r.get("mixed")) for r in items)
        if group == "clear":
            entry["false_ask_rate"] = _rate(sum(bool(r.get("asked_any")) for r in items), n)
        asked_runs = [r for r in items if r.get("asked_any")]
        if asked_runs:
            entry["card_rate"] = _rate(sum(bool(r.get("card")) for r in asked_runs), len(asked_runs))
        judge_errors = 0
        for key in ("walkthrough", "cannot_do", "assume"):
            given = [v for r in items for v in (r.get(key) or [])]
            judge_errors += sum(v == "ERROR" for v in given)
            verdicts = [v for v in given if v != "ERROR"]  # a judge outage is not a model failure
            if verdicts:
                entry[key] = _rate(sum(v == "PASS" for v in verdicts), len(verdicts))
        if judge_errors:
            entry["judge_errors"] = judge_errors
        if group == "cannot_do":
            judged = [r for r in items if "ERROR" not in (r.get("cannot_do") or [])]
            entry["stated_rate"] = _rate(sum(_stated(r) for r in judged), len(judged))
        summary[group] = entry
    if not_run:
        summary["not_run"] = not_run
    return summary


def _stated(run: dict[str, Any]) -> bool:
    """Every listed capability judged PASS, in a run that finished."""
    verdicts = run.get("cannot_do") or []
    expected = run.get("cannot_do_expected", len(verdicts))
    return not run.get("error") and bool(verdicts) and len(verdicts) == expected and all(v == "PASS" for v in verdicts)


# ---- secrets never reach the results (design §6.5) ---------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[^\s,;\"']+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|secret|password|access_token_cookie)\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"),
]


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, "<redacted>")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + "<redacted>", text)
    return text
