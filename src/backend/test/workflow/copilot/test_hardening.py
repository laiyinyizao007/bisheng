"""F007 G8 round 1: what a hostile canvas, history or answer must not get through.

- credentials on the canvas never reach the model and never show in the preview,
  yet stay on the canvas untouched (AC-25);
- the canvas, the history and the answer are bounded; YAML aliases and deep nesting
  are refused before they expand (10562 / issues);
- a model call that hangs ends as a failed turn (AC-27);
- what the declaration could not carry, and an existing tool node whose new tool
  does not resolve, stay on the canvas (AC-08, decision 4).
"""

from __future__ import annotations

import asyncio
import copy
import re
from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from bisheng.common.errcode.flow import WorkflowCopilotTooLargeError
from bisheng.workflow.domain.services.copilot import prompt, redact
from bisheng.workflow.domain.services.copilot import resources as resources_module
from bisheng.workflow.domain.services.copilot import service as service_module
from test.workflow.copilot.test_service import FULL_FLOW, START_ONLY, answer, canvas, of, prepare, run

SECRET = "Pa55-word-for-db"
_SHOWN = re.compile(r"```yaml\n(.*?)```", re.DOTALL)


def _param(node: dict[str, Any], key: str) -> Any:
    return next(p["value"] for g in node["data"]["group_params"] for p in g["params"] if p["key"] == key)


def _node(proposal_or_canvas: dict[str, Any], node_id: str) -> dict[str, Any]:
    nodes = proposal_or_canvas["canvas"]["nodes"] if "canvas" in proposal_or_canvas else proposal_or_canvas["nodes"]
    return next(n for n in nodes if n["id"] == node_id)


def _with_sql_agent() -> dict[str, Any]:
    spec = copy.deepcopy(START_ONLY)
    spec["nodes"] += [
        {
            "id": "agent_1",
            "type": "agent",
            "params": {
                "model_id": 1,
                "user_prompt": "{{#start_1.user_info#}}",
                "sql_agent": {
                    "open": True,
                    "db_address": "10.0.0.9:3306",
                    "db_name": "sales",
                    "db_username": "reader",
                    "db_password": SECRET,
                },
            },
        },
        {"id": "end_1", "type": "end"},
    ]
    spec["edges"] += [{"from": "start_1", "to": "agent_1"}, {"from": "agent_1", "to": "end_1"}]
    return canvas(spec)


def _shown_declaration(turn) -> str:
    """What the model was shown of the canvas: the yaml block in the last user message."""
    return _SHOWN.search(turn.messages[-1]["content"]).group(1)


# ---- credentials (AC-25) -----------------------------------------------------------------------


async def test_a_canvas_password_never_reaches_the_model_and_survives_the_round_trip(scripted_llm) -> None:
    turn = prepare(_with_sql_agent(), message="把数据库名改成 sales_v2")
    shown = _shown_declaration(turn)
    assert SECRET not in "\n".join(m["content"] for m in turn.messages)
    assert redact.REDACTED in shown
    assert redact.REDACTED in prompt.PREFACE  # the model is told what the marker means

    # The model keeps the marker where it was and edits a neighbouring field.
    state = scripted_llm(answer(shown.replace("db_name: sales", "db_name: sales_v2")))
    events = await run(turn)
    (proposal,) = of(events, "proposal")
    assert SECRET not in str(state["calls"])

    sql = _param(_node(proposal, "agent_1"), "sql_agent")
    assert (sql["db_name"], sql["db_password"]) == ("sales_v2", SECRET)  # the canvas keeps the real value
    (change,) = proposal["diff"]["nodes_changed"]
    (entry,) = change["params"]
    assert entry["local"]["db_name"] == "sales_v2" and entry["online"]["db_name"] == "sales"
    assert SECRET not in str(proposal["diff"])  # the preview never shows it
    assert entry["local"]["db_password"] == redact.REDACTED


async def test_the_marker_copied_to_a_new_place_is_refused_never_landed(scripted_llm) -> None:
    turn = prepare(_with_sql_agent())
    shown = _shown_declaration(turn)
    copied = shown.replace(
        "- id: end_1", f"- id: think\n  type: llm\n  params:\n    system_prompt: '{redact.REDACTED}'\n- id: end_1", 1
    )
    scripted_llm(answer(copied), answer(copied), answer(copied))
    events = await run(turn)
    assert not of(events, "proposal")
    (issues,) = of(events, "issues")
    assert any("think" in text and redact.REDACTED in text for text in issues["issues"])


