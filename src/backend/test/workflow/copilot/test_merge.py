"""F007 T010 — "apply" = the sent canvas plus the diff, never a wholesale replacement.

Design decision 3 (merge) and 8 (layout). What is pinned:

- untouched nodes keep their object as sent: position, notes and fields the
  declaration cannot carry survive (AC-08);
- the preview diff is recomputed from the merged canvas, so what the preview lists
  is exactly what "apply" lands (AC-17 / AC-18);
- new nodes go to the right of the existing canvas without overlapping (AC-09).
"""

from __future__ import annotations

import copy
from typing import Any

from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot.merge import DEFAULT_NODE_SIZE, merge
from bisheng_flow_dsl.compiler import compile_spec
from bisheng_flow_dsl.decompile import decompile
from bisheng_flow_dsl.diff import count, diff

BASE_SPEC: dict[str, Any] = {
    "name": "wf",
    "nodes": [
        {"id": "start_1", "type": "start", "position": {"x": 0, "y": 0}},
        {"id": "ask", "type": "input", "name": "提问", "position": {"x": 400, "y": 0}},
        {
            "id": "llm_a",
            "type": "llm",
            "name": "分析",
            "position": {"x": 800, "y": 0},
            "params": {"model_id": 1, "system_prompt": "你是分析师", "user_prompt": "{{#ask.user_input#}}"},
        },
        {
            "id": "out_1",
            "type": "output",
            "position": {"x": 1200, "y": 0},
            "params": {"message": {"msg": "{{#llm_a.output#}}", "files": []}},
        },
        {"id": "end_1", "type": "end", "position": {"x": 1600, "y": 0}},
    ],
    "edges": [
        {"from": "start_1", "to": "ask"},
        {"from": "ask", "to": "llm_a"},
        {"from": "llm_a", "to": "out_1"},
        {"from": "out_1", "to": "end_1"},
    ],
}


def compile_(spec: dict[str, Any]) -> dict[str, Any]:
    return compile_spec(spec, NODE_TEMPLATES, [])


def sent_canvas() -> dict[str, Any]:
    canvas = compile_(BASE_SPEC)
    canvas["viewport"] = {"x": 12, "y": 34, "zoom": 0.8}
    canvas["nodes"].append(
        {
            "id": "note_x1",
            "type": "noteNode",
            "position": {"x": 0, "y": 600},
            "data": {"id": "note_x1", "type": "note", "group_params": [], "value": "勿删"},
        }
    )
    llm = node(canvas, "llm_a")
    llm["data"]["legacy_flag"] = "keep me"
    llm["data"]["group_params"][0]["params"].append({"key": "not_in_template", "value": 42})
    llm["measured"] = {"width": 300, "height": 180}
    return canvas


def node(canvas: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in canvas["nodes"] if n["id"] == node_id)


def param(n: dict[str, Any], key: str) -> Any:
    return [p["value"] for g in n["data"]["group_params"] for p in g["params"] if p["key"] == key]


def proposal_from(edit) -> dict[str, Any]:
    """The declaration the copilot sees (decompiled sent canvas), edited, compiled."""
    declaration, _ = decompile(sent_canvas(), NODE_TEMPLATES, [], name="wf")
    edit(declaration)
    return compile_(declaration)


def decl_node(declaration: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in declaration["nodes"] if n["id"] == node_id)


# ---- untouched content -----------------------------------------------------------------------


def test_identical_proposal_changes_nothing() -> None:
    sent = sent_canvas()
    result = merge(sent, proposal_from(lambda d: None))
    assert result.canvas["nodes"] == sent["nodes"]
    assert result.canvas["edges"] == sent["edges"]
    assert result.canvas["viewport"] == sent["viewport"]
    assert count(result.diff) == 0


def test_changed_parameter_lands_and_everything_else_on_the_node_survives() -> None:
    sent = sent_canvas()
    result = merge(
        sent, proposal_from(lambda d: decl_node(d, "llm_a")["params"].update(system_prompt="你是审慎的分析师"))
    )
    llm = node(result.canvas, "llm_a")
    assert param(llm, "system_prompt") == ["你是审慎的分析师"]
    assert llm["position"] == node(sent, "llm_a")["position"]
    assert llm["measured"] == {"width": 300, "height": 180}
    assert llm["data"]["legacy_flag"] == "keep me"
    assert param(llm, "not_in_template") == [42]
    assert node(result.canvas, "note_x1") == node(sent, "note_x1")
    assert [(c["id"], [p["key"] for p in c["params"]]) for c in result.diff["nodes_changed"]] == [
        ("llm_a", ["system_prompt"])
    ]


