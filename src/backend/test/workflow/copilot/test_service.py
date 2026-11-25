# ruff: noqa: RUF001 -- Chinese prompts, answers and copy with full-width punctuation
"""F007 T013 — one copilot turn, against a scripted model (design decisions 2, 6, 7).

The model's answer is only a suggestion: it must parse as plain YAML (no tags),
resolve its resources, compile and validate under the shared rules, respect the
code-node switch and the copilot-only reachability rule (AC-05), or it is fed back
and retried — three attempts in all. Only then is it merged into a proposal.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from loguru import logger

from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum
from bisheng.common.errcode.flow import WorkflowCopilotTooLargeError
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot import prompt
from bisheng.workflow.domain.services.copilot.service import WorkflowCopilotService
from bisheng_flow_dsl.compiler import compile_spec
from test.workflow.copilot.conftest import make_catalogue

START_ONLY = {"name": "wf", "nodes": [{"id": "start_1", "type": "start", "position": {"x": 0, "y": 0}}], "edges": []}

FULL_FLOW = """\
name: wf
nodes:
  - id: start_1
    type: start
  - id: ask
    type: input
  - id: think
    type: llm
    params:
      model_id: 通义千问
      user_prompt: "{{#ask.user_input#}}"
  - id: reply
    type: output
    params:
      message: {msg: "{{#think.output#}}", files: []}
  - id: end_1
    type: end
edges:
  - {from: start_1, to: ask}
  - {from: ask, to: think}
  - {from: think, to: reply}
  - {from: reply, to: end_1}
