"""Canvas → declaration (F007 AC-24, design decisions 2 and 12).

The contract is a round trip: ``compile(decompile(canvas))`` has no ``diff`` against
``canvas`` — with everything the declaration language cannot carry listed in
``omitted`` instead of silently dropped. Node ids and positions survive, so the
copilot's diff lines up unchanged nodes (design decision 2).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from bisheng_cli.flow.spec import load_spec
from bisheng_flow_dsl.compiler import compile_spec
from bisheng_flow_dsl.decompile import decompile
from bisheng_flow_dsl.diff import count, diff
from bisheng_flow_dsl.dump import dump
from tests.helpers.flow_fixtures import find_param, templates, tools

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "src/backend/bisheng/dev_toolkit/skills/workflow-as-code/example"

RICH_SPEC: dict[str, Any] = {
    "name": "富样例",
    "description": "覆盖条件、工具、tab、位置",
    "nodes": [
        {"id": "start_1", "type": "start", "position": {"x": 10, "y": 20}},
        {"id": "ask", "type": "input", "name": "提问", "tab": "form_input", "position": {"x": 300, "y": 20}},
        {
            "id": "gate",
            "type": "condition",
            "name": "判断",
            "cases": [
                {
                    "id": "stopcase",
                    "logic": "or",
                    "conditions": [
                        {"left": "ask.user_input", "op": "regex", "value": r"^\s*结束\s*$"},
                        {"left": "ask.user_input", "op": "is_empty"},
                    ],
                },
                {
                    "id": "same",
                    "conditions": [{"left": "ask.user_input", "op": "equals", "ref": "start_1.chat_history"}],
                },
            ],
        },
        {
            "id": "llm_a",
            "type": "llm",
            "name": "分析",
            "params": {
                "model_id": 7,
                "temperature": 0.3,
                "system_prompt": "你是分析师。\n逐条回答。\n",
                "user_prompt": "{{#ask.user_input#}}",
            },
        },
        {"id": "submit", "type": "tool", "tool": "submit_candidate", "params": {"name": "{{#llm_a.output#}}"}},
        {"id": "end_1", "type": "end"},
    ],
    "edges": [
        {"from": "start_1", "to": "ask"},
        {"from": "ask", "to": "gate"},
        {"from": "gate", "to": "end_1", "handle": "stopcase"},
        {"from": "gate", "to": "llm_a"},
        {"from": "llm_a", "to": "submit"},
        {"from": "submit", "to": "ask"},
    ],
}


def _compile(spec: dict[str, Any]) -> dict[str, Any]:
    return compile_spec(spec, templates(), tools())


def _round_trip(canvas: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    declaration, omitted = decompile(canvas, templates(), tools(), name="wf")
    return declaration, omitted, diff(_compile(declaration), canvas)


# ---- the round trip ----------------------------------------------------------------


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_f006_examples_round_trip_without_difference(example: Path) -> None:
    canvas = _compile(load_spec(example))
    _, omitted, result = _round_trip(canvas)
    assert count(result) == 0, result
    assert omitted == []


def test_rich_canvas_round_trips_without_difference() -> None:
    canvas = _compile(RICH_SPEC)
    _, omitted, result = _round_trip(canvas)
    assert count(result) == 0, result
    assert omitted == []


def test_node_ids_positions_names_and_top_level_survive() -> None:
    canvas = _compile(RICH_SPEC)
    declaration, _ = decompile(canvas, templates(), tools(), name="富样例", description="覆盖条件")
    assert [n["id"] for n in declaration["nodes"]] == [n["id"] for n in canvas["nodes"]]
    by_id = {n["id"]: n for n in declaration["nodes"]}
    assert by_id["start_1"]["position"] == {"x": 10, "y": 20}
    assert by_id["ask"]["name"] == "提问"
    assert (declaration["name"], declaration["description"]) == ("富样例", "覆盖条件")
    recompiled = _compile(declaration)
    assert [n["position"] for n in recompiled["nodes"]] == [n["position"] for n in canvas["nodes"]]


def test_only_non_default_params_are_written() -> None:
    declaration, _ = decompile(_compile(RICH_SPEC), templates(), tools(), name="wf")
    llm = next(n for n in declaration["nodes"] if n["id"] == "llm_a")
    assert set(llm["params"]) == {"model_id", "temperature", "system_prompt", "user_prompt"}
    end = next(n for n in declaration["nodes"] if n["id"] == "end_1")
    assert "params" not in end


def test_condition_becomes_cases_and_tool_becomes_its_name() -> None:
    declaration, _ = decompile(_compile(RICH_SPEC), templates(), tools(), name="wf")
    by_id = {n["id"]: n for n in declaration["nodes"]}
    assert [case["id"] for case in by_id["gate"]["cases"]] == ["stopcase", "same"]
    assert "condition" not in by_id["gate"].get("params", {})
    assert by_id["submit"]["tool"] == "submit_candidate"
    assert by_id["ask"]["tab"] == "form_input"


def test_condition_with_a_stale_label_falls_back_to_the_raw_parameter() -> None:
    # The web keeps labels computed when the condition was set; renaming a node later
    # leaves them stale. A stale LEFT label has a spelling (``label``); a stale RIGHT
    # label has none, so recomputing it would be a diff -- the raw value is kept instead.
    canvas = _compile(RICH_SPEC)
    gate = next(n for n in canvas["nodes"] if n["id"] == "gate")
    value = find_param(gate, "condition")["value"]
    value[0]["conditions"][0]["left_label"] = "旧名字/user_input"
    declaration, omitted, result = _round_trip(canvas)
    assert count(result) == 0, result
    gate_decl = next(n for n in declaration["nodes"] if n["id"] == "gate")
    assert gate_decl["cases"][0]["conditions"][0]["label"] == "旧名字/user_input"

    value[1]["conditions"][0]["right_label"] = "旧名字/chat_history"
    declaration, omitted, result = _round_trip(canvas)
    assert count(result) == 0, result
    assert omitted == []
    gate_decl = next(n for n in declaration["nodes"] if n["id"] == "gate")
    assert "cases" not in gate_decl
    assert gate_decl["params"]["condition"][1]["conditions"][0]["right_label"] == "旧名字/chat_history"


def test_ambiguous_tool_name_is_written_qualified() -> None:
    catalogue = tools()
    catalogue.append(copy.deepcopy(catalogue[0]))
    catalogue[1]["name"] = "other"
    for child in catalogue[1]["children"]:
        child["tool_key"] = child["node_template"]["tool_key"] = "other_" + child["tool_key"]
    spec = copy.deepcopy(RICH_SPEC)
    next(n for n in spec["nodes"] if n["id"] == "submit")["tool"] = "invest/submit_candidate"
    canvas = compile_spec(spec, templates(), catalogue)
    declaration, omitted = decompile(canvas, templates(), catalogue, name="wf")
    assert next(n for n in declaration["nodes"] if n["id"] == "submit")["tool"] == "invest/submit_candidate"
    assert omitted == []
    assert count(diff(compile_spec(declaration, templates(), catalogue), canvas)) == 0


# ---- what the declaration cannot carry --------------------------------------------


def _with_extras(canvas: dict[str, Any]) -> dict[str, Any]:
    canvas = copy.deepcopy(canvas)
    canvas["viewport"] = {"x": 5, "y": 6, "zoom": 0.7}
    canvas["nodes"].append(
        {
            "id": "note_ab12c",
            "type": "noteNode",
            "position": {"x": 0, "y": 400},
            "data": {"id": "note_ab12c", "type": "note", "group_params": [], "value": "勿删"},
        }
    )
    llm = next(n for n in canvas["nodes"] if n["id"] == "llm_a")
    llm["data"]["legacy_flag"] = True
    llm["data"]["group_params"][0]["params"].append({"key": "no_such_param", "value": 1})
    # React Flow runtime state is canvas noise, not content: never reported.
    llm.update({"selected": True, "dragging": False, "measured": {"width": 1, "height": 2}})
    return canvas


def test_everything_the_declaration_cannot_carry_is_listed() -> None:
    declaration, omitted = decompile(_with_extras(_compile(RICH_SPEC)), templates(), tools(), name="wf")
    assert {(o["kind"], o.get("node"), o.get("key")) for o in omitted} == {
        ("viewport", None, None),
        ("note", "note_ab12c", None),
        ("unknown_field", "llm_a", "legacy_flag"),
        ("unknown_param", "llm_a", "no_such_param"),
    }
    assert "note_ab12c" not in {n["id"] for n in declaration["nodes"]}


def test_unknown_node_type_and_its_edges_are_omitted_not_broken() -> None:
    canvas = _compile(RICH_SPEC)
    canvas["nodes"].append(
        {
            "id": "x_1",
            "type": "flowNode",
            "position": {"x": 0, "y": 0},
            "data": {"id": "x_1", "type": "from_the_future", "group_params": []},
        }
    )
    canvas["edges"].append(
        {"id": "e9", "source": "llm_a", "sourceHandle": "right_handle", "target": "x_1", "targetHandle": "left_handle"}
    )
    declaration, omitted = decompile(canvas, templates(), tools(), name="wf")
    kinds = {(o["kind"], o.get("node")) for o in omitted}
    assert ("unknown_node_type", "x_1") in kinds
    assert ("edge_to_omitted_node", "x_1") in kinds
    _compile(declaration)  # still compiles: no dangling reference left behind


def test_tool_node_whose_tool_is_not_in_the_catalogue_is_omitted() -> None:
    canvas = _compile(RICH_SPEC)
    declaration, omitted = decompile(canvas, templates(), [], name="wf")
    assert ("tool_unresolved", "submit") in {(o["kind"], o.get("node")) for o in omitted}
    assert "submit" not in {n["id"] for n in declaration["nodes"]}
    _compile_without_tools = compile_spec(declaration, templates(), [])
    assert "submit" not in {n["id"] for n in _compile_without_tools["nodes"]}


def test_old_canvas_missing_a_parameter_differs_only_by_that_parameter() -> None:
    # design pitfall 8: an older canvas lacks a parameter the current template has;
    # the recompiled node gets the template default, and that is the only difference.
    canvas = _compile(RICH_SPEC)
    llm = next(n for n in canvas["nodes"] if n["id"] == "llm_a")
    for group in llm["data"]["group_params"]:
        group["params"] = [p for p in group["params"] if p["key"] != "temperature"]
    _, omitted, result = _round_trip(canvas)
    assert omitted == []
    assert result["nodes_only_local"] == [] and result["nodes_only_online"] == []
    assert [(c["id"], [p["key"] for p in c["params"]]) for c in result["nodes_changed"]] == [("llm_a", ["temperature"])]


# ---- YAML text -----------------------------------------------------------------------


def test_dump_reads_back_to_the_same_declaration_and_stays_readable() -> None:
    declaration, _ = decompile(_compile(RICH_SPEC), templates(), tools(), name="富样例")
    text = dump(declaration)
    assert yaml.safe_load(text) == declaration
    assert "富样例" in text  # Chinese is not escaped
    assert "system_prompt: |" in text  # multi-line prompts are block literals
    assert text.index("name:") < text.index("nodes:") < text.index("edges:")