def test_name_and_condition_cases_are_fields_and_params_like_any_other() -> None:
    def edit(d: dict[str, Any]) -> None:
        decl_node(d, "ask")["name"] = "用户提问"
        d["nodes"].append(
            {
                "id": "gate",
                "type": "condition",
                "cases": [{"id": "stop", "conditions": [{"left": "ask.user_input", "op": "equals", "value": "结束"}]}],
            }
        )
        d["edges"] += [
            {"from": "ask", "to": "gate"},
            {"from": "gate", "to": "end_1", "handle": "stop"},
            {"from": "gate", "to": "llm_a"},
        ]
        d["edges"] = [e for e in d["edges"] if (e["from"], e["to"]) != ("ask", "llm_a")]

    result = merge(sent_canvas(), proposal_from(edit))
    assert node(result.canvas, "ask")["data"]["name"] == "用户提问"
    assert param(node(result.canvas, "gate"), "condition")[0][0]["id"] == "stop"
    assert {(e["source"], e.get("sourceHandle"), e["target"]) for e in result.diff["edges_only_local"]} == {
        ("ask", "right_handle", "gate"),
        ("gate", "stop", "end_1"),
        ("gate", "right_handle", "llm_a"),
    }
    assert [(e["source"], e["target"]) for e in result.diff["edges_only_online"]] == [("ask", "llm_a")]


# ---- deletions -----------------------------------------------------------------------------


def test_deleted_node_and_its_edges_go_but_notes_never_do() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["nodes"] = [n for n in d["nodes"] if n["id"] != "out_1"]
        d["edges"] = [e for e in d["edges"] if "out_1" not in (e["from"], e["to"])] + [{"from": "llm_a", "to": "end_1"}]

    result = merge(sent_canvas(), proposal_from(edit))
    ids = {n["id"] for n in result.canvas["nodes"]}
    assert "out_1" not in ids and "note_x1" in ids
    assert not [e for e in result.canvas["edges"] if "out_1" in (e["source"], e["target"])]
    assert [n["id"] for n in result.diff["nodes_only_online"]] == ["out_1"]


def test_a_note_known_only_by_its_canvas_shape_is_still_never_deleted() -> None:
    # decompile treats node.type == "noteNode" as a note even without data.type, so the
    # proposal never has it and the diff reports it as removed: merge must keep it.
    sent = sent_canvas()
    del node(sent, "note_x1")["data"]["type"]
    result = merge(sent, proposal_from(lambda d: None))
    assert node(result.canvas, "note_x1") == node(sent, "note_x1")


# ---- whole-node replacement ----------------------------------------------------------------


def test_parameter_the_sent_node_lacks_replaces_the_node_but_keeps_position_and_extras() -> None:
    sent = sent_canvas()
    llm = node(sent, "llm_a")
    for group in llm["data"]["group_params"]:
        group["params"] = [p for p in group["params"] if p["key"] != "temperature"]  # an older canvas
    declaration, _ = decompile(sent, NODE_TEMPLATES, [], name="wf")
    decl_node(declaration, "llm_a")["params"]["temperature"] = 0.2
    result = merge(sent, compile_(declaration))
    merged = node(result.canvas, "llm_a")
    assert param(merged, "temperature") == [0.2]
    assert merged["position"] == llm["position"] and merged["data"]["legacy_flag"] == "keep me"
    assert param(merged, "not_in_template") == [42]  # carried over the replacement
    assert result.replaced == ["llm_a"]


def test_type_change_replaces_the_node() -> None:
    def edit(d: dict[str, Any]) -> None:
        n = decl_node(d, "llm_a")
        n["type"] = "agent"
        n["params"] = {"model_id": 1, "user_prompt": "{{#ask.user_input#}}"}

    result = merge(sent_canvas(), proposal_from(edit))
    assert node(result.canvas, "llm_a")["data"]["type"] == "agent"
    assert result.replaced == ["llm_a"]


def test_repeated_parameter_key_is_matched_in_order() -> None:
    sent = sent_canvas()
    out = node(sent, "out_1")
    out["data"]["group_params"].append(
        {"name": "extra", "params": [{"key": "message", "value": {"msg": "二", "files": []}}]}
    )
    proposal = copy.deepcopy(sent)
    proposal["nodes"] = [n for n in proposal["nodes"] if n["id"] != "note_x1"]
    pout = node(proposal, "out_1")
    pout["data"]["group_params"][-1]["params"][0]["value"] = {"msg": "贰", "files": []}
    result = merge(sent, proposal)
    assert [v["msg"] for v in param(node(result.canvas, "out_1"), "message")] == ["{{#llm_a.output#}}", "贰"]
    assert result.replaced == []


