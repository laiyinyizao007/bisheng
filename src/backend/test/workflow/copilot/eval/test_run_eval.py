# ruff: noqa: RUF001 -- Chinese prompts, questions and report copy use full-width punctuation
"""F008 T002: the evaluation runner against a fake platform and a fake judge (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from test.workflow.copilot.eval import run_eval
from test.workflow.copilot.eval.bisheng_api import BishengApi
from test.workflow.copilot.eval.cases import load_cases

CASES = {case["id"]: case for case in load_cases()}

PROPOSAL_CANVAS = {
    "nodes": [
        {
            "id": "start_1",
            "type": "flowNode",
            "position": {"x": 0, "y": 0},
            "data": {"id": "start_1", "type": "start", "name": "开始", "group_params": []},
        },
    ],
    "edges": [],
}


class FakePlatform:
    """Answers the platform routes the runner uses; scripts the copilot replies in order."""

    def __init__(self, replies: list[list[dict[str, Any]]], token: str = "TOKEN-123") -> None:
        self.replies = replies
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.token = token

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie", "").endswith(self.token)
        path = request.url.path
        if path == "/api/v1/workflow/node_templates":
            return httpx.Response(200, json={"status_code": 200, "data": []})
        if path == "/api/v1/tool":
            return httpx.Response(200, json={"status_code": 200, "data": []})
        if path == "/api/v1/llm/workflow":
            return httpx.Response(200, json={"status_code": 200, "data": {"data": {"model_id": 7}}})
        if path == "/api/v1/workflow/create":
            flow_id = f"flow{len(self.created)}"
            self.created.append(flow_id)
            return httpx.Response(201, json={"status_code": 200, "data": {"id": flow_id}})
        if path.startswith("/api/v1/flows/") and request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"status_code": 200, "data": None})
        if path == "/api/v1/workflow/copilot":
            self.messages.append(json.loads(request.content))
            events = self.replies.pop(0)
            body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            return httpx.Response(200, text=body)
        raise AssertionError(f"unexpected {request.method} {path}")


class FakeJudge:
    def __init__(self, verdict: str = "PASS") -> None:
        self.verdict = verdict
        self.calls = 0

    def walkthrough(self, reply: str, declaration: str, sample: str, expect: str) -> tuple[str, str]:
        self.calls += 1
        return self.verdict, "traced"

    def cannot_do(self, reply: str, capability: str, hints: list[str]) -> tuple[str, str]:
        self.calls += 1
        return self.verdict, "stated"

    def assume(self, reply: str, point: str) -> tuple[str, str]:
        self.calls += 1
        return self.verdict, "listed"


def api(platform: FakePlatform) -> BishengApi:
    client = httpx.Client(base_url="http://bisheng.test", transport=httpx.MockTransport(platform.handler))
    return BishengApi(client, token=platform.token)


def ask(*questions: str) -> list[dict[str, Any]]:
    return [
        {"type": "status", "stage": "generating", "attempt": 1},
        {"type": "message", "text": "开始前想确认几点。"},
        {
            "type": "ask",
            "questions": [{"id": f"q{i}", "question": q, "options": ["是", "否"]} for i, q in enumerate(questions)],
        },
        {"type": "done"},
    ]


def propose(text: str = "我做了一个方案。") -> list[dict[str, Any]]:
    return [
        {"type": "status", "stage": "generating", "attempt": 1},
        {"type": "message", "text": text},
        {"type": "proposal", "canvas": PROPOSAL_CANVAS, "diff": {}, "needs_selection": [], "replaced": []},
        {"type": "done"},
    ]


def test_a_should_ask_case_asks_is_answered_and_then_proposes() -> None:
    platform = FakePlatform([ask("金额会不会是外币？", "审批怎么发起？"), propose()])
    record = run_eval.run_case(
        CASES["ask-reimburse"], api(platform), FakeJudge(), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert record["asked"] is True and record["card"] is True and record["mixed"] is False
    assert record["structure_ok"] is True
    # The second turn carries the question in the assistant's history and answers it.
    second = platform.messages[1]
    assert "外币" in second["history"][1]["content"]
    assert "7.2" in second["message"]
    assert platform.deleted == platform.created == ["flow0"]  # only its own flow, cleaned up


def test_a_clear_case_that_gets_asked_is_told_to_assume_and_counted_as_a_false_ask() -> None:
    platform = FakePlatform([ask("要几轮对话？"), propose()])
    record = run_eval.run_case(
        CASES["clear-qa-assistant"], api(platform), FakeJudge(), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert record["asked_any"] is True
    assert "合理假设" in platform.messages[1]["message"]


def test_asking_and_proposing_together_is_mixed_not_a_hit() -> None:
    platform = FakePlatform([propose("金额会不会是外币？审批怎么发起？下面先给一个方案。")])
    record = run_eval.run_case(
        CASES["ask-reimburse"], api(platform), FakeJudge(), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert record["asked"] is False and record["mixed"] is True


def test_at_most_two_clarification_rounds() -> None:
    platform = FakePlatform([ask("币种？"), ask("审批？"), ask("还有？")])
    record = run_eval.run_case(
        CASES["ask-reimburse"], api(platform), FakeJudge(), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert len(platform.messages) == 3 and record["structure_ok"] is False


def test_the_flow_is_deleted_even_when_the_run_fails_and_errors_are_scrubbed() -> None:
    platform = FakePlatform([])  # popping from an empty script raises inside the copilot call
    record = run_eval.run_case(
        CASES["ask-reimburse"], api(platform), FakeJudge(), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert record["error"] and platform.deleted == platform.created
    assert platform.token not in json.dumps(record, ensure_ascii=False)


def test_the_call_budget_stops_the_run() -> None:
    budget = run_eval.Budget(1)
    platform = FakePlatform([ask("币种？"), propose()])
    record = run_eval.run_case(CASES["ask-reimburse"], api(platform), FakeJudge(), run_id="r1", index=0, budget=budget)
    assert "budget" in record["error"] and len(platform.messages) == 1


def test_judge_verdicts_are_recorded_with_reasons() -> None:
    platform = FakePlatform([ask("币种？", "审批？"), propose()])
    record = run_eval.run_case(
        CASES["ask-reimburse"], api(platform), FakeJudge("FAIL"), run_id="r1", index=0, budget=run_eval.Budget(100)
    )
    assert record["walkthrough"] == ["FAIL", "FAIL"]
    assert record["cannot_do"] == ["FAIL"]
    assert all(item["reason"] for item in record["judgements"])


def test_flow_names_carry_the_run_id() -> None:
    platform = FakePlatform([propose()])
    names: list[str] = []
    original = BishengApi.create_flow

    def spy(self: BishengApi, name: str) -> str:
        names.append(name)
        return original(self, name)

    BishengApi.create_flow = spy  # type: ignore[method-assign]
    try:
        run_eval.run_case(
            CASES["clear-qa-assistant"],
            api(platform),
            FakeJudge(),
            run_id="abc123",
            index=2,
            budget=run_eval.Budget(100),
        )
    finally:
        BishengApi.create_flow = original  # type: ignore[method-assign]
    assert names == ["eval-abc123-clear-qa-assistant-2"]


def test_report_renders_rates_with_intervals(tmp_path) -> None:
    runs = [
        {
            "id": "x",
            "group": "should_ask",
            "asked": True,
            "asked_any": True,
            "card": True,
            "mixed": False,
            "structure_ok": True,
            "must_ok": True,
            "walkthrough": ["PASS"],
            "cannot_do": [],
            "assume": [],
            "error": None,
        }
    ]
    paths = run_eval.write_results(runs, tmp_path, label="unit", meta={"model": "m", "judge_calibrated": True})
    text = paths["md"].read_text(encoding="utf-8")
    assert "should_ask" in text and "ci95" not in text and "%" in text


@pytest.mark.parametrize("value", ["__DEFAULT_MODEL__", {"a": ["__DEFAULT_MODEL__"]}])
def test_fixture_model_placeholder_is_replaced(value) -> None:
    assert "__DEFAULT_MODEL__" not in json.dumps(run_eval.fill_model(value, 7))


def test_cases_whose_platform_preconditions_do_not_hold_are_skipped() -> None:
    cases = [CASES["cannot-code-node"], CASES["clear-web-reader"]]
    assert CASES["cannot-code-node"]["requires"] == {"code_node_enabled": False}
    kept, skipped = run_eval.applicable(cases, {"code_node_enabled": True})
    assert [c["id"] for c in kept] == ["clear-web-reader"]
    assert skipped == [{"id": "cannot-code-node", "requires": {"code_node_enabled": False}}]
    kept, skipped = run_eval.applicable(cases, {"code_node_enabled": False})
    assert len(kept) == 2 and skipped == []


def test_platform_switches_are_read_from_env() -> None:
    class EnvPlatform(FakePlatform):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/env":
                return httpx.Response(200, json={"status_code": 200, "data": {"workflow": {"code_node_enabled": True}}})
            return super().handler(request)

    assert api(EnvPlatform([])).platform_switches() == {"code_node_enabled": True}