def test_redact_covers_header_pairs_and_leaves_ordinary_keys() -> None:
    declaration = {
        "nodes": [
            {
                "id": "a",
                "params": {
                    "headers": [{"key": "Authorization", "value": "Bearer abc"}, {"key": "Accept", "value": "json"}],
                    "api_key": "sk-1",
                    "max_tokens": 512,
                    "nested": {"client_secret": "s", "empty_token": ""},
                },
            }
        ]
    }
    hidden, count = redact.redact(declaration)
    params = hidden["nodes"][0]["params"]
    assert count == 3
    assert params["headers"] == [{"key": "Authorization", "value": redact.REDACTED}, {"key": "Accept", "value": "json"}]
    assert (params["api_key"], params["max_tokens"], params["nested"]) == (
        redact.REDACTED,
        512,
        {"client_secret": redact.REDACTED, "empty_token": ""},
    )
    assert declaration["nodes"][0]["params"]["api_key"] == "sk-1"  # input untouched


def test_restore_does_not_follow_a_renamed_header() -> None:
    base = {"nodes": [{"id": "a", "params": {"headers": [{"key": "Authorization", "value": "Bearer abc"}]}}]}
    moved = {"nodes": [{"id": "a", "params": {"headers": [{"key": "X-Other", "value": redact.REDACTED}]}}]}
    assert redact.restore(moved, base)
    kept = {"nodes": [{"id": "a", "params": {"headers": [{"key": "Authorization", "value": redact.REDACTED}]}}]}
    assert redact.restore(kept, base) == []
    assert kept["nodes"][0]["params"]["headers"][0]["value"] == "Bearer abc"


# ---- bounds ------------------------------------------------------------------------------------


def test_a_canvas_with_too_many_nodes_or_edges_is_refused_before_decompiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "MAX_CANVAS_NODES", 1)
    called = []
    monkeypatch.setattr(service_module, "decompile", lambda *a, **k: called.append(1))
    with pytest.raises(WorkflowCopilotTooLargeError):
        prepare(canvas({**START_ONLY, "nodes": [*START_ONLY["nodes"], {"id": "end_1", "type": "end"}]}))
    assert not called


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "assistant", "content": "x"}] * (2 * prompt.MAX_HISTORY_TURNS + 1),  # no user turn at all
        [{"role": "user", "content": "x" * 15_000}] * 5,  # few turns, too much text
    ],
)
def test_history_is_bounded_in_turns_and_text_whatever_the_roles(history: list[dict[str, str]]) -> None:
    with pytest.raises(WorkflowCopilotTooLargeError):
        prepare(history=history)


async def test_yaml_aliases_are_refused_before_they_expand(scripted_llm) -> None:
    bomb = "a: &a [x, x, x, x, x, x, x, x, x]\nb: &b [*a, *a, *a, *a, *a, *a, *a, *a, *a]\nc: [*b, *b, *b]\nnodes: []\n"
    scripted_llm(answer(bomb), answer(bomb), answer(bomb))
    events = await run(prepare())
    (issues,) = of(events, "issues")
    assert all("别名" in text for text in issues["issues"])


async def test_deep_yaml_is_refused(scripted_llm) -> None:
    deep = "nodes: []\nx: " + "[" * 100 + "]" * 100 + "\n"
    scripted_llm(answer(deep), answer(deep), answer(deep))
    events = await run(prepare())
    (issues,) = of(events, "issues")
    assert any("嵌套" in text for text in issues["issues"])


async def test_an_overlong_answer_is_cut_off_without_a_retry(scripted_llm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "MAX_ANSWER_CHARS", 50)
    state = scripted_llm("很长" * 100)
    events = await run(prepare())
    assert len(state["calls"]) == 1
    (issues,) = of(events, "issues")
    assert "50" in issues["issues"][0]
    assert sum(len(e["text"]) for e in of(events, "delta")) <= 50


async def test_a_model_call_that_hangs_ends_as_a_timeout(scripted_llm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "MODEL_TIMEOUT_SECONDS", 0.05)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    state = scripted_llm(answer(FULL_FLOW))
    state["gate"] = asyncio.Event()  # never set: the call hangs
    try:
        events = await asyncio.wait_for(run(prepare()), timeout=5)
    finally:
        logger.remove(sink)
    (error,) = of(events, "error")
    assert error["code"] == 10563 and "TimeoutError" in error["message"]
    assert state["cancelled"]  # the hanging call itself was cancelled, not left running
    assert any("outcome=timeout" in line for line in lines)


# ---- what must stay on the canvas (AC-08, decision 4) ------------------------------------------


def _with_mystery() -> dict[str, Any]:
    sent = canvas(START_ONLY)
    edge = {
        "id": "xy-edge__start_1right_handle-mysteryleft_handle",
        "source": "start_1",
        "sourceHandle": "right_handle",
        "target": "mystery",
        "targetHandle": "left_handle",
        "type": "customEdge",
        "animated": True,
    }
    sent["nodes"].append(
        {
            "id": "mystery",
            "type": "flowNode",
            "position": {"x": 900, "y": 0},
            "data": {"id": "mystery", "type": "from_a_plugin", "group_params": []},
        }
    )
    sent["edges"].append(edge)
    return sent