"""


def answer(yaml_text: str, before: str = "我加了一个对话助手。", after: str = "") -> str:
    return f"{before}\n```yaml\n{yaml_text}```\n{after}"


def canvas(spec: dict[str, Any]) -> dict[str, Any]:
    compiled = compile_spec(spec, NODE_TEMPLATES, make_catalogue().tools)
    compiled["viewport"] = {"x": 0, "y": 0, "zoom": 1}
    return compiled


def prepare(
    sent: dict[str, Any] | None = None, *, message: str = "做一个多轮对话助手", code_node_enabled: bool = False, **kw
):
    return WorkflowCopilotService.prepare(
        flow_id="f1",
        flow_name="wf",
        canvas=sent or canvas(START_ONLY),
        history=kw.pop("history", []),
        message=message,
        catalogue=kw.pop("catalogue", make_catalogue()),
        code_node_enabled=code_node_enabled,
        user_id=9,
    )


async def run(turn) -> list[dict[str, Any]]:
    return [event async for event in WorkflowCopilotService.stream(turn)]


def of(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == kind]


# ---- the happy path ---------------------------------------------------------------------------


async def test_empty_canvas_becomes_a_complete_proposal(scripted_llm) -> None:
    state = scripted_llm(answer(FULL_FLOW))
    events = await run(prepare())

    (proposal,) = of(events, "proposal")
    assert {n["id"] for n in proposal["diff"]["nodes_only_local"]} == {"ask", "think", "reply", "end_1"}
    think = next(n for n in proposal["canvas"]["nodes"] if n["id"] == "think")
    model_id = [p["value"] for g in think["data"]["group_params"] for p in g["params"] if p["key"] == "model_id"]
    assert model_id == [1]  # the name became the usable id
    assert proposal["needs_selection"] == []
    assert of(events, "message")[0]["text"].startswith("我加了一个对话助手")
    assert not of(events, "issues") and not of(events, "error")
    (kwargs,) = state["factory"]
    assert (kwargs["model_id"], kwargs["app_type"], kwargs["app_id"], kwargs["user_id"]) == (
        1,
        ApplicationTypeEnum.WORKFLOW,
        "f1",
        9,
    )


async def test_streamed_text_never_contains_the_yaml(scripted_llm) -> None:
    scripted_llm(answer(FULL_FLOW, before="先说明。", after="完成。"))
    events = await run(prepare())
    streamed = "".join(e["text"] for e in of(events, "delta"))
    assert "先说明。" in streamed and "完成。" in streamed
    assert "start_1" not in streamed and "```" not in streamed


async def test_the_yaml_streams_as_draft_without_its_fences(scripted_llm) -> None:
    scripted_llm(answer(FULL_FLOW, before="先说明。", after="完成。"))
    events = await run(prepare())
    draft = "".join(e["text"] for e in of(events, "draft"))
    assert draft == FULL_FLOW  # the block's body only: no fence, no ``yaml`` info line
    assert "先说明。" not in draft and "完成。" not in draft
    # Each attempt's draft starts after its own status event.
    kinds = [e["type"] for e in events]
    assert kinds.index("status") < kinds.index("draft")


async def test_each_retry_streams_its_own_draft(scripted_llm) -> None:
    broken = FULL_FLOW.replace("user_prompt:", "no_such_param: x\n      user_prompt:")
    scripted_llm(answer(broken), answer(FULL_FLOW))
    events = await run(prepare())
    retry = next(i for i, e in enumerate(events) if e["type"] == "status" and e["stage"] == "retrying")
    assert "".join(e["text"] for e in of(events[:retry], "draft")) == broken
    assert "".join(e["text"] for e in of(events[retry:], "draft")) == FULL_FLOW


async def test_a_plain_answer_is_text_only(scripted_llm) -> None:
    scripted_llm("正则是部分匹配（re.search）。")
    events = await run(prepare(message="条件节点的正则是全匹配吗"))
    assert of(events, "message")[0]["text"] == "正则是部分匹配（re.search）。"
    assert not of(events, "proposal") and not of(events, "issues")  # AC-13


# ---- validate, feed back, retry ---------------------------------------------------------------


async def test_invalid_answer_is_fed_back_and_the_retry_wins(scripted_llm) -> None:
    broken = FULL_FLOW.replace("user_prompt:", "no_such_param: x\n      user_prompt:")
    state = scripted_llm(answer(broken), answer(FULL_FLOW))
    events = await run(prepare())

    assert of(events, "proposal")
    assert [e["attempt"] for e in of(events, "status") if e["stage"] == "retrying"] == [2]
    feedback = state["calls"][1]["messages"][-1][1]
    assert "no_such_param" in feedback


async def test_three_failures_end_with_the_issues_and_no_proposal(scripted_llm) -> None:
    broken = answer(FULL_FLOW.replace("{{#ask.user_input#}}", "{{#nobody.user_input#}}"))
    state = scripted_llm(broken, broken, broken)
    events = await run(prepare())

    assert len(state["calls"]) == WorkflowCopilotService.MAX_ATTEMPTS
    assert not of(events, "proposal")
    (issues,) = of(events, "issues")
    assert any("nobody" in text for text in issues["issues"])  # names the node, not a stack trace (AC-10)


async def test_include_tags_are_refused_not_resolved(scripted_llm) -> None:
    tagged = answer(FULL_FLOW.replace('user_prompt: "{{#ask.user_input#}}"', "user_prompt: !include /etc/passwd"))
    scripted_llm(tagged, tagged, tagged)
    events = await run(prepare())
    assert not of(events, "proposal")
    assert any("!include" in text for text in of(events, "issues")[0]["issues"])


async def test_retry_answer_without_yaml_counts_as_a_failure(scripted_llm) -> None:
    broken = answer(FULL_FLOW.replace("user_prompt:", "no_such_param: x\n      user_prompt:"))
    scripted_llm(broken, "抱歉，我放弃了。", "还是没有。")
    events = await run(prepare())
    assert not of(events, "proposal") and of(events, "issues")


# ---- copilot-only gates -------------------------------------------------------------------


CODE_FLOW = FULL_FLOW.replace(
    "  - id: reply\n",
    "  - id: shout\n    type: code\n  - id: reply\n",
).replace("  - {from: think, to: reply}\n", "  - {from: think, to: shout}\n  - {from: shout, to: reply}\n")


async def test_code_nodes_are_refused_while_the_platform_switch_is_off(scripted_llm) -> None:
    scripted_llm(answer(CODE_FLOW), answer(CODE_FLOW), answer(CODE_FLOW))
    events = await run(prepare(code_node_enabled=False))
    assert not of(events, "proposal")
    assert any("代码节点" in text for text in of(events, "issues")[0]["issues"])  # AC-12


async def test_code_nodes_pass_when_the_switch_is_on(scripted_llm) -> None:
    scripted_llm(answer(CODE_FLOW))
    events = await run(prepare(code_node_enabled=True))
    assert of(events, "proposal")


async def test_new_node_that_cannot_be_reached_from_start_is_refused(scripted_llm) -> None:
    island = FULL_FLOW.replace(
        "  - id: end_1\n",
        "  - id: lonely\n    type: llm\n    params: {model_id: 通义千问, user_prompt: x}\n  - id: end_1\n",
    )
    island = island.replace(
        "  - {from: reply, to: end_1}\n", "  - {from: reply, to: end_1}\n  - {from: lonely, to: end_1}\n"
    )
    scripted_llm(answer(island), answer(island), answer(island))
    events = await run(prepare())
    assert any("lonely" in text and "开始节点" in text for text in of(events, "issues")[0]["issues"])  # AC-05


async def test_new_non_end_node_without_an_outgoing_edge_is_refused(scripted_llm) -> None:
    dead_end = FULL_FLOW.replace("  - {from: reply, to: end_1}\n", "  - {from: think, to: end_1}\n")
    scripted_llm(answer(dead_end), answer(dead_end), answer(dead_end))
    events = await run(prepare())
    assert any("reply" in text and "出边" in text for text in of(events, "issues")[0]["issues"])  # AC-05


async def test_the_users_own_dangling_node_does_not_block_a_proposal(scripted_llm) -> None:
    # The canvas already has a node with only an incoming edge; the request does not touch it.
    mine = copy.deepcopy(START_ONLY)
    mine["nodes"].append(
        {"id": "draft", "type": "llm", "params": {"model_id": 1, "user_prompt": "草稿"}, "position": {"x": 400, "y": 0}}
    )
    mine["edges"].append({"from": "start_1", "to": "draft"})
    with_draft = FULL_FLOW.replace(
        "  - id: end_1\n", "  - id: draft\n    type: llm\n    params: {model_id: 1, user_prompt: 草稿}\n  - id: end_1\n"
    ).replace("edges:\n", "edges:\n  - {from: start_1, to: draft}\n")
    scripted_llm(answer(with_draft))
    events = await run(prepare(canvas(mine)))
    assert of(events, "proposal"), of(events, "issues")


# ---- resources ----------------------------------------------------------------------------


async def test_a_tool_that_does_not_resolve_leaves_the_proposal_and_is_listed(scripted_llm) -> None:
    with_tool = FULL_FLOW.replace(
        "  - id: reply\n", "  - id: mail\n    type: tool\n    tool: send_mail\n    params: {}\n  - id: reply\n"
    ).replace("  - {from: think, to: reply}\n", "  - {from: think, to: mail}\n  - {from: mail, to: reply}\n")
    scripted_llm(answer(with_tool))
    events = await run(prepare())
    (proposal,) = of(events, "proposal")
    assert "mail" not in {n["id"] for n in proposal["canvas"]["nodes"]}
    assert not [e for e in proposal["canvas"]["edges"] if "mail" in (e["source"], e["target"])]
    assert [(n["node_id"], n["kind"], n["wanted"]) for n in proposal["needs_selection"]] == [
        ("mail", "tool", "send_mail")
    ]


# ---- failures, secrets, logs ---------------------------------------------------------------


async def test_model_failure_is_an_error_event_without_secrets(scripted_llm) -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        scripted_llm(ConnectionError("POST https://user:hunter2@llm.example/v1 failed, api_key=sk-SECRET123"))
        events = await run(prepare(message="请勿记录这句需求原文"))
    finally:
        logger.remove(sink)

    (error,) = of(events, "error")
    assert error["code"] == 10563
    assert "ConnectionError" in error["message"]
    log = "\n".join(lines)
    for secret in ("hunter2", "sk-SECRET123", "llm.example"):
        assert secret not in error["message"] and secret not in log  # AC-25
    assert "请勿记录这句需求原文" not in log and "start_1" not in log  # no canvas or dialog text in logs
    assert "outcome=model_error" in log


async def test_each_turn_logs_one_line_without_content(scripted_llm) -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        scripted_llm(answer(FULL_FLOW))
        await run(prepare(message="请勿记录这句需求原文"))
    finally:
        logger.remove(sink)
    turn_lines = [line for line in lines if "workflow copilot turn" in line]
    assert len(turn_lines) == 1
    assert "outcome=proposal" in turn_lines[0] and "attempts=1" in turn_lines[0]
    assert "请勿记录这句需求原文" not in "\n".join(lines)


def test_prepare_refuses_an_oversized_canvas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt, "MAX_DECLARATION_CHARS", 50)
    with pytest.raises(WorkflowCopilotTooLargeError):
        prepare()


async def test_history_text_reaches_the_model_and_the_sent_canvas_is_the_base(scripted_llm) -> None:
    state = scripted_llm("好的。")
    await run(prepare(history=[{"role": "user", "content": "上一轮"}, {"role": "assistant", "content": "收到"}]))
    sent = state["calls"][0]["messages"]
    assert [kind for kind, _ in sent] == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"]
    assert "start_1" in sent[-1][1]
