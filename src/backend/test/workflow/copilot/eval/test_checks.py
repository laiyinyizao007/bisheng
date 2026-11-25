# ruff: noqa: RUF001 -- Chinese prompts, questions and report copy use full-width punctuation
"""F008 T002: the evaluation's own logic, offline (no platform, no model)."""

from __future__ import annotations

import pytest

from test.workflow.copilot.eval import checks
from test.workflow.copilot.eval.cases import load_cases

# ---- the cases themselves -------------------------------------------------------------------


def test_cases_cover_the_four_groups_with_at_least_fifteen() -> None:
    cases = load_cases()
    groups = {case["group"] for case in cases}
    assert len(cases) >= 15 and groups == {"clear", "should_ask", "cannot_do", "modify"}
    for case in cases:
        if case["group"] == "should_ask":
            assert case["ask"] and case["answers"], case["id"]
        if case["group"] == "cannot_do":
            assert case["cannot_do"], case["id"]
        assert case["walkthrough"], case["id"]


# ---- "asked": no proposal in round one, and every expected topic was asked about -------------


TOPICS = {"currency": ["币种", "外币"], "approval": ["审批", "通知"]}


def test_asked_needs_every_topic_across_the_clarification_rounds() -> None:
    rounds = [
        checks.Round(questions=["金额会不会是外币？"], text="", proposal=False),
        checks.Round(questions=["审批怎么发起？"], text="", proposal=False),
    ]
    assert checks.asked(TOPICS, rounds) is True
    assert checks.asked(TOPICS, rounds[:1]) is False  # approval never came up


def test_asking_and_proposing_in_round_one_is_not_asking() -> None:
    rounds = [checks.Round(questions=["币种？审批？"], text="", proposal=True)]
    assert checks.asked(TOPICS, rounds) is False
    assert checks.asked_and_proposed(rounds) is True


def test_questions_in_prose_count_for_content_but_not_for_the_card() -> None:
    rounds = [checks.Round(questions=None, text="开始前想确认：金额会是外币吗？审批要不要通知主管？", proposal=False)]
    assert checks.asked(TOPICS, rounds) is True
    assert checks.asked_any(rounds[0]) is True
    assert checks.asked_by_card(rounds[0]) is False


def test_a_plain_answer_without_a_question_is_not_asking() -> None:
    assert checks.asked_any(checks.Round(questions=None, text="这个流程很简单。", proposal=False)) is False


# ---- mechanical checks on the final declaration ----------------------------------------------


DECLARATION = {
    "nodes": [
        {"id": "s", "type": "start"},
        {"id": "a", "type": "llm", "params": {"system_prompt": "把金额换算成人民币"}},
        {"id": "b", "type": "llm"},
        {"id": "mail", "type": "tool", "tool": "发送邮件"},
        {
            "id": "gate",
            "type": "condition",
            "cases": [
                {"id": "big", "conditions": [{"left": "a.output", "op": "greater_than_or_equal", "value": "1000"}]}
            ],
        },
    ],
    "edges": [],
}


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        ({"node_type": "llm"}, True),
        ({"node_type": "code"}, False),
        ({"no_node_type": "code"}, True),
        ({"no_node_type": "llm"}, False),
        ({"node_type": "llm", "min": 2}, True),
        ({"node_type": "llm", "min": 3}, False),
        ({"node_type": "llm", "max": 1}, False),
        ({"node_type": "tool", "tool": "发送邮件"}, True),
        ({"node_type": "tool", "tool": "计算器"}, False),
        ({"condition_op": ["greater_than_or_equal"]}, True),
        ({"condition_op": ["greater_than_or_equal"], "right_value": "1000"}, True),
        ({"condition_op": ["greater_than_or_equal"], "right_value": "500"}, False),
        ({"condition_op": ["greater_than"]}, False),
        ({"param_contains": {"node_type": "llm", "key": "system_prompt", "text": "人民币"}}, True),
        ({"param_contains": {"node_type": "llm", "key": "system_prompt", "text": "美元"}}, False),
    ],
)
def test_must_checks(check: dict, expected: bool) -> None:
    assert checks.must_holds(check, DECLARATION) is expected


def test_an_unknown_check_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError):
        checks.must_holds({"nonsense": 1}, DECLARATION)


# ---- aggregation ------------------------------------------------------------------------------


def test_wilson_interval_brackets_the_rate() -> None:
    low, high = checks.wilson(8, 10)
    assert 0.44 < low < 0.8 < high < 0.95
    assert checks.wilson(0, 0) == (0.0, 0.0)


def test_rates_use_runs_as_the_denominator() -> None:
    runs = [
        {
            "group": "should_ask",
            "asked": True,
            "asked_any": True,
            "card": True,
            "mixed": False,
            "structure_ok": True,
            "must_ok": True,
        },
        {
            "group": "should_ask",
            "asked": False,
            "asked_any": False,
            "card": False,
            "mixed": True,
            "structure_ok": True,
            "must_ok": False,
        },
        {
            "group": "clear",
            "asked": False,
            "asked_any": True,
            "card": True,
            "mixed": False,
            "structure_ok": True,
            "must_ok": True,
        },
        {
            "group": "clear",
            "asked": False,
            "asked_any": False,
            "card": False,
            "mixed": False,
            "structure_ok": False,
            "must_ok": False,
        },
    ]
    summary = checks.summarize(runs)
    assert summary["should_ask"]["hit_rate"]["rate"] == 0.5
    assert summary["should_ask"]["mixed"] == 1
    assert summary["clear"]["false_ask_rate"]["rate"] == 0.5
    assert summary["all"]["structure"]["rate"] == 0.75


# ---- secrets never reach the results ---------------------------------------------------------


def test_scrub_removes_tokens_and_keys() -> None:
    text = 'Authorization: Bearer abc.def.ghi api_key="sk-123456" cookie access_token_cookie=xyz; and SECRET-VALUE'
    cleaned = checks.scrub(text, secrets=["SECRET-VALUE"])
    for leaked in ("abc.def.ghi", "sk-123456", "xyz", "SECRET-VALUE"):
        assert leaked not in cleaned


# ---- the fixture canvases compile and validate under the platform's own rules ----------------


@pytest.mark.parametrize("name", sorted({case["canvas"] for case in load_cases()} - {"empty"}))
def test_fixtures_compile_and_validate(name: str) -> None:
    from bisheng.workflow.common.node_templates import NODE_TEMPLATES
    from bisheng_flow_dsl.compiler import compile_spec
    from bisheng_flow_dsl.validate import validate
    from test.workflow.copilot.eval.cases import load_fixture
    from test.workflow.copilot.eval.run_eval import fill_model

    compiled = compile_spec(fill_model(load_fixture(name), 1), NODE_TEMPLATES, [])
    assert validate(compiled, NODE_TEMPLATES, []) == []


def test_disabled_cases_are_not_run_and_say_why() -> None:
    ids = {case["id"] for case in load_cases()}
    assert "clear-calculator" not in ids  # the preset calculator tool has no parameters
    assert "clear-web-reader" in ids
    from test.workflow.copilot.eval.cases import disabled_cases

    assert "参数" in disabled_cases()["clear-calculator"]