async def test_nodes_the_declaration_cannot_carry_stay_with_their_edges(scripted_llm) -> None:
    sent = _with_mystery()
    turn = prepare(sent)
    assert "mystery" not in _shown_declaration(turn)
    scripted_llm(answer(FULL_FLOW))
    (proposal,) = of(await run(turn), "proposal")
    assert _node(proposal, "mystery") == sent["nodes"][-1]
    assert any(e["target"] == "mystery" for e in proposal["canvas"]["edges"])
    assert "mystery" not in {n["id"] for n in proposal["diff"]["nodes_only_online"]}
    assert not proposal["diff"]["edges_only_online"]


async def test_a_new_node_may_not_take_the_id_of_one_it_cannot_see(scripted_llm) -> None:
    clash = (
        FULL_FLOW.replace("id: reply", "id: mystery")
        .replace("to: reply", "to: mystery")
        .replace("from: reply", "from: mystery")
    )
    scripted_llm(answer(clash), answer(clash), answer(clash))
    (issues,) = of(await run(prepare(_with_mystery())), "issues")
    assert any("mystery" in text and "占用" in text for text in issues["issues"])


def _with_tool_node() -> dict[str, Any]:
    spec = copy.deepcopy(START_ONLY)
    spec["nodes"] += [
        {"id": "sub", "type": "tool", "tool": "submit_candidate", "params": {"name": "固定名"}},
        {"id": "end_1", "type": "end"},
    ]
    spec["edges"] += [{"from": "start_1", "to": "sub"}, {"from": "sub", "to": "end_1"}]
    return canvas(spec)


async def test_an_existing_tool_node_whose_new_tool_does_not_resolve_stays_as_it_was(scripted_llm) -> None:
    sent = _with_tool_node()
    turn = prepare(sent)
    shown = _shown_declaration(turn)
    assert "tool: submit_candidate" in shown
    scripted_llm(answer(shown.replace("tool: submit_candidate", "tool: send_mail")))
    (proposal,) = of(await run(turn), "proposal")
    assert _node(proposal, "sub") == next(n for n in sent["nodes"] if n["id"] == "sub")
    assert not proposal["diff"]["nodes_only_online"] and not proposal["diff"]["nodes_changed"]
    assert [(n["node_id"], n["kind"], n["wanted"]) for n in proposal["needs_selection"]] == [
        ("sub", "tool_unchanged", "send_mail")
    ]


# ---- the catalogue is listed once per short window ---------------------------------------------


async def test_the_usable_lists_are_reused_briefly_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def default_model():
        return 1, {1: resources_module.Resource(1, "通义千问")}

    async def assistant():
        return SimpleNamespace(llm_list=[])

    async def knowledge(request, login_user, knowledge_type):
        calls.append(("knowledge", login_user.user_id))
        return []

    class Tools:
        def __init__(self, login_user):
            calls.append(("tools", login_user.user_id))

        async def get_tool_list(self, *args):
            return []

    monkeypatch.setattr(resources_module, "check_default_model", default_model)
    monkeypatch.setattr(resources_module.LLMService, "get_assistant_llm", assistant)
    monkeypatch.setattr(resources_module, "_knowledge", knowledge)
    monkeypatch.setattr(resources_module, "ToolServices", Tools)
    clock = [1000.0]
    monkeypatch.setattr(resources_module.time, "monotonic", lambda: clock[0])

    alice, bob = SimpleNamespace(user_id=1), SimpleNamespace(user_id=2)
    await resources_module.load_catalogue(request=None, login_user=alice)
    await resources_module.load_catalogue(request=None, login_user=alice)
    assert calls.count(("tools", 1)) == 1  # the second turn reused the lists
    await resources_module.load_catalogue(request=None, login_user=bob)
    assert calls.count(("tools", 2)) == 1  # never shared across users
    clock[0] += resources_module.CATALOGUE_CACHE_SECONDS
    await resources_module.load_catalogue(request=None, login_user=alice)
    assert calls.count(("tools", 1)) == 2  # expired: listed again


# ---- G8 round 2 ----------------------------------------------------------------------------------


async def test_a_value_rewritten_around_the_marker_is_refused_not_landed(scripted_llm) -> None:
    turn = prepare(_with_sql_agent())
    shown = _shown_declaration(turn)
    rewritten = shown.replace(f"db_password: {redact.REDACTED}", f"db_password: Bearer-{redact.REDACTED}")
    assert rewritten != shown
    scripted_llm(answer(rewritten), answer(rewritten), answer(rewritten))
    events = await run(turn)
    assert not of(events, "proposal")
    (issues,) = of(events, "issues")
    assert any("db_password" in text and "改写" in text for text in issues["issues"])


