"""F007 T008 — resources the model writes by name become ids the user can actually use.

Design decision 4. ``resolve`` is pure (declaration in, declaration out) and only
touches values the model wrote or changed: a value equal to the one in the canvas
the user sent is left alone, even if it points at something no longer usable
(AC-08). ``load_catalogue`` gathers the usable set from the same services the
canvas dropdowns use, with no tenant id of its own (AC-26, constitution C3).
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from bisheng.common.errcode.flow import WorkflowCopilotModelNotConfiguredError, WorkflowCopilotModelUnavailableError
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot import resources
from bisheng.workflow.domain.services.copilot.resources import Catalogue, Resource, resolve

TOOLS = [
    {
        "id": 3,
        "name": "invest",
        "children": [
            {
                "id": 7,
                "name": "submit_candidate",
                "node_template": {"tool_key": "k7", "type": "tool", "group_params": []},
            },
            {"id": 8, "name": "get_feedback", "node_template": {"tool_key": "k8", "type": "tool", "group_params": []}},
        ],
    }
]


def catalogue(**overrides: Any) -> Catalogue:
    base = {
        "models": [
            Resource(1, "通义千问", ("qwen-max",)),
            Resource(2, "深度求索", ("deepseek-chat",)),
            Resource(3, "深度求索", ("ds-v3",)),
        ],
        "agent_models": [Resource(1, "通义千问", ("qwen-max",))],
        "knowledge": [Resource(11, "研报库"), Resource(12, "制度库"), Resource(13, "制度库")],
        "qa_knowledge": [Resource(21, "客服问答")],
        "tools": copy.deepcopy(TOOLS),
        "default_model_id": 1,
        "default_agent_model_id": 1,
    }
    base.update(overrides)
    return Catalogue(**base)


def spec(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"name": "wf", "nodes": list(nodes), "edges": []}


def node(result: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in result["nodes"] if n["id"] == node_id)


def run(declaration: dict[str, Any], base: dict[str, Any] | None = None, cat: Catalogue | None = None):
    return resolve(declaration, base or spec(), cat or catalogue(), NODE_TEMPLATES)


# ---- models ---------------------------------------------------------------------------


def test_unique_model_name_or_alias_becomes_its_numeric_id() -> None:
    out = run(
        spec(
            {"id": "a", "type": "llm", "params": {"model_id": " 通义千问 "}},
            {"id": "b", "type": "llm", "params": {"model_id": "DeepSeek-Chat"}},
        )
    )
    assert node(out.declaration, "a")["params"]["model_id"] == 1
    assert node(out.declaration, "b")["params"]["model_id"] == 2
    assert out.needs_selection == []


def test_numeric_string_id_is_written_back_as_a_number() -> None:
    # design pitfall 7: "2" and 2 differ in the diff; the web stores numbers.
    out = run(spec({"id": "a", "type": "llm", "params": {"model_id": "2"}}))
    assert node(out.declaration, "a")["params"]["model_id"] == 2


def test_ambiguous_model_name_is_left_for_the_user_with_its_candidates() -> None:
    out = run(spec({"id": "a", "type": "llm", "params": {"model_id": "深度求索"}}))
    assert node(out.declaration, "a")["params"]["model_id"] == 0
    assert out.needs_selection == [
        {
            "node_id": "a",
            "param_key": "model_id",
            "kind": "model",
            "wanted": "深度求索",
            "candidates": ["深度求索", "深度求索"],
        }
    ]


def test_unknown_model_name_lists_at_most_ten_candidates() -> None:
    many = [Resource(i, f"m{i}") for i in range(1, 15)]
    out = run(spec({"id": "a", "type": "llm", "params": {"model_id": "gpt-9"}}), cat=catalogue(models=many))
    (item,) = out.needs_selection
    assert (item["wanted"], len(item["candidates"])) == ("gpt-9", 10)


def test_an_id_the_user_cannot_use_is_cleared_even_if_typed_in_the_request() -> None:
    out = run(spec({"id": "a", "type": "llm", "params": {"model_id": 999}}))  # AC-16
    assert node(out.declaration, "a")["params"]["model_id"] == 0
    assert out.needs_selection[0]["wanted"] == "999"


def test_new_nodes_get_the_default_models_and_the_optional_recommendation_does_not() -> None:
    out = run(
        spec(
            {"id": "l", "type": "llm"},
            {"id": "r", "type": "rag", "params": {"model_id": ""}},
            {"id": "g", "type": "agent"},
            {"id": "i", "type": "input"},
        ),
        cat=catalogue(default_model_id=2, default_agent_model_id=1),
    )
    assert node(out.declaration, "l")["params"]["model_id"] == 2
    assert node(out.declaration, "r")["params"]["model_id"] == 2
    assert node(out.declaration, "g")["params"]["model_id"] == 1  # assistant default, as the platform does
    assert "recommended_llm" not in node(out.declaration, "i").get("params", {})
    assert out.needs_selection == []


def test_agent_model_is_matched_only_among_assistant_models() -> None:
    out = run(spec({"id": "g", "type": "agent", "params": {"model_id": "deepseek-chat"}}))
    assert node(out.declaration, "g")["params"]["model_id"] == 0
    assert out.needs_selection[0]["candidates"] == ["通义千问"]


def test_agent_without_an_assistant_default_is_left_for_the_user() -> None:
    out = run(spec({"id": "g", "type": "agent"}), cat=catalogue(default_agent_model_id=None))
    assert node(out.declaration, "g")["params"]["model_id"] == 0
    assert out.needs_selection[0]["kind"] == "model"


# ---- knowledge and tool lists -------------------------------------------------------------


def test_knowledge_by_label_gets_its_key_and_shape_is_kept() -> None:
    out = run(
        spec(
            {
                "id": "r",
                "type": "rag",
                "params": {"model_id": 1, "knowledge": {"type": "knowledge", "value": [{"label": "研报库"}]}},
            }
        )
    )
    assert node(out.declaration, "r")["params"]["knowledge"] == {
        "type": "knowledge",
        "value": [{"key": 11, "label": "研报库"}],
    }


def test_ambiguous_or_unusable_knowledge_items_are_dropped_and_listed() -> None:
    value = {
        "type": "knowledge",
        "value": [{"label": "制度库"}, {"key": 404, "label": "别人的库"}, {"label": "研报库"}],
    }
    out = run(spec({"id": "r", "type": "rag", "params": {"model_id": 1, "knowledge": value}}))
    assert node(out.declaration, "r")["params"]["knowledge"]["value"] == [{"key": 11, "label": "研报库"}]
    assert [(i["kind"], i["wanted"], i["candidates"]) for i in out.needs_selection] == [
        ("knowledge", "制度库", ["制度库", "制度库"]),
        ("knowledge", "别人的库", ["研报库", "制度库", "制度库"]),
    ]


def test_qa_knowledge_and_agent_tools_use_their_own_catalogues() -> None:
    out = run(
        spec(
            {
                "id": "q",
                "type": "qa_retriever",
                "params": {"qa_knowledge_id": [{"label": "客服问答"}, {"label": "研报库"}]},
            },
            {"id": "g", "type": "agent", "params": {"model_id": 1, "tool_list": [{"label": "get_feedback"}]}},
        )
    )
    assert node(out.declaration, "q")["params"]["qa_knowledge_id"] == [{"key": 21, "label": "客服问答"}]
    assert node(out.declaration, "g")["params"]["tool_list"] == [{"key": 8, "label": "get_feedback"}]
    assert [(i["kind"], i["wanted"]) for i in out.needs_selection] == [("knowledge", "研报库")]


# ---- what the user already had -------------------------------------------------------------


def test_values_unchanged_from_the_sent_canvas_are_left_alone() -> None:
    # The user's own node points at a model that is now offline: the request did not
    # touch it, so neither does the copilot (AC-08). A changed node is still checked.
    mine = {"id": "a", "type": "llm", "params": {"model_id": 77, "knowledge": None}}
    mine["params"].pop("knowledge")
    base = spec(copy.deepcopy(mine), {"id": "b", "type": "llm", "params": {"model_id": 77}})
    out = run(spec(mine, {"id": "b", "type": "llm", "params": {"model_id": 78}}), base=base)
    assert node(out.declaration, "a")["params"]["model_id"] == 77
    assert node(out.declaration, "b")["params"]["model_id"] == 0
    assert [i["node_id"] for i in out.needs_selection] == ["b"]


def test_knowledge_items_already_on_the_canvas_are_kept_when_the_list_changes() -> None:
    old = {"type": "knowledge", "value": [{"key": 404, "label": "旧库"}]}
    base = spec({"id": "r", "type": "rag", "params": {"model_id": 1, "knowledge": old}})
    new = {"type": "knowledge", "value": [{"key": 404, "label": "旧库"}, {"label": "研报库"}]}
    out = run(spec({"id": "r", "type": "rag", "params": {"model_id": 1, "knowledge": new}}), base=base)
    assert node(out.declaration, "r")["params"]["knowledge"]["value"] == [
        {"key": 404, "label": "旧库"},
        {"key": 11, "label": "研报库"},
    ]
    assert out.needs_selection == []


# ---- tool nodes ----------------------------------------------------------------------------


def test_tool_node_that_does_not_resolve_is_reported_not_compiled() -> None:
    out = run(
        spec({"id": "t", "type": "tool", "tool": "send_mail"}, {"id": "u", "type": "tool", "tool": "submit_candidate"})
    )
    assert set(out.unresolved_tools) == {"t"}
    assert out.needs_selection == [
        {
            "node_id": "t",
            "param_key": "tool",
            "kind": "tool",
            "wanted": "send_mail",
            "candidates": ["submit_candidate", "get_feedback"],
        }
    ]


def test_unchanged_tool_node_is_not_rechecked() -> None:
    base = spec({"id": "t", "type": "tool", "tool": "retired_tool"})
    out = run(spec({"id": "t", "type": "tool", "tool": "retired_tool"}), base=base)
    assert out.unresolved_tools == {} and out.needs_selection == []


# ---- driven by the templates, not by key names ----------------------------------------------


def test_a_new_model_parameter_in_a_template_is_resolved_without_code_changes() -> None:
    templates = copy.deepcopy(NODE_TEMPLATES)
    llm = next(t for t in templates if t["type"] == "llm")
    llm["group_params"][0]["params"].append({"key": "judge_model", "type": "bisheng_model", "value": ""})
    out = resolve(
        spec({"id": "a", "type": "llm", "params": {"model_id": 1, "judge_model": "深度求索 "}}),
        spec(),
        catalogue(),
        templates,
    )
    assert [(i["param_key"], i["wanted"]) for i in out.needs_selection] == [("judge_model", "深度求索")]


# ---- load_catalogue --------------------------------------------------------------------------


def _model(mid: int, name: str, *, model_type: str = "llm", online: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id=mid, name=name, model_name=f"m-{mid}", model_type=model_type, online=online)


@pytest.fixture
def services(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"knowledge": []}
    state = {"workflow_default": 1, "assistant": [(1, True), (2, False)]}

    async def workflow_llm(*args, **kwargs):
        assert not args and "tenant_id" not in kwargs  # C3: the request context carries the tenant
        return SimpleNamespace(model_id=state["workflow_default"]), False, False

    async def all_llm(*args, **kwargs):
        return [
            SimpleNamespace(
                models=[
                    _model(1, "通义千问"),
                    _model(2, "深度求索"),
                    _model(5, "向量", model_type="embedding"),
                    _model(6, "下线的", online=False),
                ]
            )
        ]

    async def assistant_llm(*args, **kwargs):
        assert "tenant_id" not in kwargs
        return SimpleNamespace(llm_list=[SimpleNamespace(model_id=m, default=d) for m, d in state["assistant"]])

    async def get_knowledge(request, login_user, knowledge_type, *, cursor=None, page_size=10, action="use", **kwargs):
        calls["knowledge"].append((knowledge_type.value, cursor, action))
        pages = {
            (0, None): ([SimpleNamespace(id=11, name="研报库")], True, "c2"),
            (0, "c2"): ([SimpleNamespace(id=12, name="制度库")], False, None),
            (1, None): ([SimpleNamespace(id=21, name="客服问答")], False, None),
        }
        rows, more, nxt = pages[(knowledge_type.value, cursor)]
        return SimpleNamespace(data=rows, has_more=more, next_cursor=nxt)

    class Tools:
        def __init__(self, login_user):
            calls["tools_user"] = login_user

        async def get_tool_list(self, is_preset=None, action="use"):
            calls["tools_action"] = action
            return [SimpleNamespace(model_dump=lambda: copy.deepcopy(TOOLS[0]))]

    monkeypatch.setattr(resources.LLMService, "aget_workflow_llm_with_meta", workflow_llm)
    monkeypatch.setattr(resources.LLMService, "get_all_llm", all_llm)
    monkeypatch.setattr(resources.LLMService, "get_assistant_llm", assistant_llm)
    monkeypatch.setattr(resources.KnowledgeService, "get_knowledge", get_knowledge)
    monkeypatch.setattr(resources, "ToolServices", Tools)
    calls["state"] = state
    return calls


async def test_catalogue_is_the_usable_set(services: dict[str, Any]) -> None:
    user = object()
    cat = await resources.load_catalogue(request=object(), login_user=user)
    assert [(m.id, m.name) for m in cat.models] == [(1, "通义千问"), (2, "深度求索")]  # LLM type, online only
    assert [m.id for m in cat.agent_models] == [1, 2]
    assert (cat.default_model_id, cat.default_agent_model_id) == (1, 1)  # the assistant item marked default
    assert [(k.id, k.name) for k in cat.knowledge] == [(11, "研报库"), (12, "制度库")]  # every page
    assert [k.id for k in cat.qa_knowledge] == [21]
    assert services["knowledge"] == [(0, None, "use"), (0, "c2", "use"), (1, None, "use")]
    assert (services["tools_user"], services["tools_action"]) == (user, "use")
    assert cat.tools[0]["children"][0]["node_template"]["tool_key"] == "k7"


async def test_catalogue_refuses_without_a_workflow_default_model(services: dict[str, Any]) -> None:
    services["state"]["workflow_default"] = None
    with pytest.raises(WorkflowCopilotModelNotConfiguredError):
        await resources.load_catalogue(request=object(), login_user=object())


async def test_catalogue_refuses_when_the_default_model_is_offline_or_gone(services: dict[str, Any]) -> None:
    services["state"]["workflow_default"] = 6
    with pytest.raises(WorkflowCopilotModelUnavailableError):
        await resources.load_catalogue(request=object(), login_user=object())
    services["state"]["workflow_default"] = 404
    with pytest.raises(WorkflowCopilotModelUnavailableError):
        await resources.load_catalogue(request=object(), login_user=object())


async def test_assistant_models_that_are_offline_do_not_count(services: dict[str, Any]) -> None:
    services["state"]["assistant"] = [(6, True), (1, False)]
    cat = await resources.load_catalogue(request=object(), login_user=object())
    assert [m.id for m in cat.agent_models] == [1]
    assert cat.default_agent_model_id is None