# ---- new nodes and layout ------------------------------------------------------------------


def _box(n: dict[str, Any]) -> tuple[float, float, float, float]:
    w, h = (
        (n.get("measured") or {}).get("width", DEFAULT_NODE_SIZE[0]),
        (n.get("measured") or {}).get("height", DEFAULT_NODE_SIZE[1]),
    )
    return n["position"]["x"], n["position"]["y"], n["position"]["x"] + w, n["position"]["y"] + h


def _overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ax0, ay0, ax1, ay1 = _box(a)
    bx0, by0, bx1, by1 = _box(b)
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def test_new_nodes_sit_right_of_the_canvas_without_overlap_and_old_ones_do_not_move() -> None:
    def edit(d: dict[str, Any]) -> None:
        for i in range(3):
            d["nodes"].append(
                {
                    "id": f"sum_{i}",
                    "type": "llm",
                    "params": {"model_id": 1, "user_prompt": "{{#llm_a.output#}}"},
                    "position": {"x": 0, "y": 0},
                }
            )
            d["edges"].append({"from": "llm_a", "to": f"sum_{i}"})
        d["nodes"].append(
            {"id": "after", "type": "llm", "params": {"model_id": 1, "user_prompt": "{{#sum_0.output#}}"}}
        )
        d["edges"].append({"from": "sum_0", "to": "after"})

    sent = sent_canvas()
    result = merge(sent, proposal_from(edit))
    old_right = max(_box(n)[2] for n in sent["nodes"])
    new = [node(result.canvas, i) for i in ("sum_0", "sum_1", "sum_2", "after")]
    assert all(_box(n)[0] > old_right for n in new)
    assert node(result.canvas, "after")["position"]["x"] > node(result.canvas, "sum_0")["position"]["x"]  # next layer
    nodes = result.canvas["nodes"]
    assert not [(a["id"], b["id"]) for i, a in enumerate(nodes) for b in nodes[i + 1 :] if _overlap(a, b)]
    for n in sent["nodes"]:
        assert node(result.canvas, n["id"])["position"] == n["position"]


def test_empty_canvas_gets_a_complete_layout() -> None:
    sent = {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 1}}
    result = merge(sent, compile_(BASE_SPEC))
    assert {n["id"] for n in result.canvas["nodes"]} == {n["id"] for n in BASE_SPEC["nodes"]}
    nodes = result.canvas["nodes"]
    assert not [(a["id"], b["id"]) for i, a in enumerate(nodes) for b in nodes[i + 1 :] if _overlap(a, b)]
    xs = [node(result.canvas, i)["position"]["x"] for i in ("start_1", "ask", "llm_a", "out_1", "end_1")]
    assert xs == sorted(xs)


# ---- the preview is what apply lands -------------------------------------------------------


def test_preview_diff_is_the_diff_of_the_merged_canvas() -> None:
    def edit(d: dict[str, Any]) -> None:
        decl_node(d, "llm_a")["params"]["system_prompt"] = "新的"
        d["nodes"] = [n for n in d["nodes"] if n["id"] != "out_1"]
        d["edges"] = [e for e in d["edges"] if "out_1" not in (e["from"], e["to"])] + [{"from": "llm_a", "to": "end_1"}]

    sent = sent_canvas()
    result = merge(sent, proposal_from(edit))
    assert result.diff == diff(result.canvas, sent)
    # and nothing the proposal asked for was lost on the way (the sent node's extra,
    # unknown parameter is the only thing the merged canvas has that the proposal lacks)
    leftover = diff(result.canvas, proposal_from(edit))
    assert leftover["nodes_only_local"] == [] and leftover["nodes_only_online"] == []
    assert leftover["edges_only_local"] == [] and leftover["edges_only_online"] == []
    assert [(c["id"], [p["key"] for p in c["params"]]) for c in leftover["nodes_changed"]] == [
        ("llm_a", ["not_in_template"])
    ]


def test_the_sent_canvas_is_not_mutated() -> None:
    sent = sent_canvas()
    snapshot = copy.deepcopy(sent)
    merge(sent, proposal_from(lambda d: decl_node(d, "llm_a")["params"].update(system_prompt="x")))
    assert sent == snapshot