@pytest.mark.parametrize("key", ["accessToken", "dbPassword", "x-api-key", "pwd123", "password_input", "authHeader"])
def test_credential_words_count_anywhere_in_the_key(key: str) -> None:
    assert redact.is_sensitive(key)


@pytest.mark.parametrize("key", ["max_tokens", "user_auth", "model_id", "system_prompt", "key"])
def test_ordinary_keys_are_not_credentials(key: str) -> None:
    assert not redact.is_sensitive(key)


def test_a_structure_under_a_credential_key_is_hidden_whole_and_restored_whole() -> None:
    base = {"nodes": [{"id": "a", "params": {"api_key": {"value": "sk-1", "env": "prod"}}}]}
    hidden, count = redact.redact(base)
    assert count == 1 and hidden["nodes"][0]["params"]["api_key"] == redact.REDACTED
    answer_spec = copy.deepcopy(hidden)
    assert redact.restore(answer_spec, base) == []
    assert answer_spec["nodes"][0]["params"]["api_key"] == {"value": "sk-1", "env": "prod"}


async def test_repeated_node_ids_are_fed_back_not_crashed(scripted_llm) -> None:
    twice = FULL_FLOW.replace(
        "  - id: end_1\n    type: end\n", "  - id: end_1\n    type: end\n  - id: end_1\n    type: end\n"
    )
    scripted_llm(answer(twice), answer(twice), answer(twice))
    events = await run(prepare())
    assert not of(events, "error")
    (issues,) = of(events, "issues")
    assert any("end_1" in text and "重复" in text for text in issues["issues"])


async def test_the_whole_turn_has_a_deadline_across_attempts(scripted_llm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "MODEL_TIMEOUT_SECONDS", 60.0)
    monkeypatch.setattr(service_module, "TURN_TIMEOUT_SECONDS", 0.05)
    state = scripted_llm(answer(FULL_FLOW))
    state["gate"] = asyncio.Event()
    events = await asyncio.wait_for(run(prepare()), timeout=5)
    (error,) = of(events, "error")
    assert "TimeoutError" in error["message"]


async def test_a_stream_that_will_not_close_does_not_hang_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "CLOSE_TIMEOUT_SECONDS", 0.05)
    closed: list[str] = []

    class Stuck:
        def __init__(self) -> None:
            self._chunks = [SimpleNamespace(content="只是文字。")]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop()

        async def aclose(self) -> None:
            closed.append("asked")
            await asyncio.Event().wait()  # never returns

    class LLM:
        def astream(self, messages):
            return Stuck()

    async def factory(**kwargs):
        return LLM()

    monkeypatch.setattr(service_module.LLMService, "get_bisheng_llm", factory)
    events = await asyncio.wait_for(run(prepare()), timeout=5)
    assert closed == ["asked"]
    assert of(events, "message")[0]["text"] == "只是文字。"


async def test_the_cache_separates_tenant_views(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def default_model():
        return 1, {1: resources_module.Resource(1, "通义千问")}

    async def assistant():
        return SimpleNamespace(llm_list=[])

    async def knowledge(request, login_user, knowledge_type):
        return []

    class Tools:
        def __init__(self, login_user):
            calls.append(login_user.user_id)

        async def get_tool_list(self, *args):
            return []

    monkeypatch.setattr(resources_module, "check_default_model", default_model)
    monkeypatch.setattr(resources_module.LLMService, "get_assistant_llm", assistant)
    monkeypatch.setattr(resources_module, "_knowledge", knowledge)
    monkeypatch.setattr(resources_module, "ToolServices", Tools)
    scope = [None]
    monkeypatch.setattr(resources_module, "get_admin_scope_tenant_id", lambda: scope[0])
    admin = SimpleNamespace(user_id=1, tenant_id=1)
    await resources_module.load_catalogue(request=None, login_user=admin)
    scope[0] = 7  # the same admin switched the management view
    await resources_module.load_catalogue(request=None, login_user=admin)
    assert calls == [1, 1]


async def test_a_node_id_that_is_not_a_string_is_fed_back(scripted_llm) -> None:
    odd = FULL_FLOW.replace("  - id: end_1\n", "  - id: [end, 1]\n").replace("to: end_1}", "to: end}")
    scripted_llm(answer(odd), answer(odd), answer(odd))
    events = await run(prepare())
    assert not of(events, "error")
    (issues,) = of(events, "issues")
    assert any("字符串 id" in text for text in issues["issues"])
