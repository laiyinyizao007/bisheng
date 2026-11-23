"""T028 — the structural diff (``bisheng_cli/flow/diff.py``), probed in both directions.

Every rule in design decision 5's comparison table has two tests here: one
showing the difference **is** reported when it matters, one showing it is
**not** reported when it does not. A diff that only has the first kind passes
by reporting everything; one that only has the second passes by reporting
nothing. Both failure modes were seen in the wild: the old ``--update`` path
reported nothing and silently overwrote, and a byte-level JSON diff reports
every canvas drag.

The local side is a real compile of a real declaration against the frozen
templates, so these tests exercise the node shape the compiler actually emits,
not a hand-written approximation of it.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from bisheng_cli.errors import EXIT_DEFECT, EXIT_LOCAL_INVALID, CliError
from bisheng_cli.flow.compiler import compile_spec
from bisheng_cli.flow.diff import diff, online_only_items, render
from tests.helpers.flow_fixtures import find_param, templates, tools

SPEC: dict[str, Any] = {
    "name": "研究",
    "nodes": [
        {"id": "start_1", "type": "start"},
        {"id": "input_1", "type": "input", "tab": "dialog_input"},
        {
            "id": "llm_1",
            "type": "llm",
            "name": "写代码",
            "params": {"model_id": 7, "system_prompt": "你是研究员", "user_prompt": "{{#input_1.user_input#}}"},
        },
        {"id": "tool_1", "type": "tool", "tool": "submit_candidate", "params": {"name": "x", "source": "y"}},
        {
            "id": "cond_1",
            "type": "condition",
            "cases": [{"id": "stop", "conditions": [{"left": "input_1.user_input", "op": "equals", "value": "结束"}]}],
        },
        {"id": "end_1", "type": "end"},
    ],
    "edges": [
        {"from": "start_1", "to": "input_1"},
        {"from": "input_1", "to": "llm_1"},
        {"from": "llm_1", "to": "tool_1"},
        {"from": "tool_1", "to": "cond_1"},
        {"from": "cond_1", "to": "end_1", "handle": "stop"},
        {"from": "cond_1", "to": "input_1"},
    ],
}


def _local() -> dict[str, Any]:
    return compile_spec(copy.deepcopy(SPEC), templates(), tools())


def _node(definition: dict[str, Any], node_id: str) -> dict[str, Any]:
    return next(n for n in definition["nodes"] if n["id"] == node_id)


def _counts(result: dict[str, Any]) -> dict[str, int]:
    return {k: len(v) for k, v in result.items() if isinstance(v, list)}


def _only(result: dict[str, Any], key: str) -> list:
    """Assert ``key`` is the only non-empty section and return it."""
    others = {k: v for k, v in result.items() if isinstance(v, list) and v and k != key}
    assert not others, f"unexpected sections: {others}"
    assert result[key], f"{key} is empty"
    assert result["identical"] is False
    return result[key]


# ---- baseline -------------------------------------------------------------------


def test_identical_definitions_have_no_diff() -> None:
    result = diff(_local(), _local())
    assert result["identical"] is True
    assert all(v == 0 for v in _counts(result).values())
    assert render(result) == ["本地定义与线上一致（不计画布坐标与视图）"]


# ---- must report ----------------------------------------------------------------


def test_reports_node_only_online() -> None:
    online = _local()
    extra = copy.deepcopy(_node(online, "llm_1"))
    extra["id"] = extra["data"]["id"] = "llm_web"
    extra["data"]["name"] = "网页上加的"
    online["nodes"].append(extra)
    (item,) = _only(diff(_local(), online), "nodes_only_online")
    assert item == {"id": "llm_web", "type": "llm", "name": "网页上加的"}


def test_reports_node_only_local() -> None:
    online = _local()
    online["nodes"] = [n for n in online["nodes"] if n["id"] != "tool_1"]
    (item,) = _only(diff(_local(), online), "nodes_only_local")
    assert item["id"] == "tool_1" and item["type"] == "tool"


def test_reports_changed_param() -> None:
    online = _local()
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "网页上改过的提示词"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["id"] == "llm_1"
    assert node["fields"] == []
    assert node["params"] == [{"key": "system_prompt", "online": "网页上改过的提示词", "local": "你是研究员"}]


def test_reports_param_present_on_one_side_only() -> None:
    online = _local()
    group = _node(online, "llm_1")["data"]["group_params"][0]
    group["params"].append({"key": "retry", "type": "number", "value": 3})
    local = _local()
    (node,) = _only(diff(local, online), "nodes_changed")
    assert node["params"] == [{"key": "retry", "online": 3, "only": "online"}]

    (node,) = _only(diff(online, local), "nodes_changed")
    assert node["params"] == [{"key": "retry", "local": 3, "only": "local"}]


def test_reports_edge_only_online() -> None:
    online = _local()
    online["edges"].append(
        {
            "id": "xy-edge__x",
            "source": "llm_1",
            "sourceHandle": "right_handle",
            "target": "end_1",
            "targetHandle": "left_handle",
            "type": "customEdge",
            "animated": True,
        }
    )
    (edge,) = _only(diff(_local(), online), "edges_only_online")
    assert edge == {"source": "llm_1", "sourceHandle": "right_handle", "target": "end_1"}


def test_reports_edge_only_local() -> None:
    online = _local()
    online["edges"] = [e for e in online["edges"] if e["sourceHandle"] != "stop"]
    (edge,) = _only(diff(_local(), online), "edges_only_local")
    assert edge == {"source": "cond_1", "sourceHandle": "stop", "target": "end_1"}


def test_edge_handle_change_is_one_removed_and_one_added() -> None:
    """Same endpoints, different handle: a different route, not the same edge."""
    online = _local()
    for edge in online["edges"]:
        if edge["sourceHandle"] == "stop":
            edge["sourceHandle"] = "right_handle"
    result = diff(_local(), online)
    assert result["edges_only_online"] == [{"source": "cond_1", "sourceHandle": "right_handle", "target": "end_1"}]
    assert result["edges_only_local"] == [{"source": "cond_1", "sourceHandle": "stop", "target": "end_1"}]


def test_duplicate_edge_counts() -> None:
    """Edges are a multiset: a doubled edge online is one extra edge, not nothing."""
    online = _local()
    online["edges"].append(copy.deepcopy(online["edges"][0]))
    (edge,) = _only(diff(_local(), online), "edges_only_online")
    assert edge["source"] == "start_1"


def test_tab_value_is_a_diff() -> None:
    online = _local()
    _node(online, "input_1")["data"]["tab"]["value"] = "form_input"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["fields"] == [{"field": "tab", "online": "form_input", "local": "dialog_input"}]


def test_tool_key_is_a_diff() -> None:
    online = _local()
    _node(online, "tool_1")["data"]["tool_key"] = "key_other"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["fields"] == [{"field": "tool_key", "online": "key_other", "local": "key_submit"}]


def test_name_is_a_diff() -> None:
    online = _local()
    _node(online, "llm_1")["data"]["name"] = "改名了"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["fields"] == [{"field": "name", "online": "改名了", "local": "写代码"}]


def test_description_and_type_are_diffs() -> None:
    online = _local()
    _node(online, "llm_1")["data"]["description"] = "新描述"
    _node(online, "llm_1")["data"]["type"] = "agent"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert {f["field"] for f in node["fields"]} == {"description", "type"}


def test_condition_cases_are_compared() -> None:
    online = _local()
    find_param(_node(online, "cond_1"), "condition")["value"][0]["conditions"][0]["right_value"] = "停"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert [p["key"] for p in node["params"]] == ["condition"]


def test_value_type_change_is_a_diff() -> None:
    """``7`` and ``"7"`` (or ``True`` and ``1``) are different values — the engine may treat them differently."""
    online = _local()
    find_param(_node(online, "llm_1"), "model_id")["value"] = "7"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["params"] == [{"key": "model_id", "online": "7", "local": 7}]

    online = _local()
    local = _local()
    find_param(_node(local, "llm_1"), "model_id")["value"] = 1
    find_param(_node(online, "llm_1"), "model_id")["value"] = True
    assert diff(local, online)["nodes_changed"]


def test_data_id_is_a_diff() -> None:
    """The engine builds its graph on ``data.id``, not on the canvas ``node.id``."""
    online = _local()
    _node(online, "llm_1")["data"]["id"] = "llm_other"
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["fields"] == [{"field": "id", "online": "llm_other", "local": "llm_1"}]


def test_online_only_items_is_what_a_push_would_drop() -> None:
    """AC-26: online nodes / edges / values the local side does not reproduce — not local-only additions."""
    online, local = _local(), _local()
    extra = copy.deepcopy(_node(online, "llm_1"))
    extra["id"] = extra["data"]["id"] = "llm_web"
    online["nodes"].append(extra)
    online["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "end_1"})
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "网页改的"
    _node(online, "llm_1")["data"]["name"] = "网页名"
    _node(online, "llm_1")["data"]["group_params"][0]["params"].append({"key": "retry", "value": 3})
    # Local-only additions: a push adds these, it loses nothing.
    _node(local, "tool_1")["data"]["group_params"][0]["params"].append({"key": "local_only", "value": 1})
    local["nodes"].append(dict(copy.deepcopy(_node(local, "end_1")), id="end_2"))
    online["nodes"].append({"id": "note_1", "type": "noteNode", "data": {"id": "note_1", "type": "note"}})

    items = online_only_items(diff(local, online))
    assert {
        (i["kind"], i.get("id") or i.get("node"), i.get("key") or i.get("field") or i.get("target")) for i in items
    } == {
        ("node", "llm_web", None),
        ("edge", None, "end_1"),
        ("param", "llm_1", "system_prompt"),
        ("param", "llm_1", "retry"),
        ("field", "llm_1", "name"),
    }
    assert next(i for i in items if i.get("key") == "system_prompt")["online"] == "网页改的"


def test_no_online_only_items_when_local_only_adds() -> None:
    local = _local()
    local["nodes"].append(dict(copy.deepcopy(_node(local, "end_1")), id="end_2"))
    result = diff(local, _local())
    assert result["identical"] is False
    assert online_only_items(result) == []


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_values_are_refused_not_emitted(bad: float) -> None:
    """``--json`` must stay RFC 8259 JSON: NaN / Infinity cannot appear in the diff output."""
    local = _local()
    find_param(_node(local, "llm_1"), "temperature")["value"] = bad
    with pytest.raises(CliError) as caught:
        diff(local, _local())
    assert caught.value.exit_code == EXIT_LOCAL_INVALID
    online = _local()
    online["edges"][0]["source"] = bad
    with pytest.raises(CliError) as caught:
        diff(_local(), online)
    assert caught.value.exit_code == EXIT_DEFECT


def test_null_edges_is_no_edges() -> None:
    local, online = _local(), _local()
    local["edges"] = []
    online["edges"] = None
    assert diff(local, online)["identical"] is True


# ---- must not report ------------------------------------------------------------


def _note(node_id: str, text: str = "批注") -> dict:
    return {
        "id": node_id,
        "type": "noteNode",
        "position": {"x": 0, "y": 0},
        "data": {"id": node_id, "type": "note", "value": text},
    }


def test_note_nodes_are_not_execution_differences() -> None:
    """Canvas annotations: skipped by the engine, absent from the DSL — reported apart, never counted."""
    online = _local()
    online["nodes"].append(_note("note_abc12"))
    result = diff(_local(), online)
    assert result["identical"] is True
    assert result["notes_only_online"] == [{"id": "note_abc12", "type": "note", "name": None}]
    assert result["notes_only_local"] == []
    lines = render(result)
    assert lines[0] == "本地定义与线上一致（不计画布坐标与视图）"
    assert any("批注" in line and "note_abc12" in line for line in lines[1:])
    assert not any(line.startswith("  ") for line in lines), "a note is not a diff item"


def test_note_text_change_is_not_a_diff_and_notes_do_not_hide_real_ones() -> None:
    local, online = _local(), _local()
    local["nodes"].append(_note("note_1", "旧"))
    online["nodes"].append(_note("note_1", "新"))
    assert diff(local, online)["identical"] is True

    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "改了"
    result = diff(local, online)
    assert [n["id"] for n in result["nodes_changed"]] == ["llm_1"]
    assert result["notes_only_online"] == result["notes_only_local"] == []


def test_null_group_params_is_a_node_without_params() -> None:
    """``BaseNodeData.group_params`` is Optional — ``null`` is legal, not a defect."""
    local, online = _local(), _local()
    _node(local, "start_1")["data"]["group_params"] = []
    _node(online, "start_1")["data"]["group_params"] = None
    assert diff(local, online)["identical"] is True


def test_position_only_is_not_a_diff() -> None:
    online = _local()
    for index, node in enumerate(online["nodes"]):
        node["position"] = {"x": 999 + index, "y": -42}
        node["positionAbsolute"] = {"x": 1, "y": 2}
        node["measured"] = {"width": 334, "height": 500}
        node["selected"] = True
    assert diff(_local(), online)["identical"] is True


def test_viewport_only_is_not_a_diff() -> None:
    online = _local()
    online["viewport"] = {"x": -300, "y": 120, "zoom": 0.6}
    assert diff(_local(), online)["identical"] is True
    del online["viewport"]
    assert diff(_local(), online)["identical"] is True


def test_node_version_only_is_not_a_diff() -> None:
    online = _local()
    for node in online["nodes"]:
        node["data"]["v"] = "99"
    assert diff(_local(), online)["identical"] is True


def test_param_label_only_is_not_a_diff() -> None:
    online = _local()
    for node in online["nodes"]:
        for group in node["data"].get("group_params") or []:
            group["name"] = "改过的分组名"
            for param in group["params"]:
                param["label"] = "界面文案"
                param["desc"] = "说明"
                param["type"] = "changed"
                param["required"] = not param.get("required")
                param["placeholder"] = "占位"
                param["options"] = [{"label": "a", "value": "a"}]
                param["help"] = "帮助"
    assert diff(_local(), online)["identical"] is True


def test_edge_metadata_is_not_a_diff() -> None:
    online = _local()
    for edge in online["edges"]:
        edge["id"] = "reactflow__edge-" + edge["source"]
        edge["animated"] = False
        edge["type"] = "default"
        edge["targetHandle"] = "left_handle"
    online["edges"].reverse()
    assert diff(_local(), online)["identical"] is True


def test_node_order_is_not_a_diff() -> None:
    online = _local()
    online["nodes"].reverse()
    assert diff(_local(), online)["identical"] is True


def test_params_compared_by_key_not_position() -> None:
    """Adding one parameter, regrouping and reordering report the one addition — nothing else."""
    online = _local()
    data = _node(online, "llm_1")["data"]
    flat = [p for g in data["group_params"] for p in g["params"]]
    flat.reverse()
    flat.insert(0, {"key": "retry", "type": "number", "value": 3})
    data["group_params"] = [{"name": "全部", "params": flat[:2]}, {"name": "其余", "params": flat[2:]}]
    (node,) = _only(diff(_local(), online), "nodes_changed")
    assert node["params"] == [{"key": "retry", "online": 3, "only": "online"}]


def test_dict_key_order_in_values_is_not_a_diff() -> None:
    online = _local()
    param = find_param(_node(online, "cond_1"), "condition")
    param["value"] = [dict(reversed(list(case.items()))) for case in param["value"]]
    assert diff(_local(), online)["identical"] is True


# ---- locating, ordering, rendering ----------------------------------------------


def test_items_are_locatable_and_sorted() -> None:
    online = _local()
    find_param(_node(online, "llm_1"), "user_prompt")["value"] = "b"
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "a"
    _node(online, "tool_1")["data"]["name"] = "改名"
    result = diff(_local(), online)
    assert [n["id"] for n in result["nodes_changed"]] == ["llm_1", "tool_1"]
    assert [p["key"] for p in result["nodes_changed"][0]["params"]] == ["system_prompt", "user_prompt"]
    assert diff(_local(), online) == result, "the same inputs give the same output"


def test_human_and_machine_agree() -> None:
    """Every item in the structured diff appears in the rendering, and nothing else does (AC-23)."""
    online = _local()
    extra = copy.deepcopy(_node(online, "llm_1"))
    extra["id"] = "llm_web"
    online["nodes"].append(extra)
    online["nodes"] = [n for n in online["nodes"] if n["id"] != "tool_1"]
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "改过"
    _node(online, "input_1")["data"]["tab"]["value"] = "form_input"
    online["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "end_1"})
    result = diff(_local(), online)

    lines = render(result)
    items = (
        [("+", n["id"]) for n in result["nodes_only_online"]]
        + [("-", n["id"]) for n in result["nodes_only_local"]]
        + [("~", n["id"], p["key"]) for n in result["nodes_changed"] for p in n["params"]]
        + [("~", n["id"], f["field"]) for n in result["nodes_changed"] for f in n["fields"]]
        + [("+e", e["source"], e["target"]) for e in result["edges_only_online"]]
        + [("-e", e["source"], e["target"]) for e in result["edges_only_local"]]
    )
    item_lines = [line for line in lines if line.startswith("  ")]
    assert len(item_lines) == len(items)
    for item in items:
        assert any(all(part in line for part in item[1:]) for line in item_lines), item
    # The summary line counts what the details hold.
    assert f"{len(items)} 处" in lines[0]


def test_render_keeps_one_item_per_line() -> None:
    """A multi-line prompt is shown escaped on one line; a long one is cut with a pointer to --json."""
    online = _local()
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "第一行\n第二行"
    lines = render(diff(_local(), online))
    assert all("\n" not in line for line in lines)
    assert any("第一行\\n第二行" in line for line in lines)

    online = _local()
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = "长" * 5000
    (line,) = [one for one in render(diff(_local(), online)) if "system_prompt" in one]
    assert len(line) < 1000
    assert "--json" in line

    # A long value that exists on one side only is cut too.
    online = _local()
    _node(online, "llm_1")["data"]["group_params"][0]["params"].append({"key": "note", "value": "长" * 5000})
    (line,) = [one for one in render(diff(_local(), online)) if "note" in one]
    assert len(line) < 1000
    assert "只在线上" in line and "--json" in line


def test_long_values_are_cut_around_the_first_difference() -> None:
    """Two long prompts that differ only at the end: the rendering must show the end."""
    base = "前缀" * 300
    online, local = _local(), _local()
    find_param(_node(online, "llm_1"), "system_prompt")["value"] = base + "网页上补的一句"
    find_param(_node(local, "llm_1"), "system_prompt")["value"] = base + "本地写的一句"
    (line,) = [one for one in render(diff(local, online)) if "system_prompt" in one]
    assert "网页上补的一句" in line and "本地写的一句" in line
    assert "第 601 个字符起不同" in line
    assert "--json" in line
    assert len(line) < 1000


# ---- malformed input --------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(nodes="x"), id="nodes-not-list"),
        pytest.param(lambda d: d["nodes"].append("x"), id="node-not-mapping"),
        pytest.param(lambda d: d["nodes"].append({"data": {}}), id="node-without-id"),
        pytest.param(lambda d: d["nodes"].append(copy.deepcopy(d["nodes"][0])), id="duplicate-node-id"),
        pytest.param(lambda d: d["edges"].append("x"), id="edge-not-mapping"),
        pytest.param(lambda d: d.update(edges={"a": 1}), id="edges-not-list"),
        pytest.param(lambda d: d["nodes"][0].update(data="x"), id="data-not-mapping"),
        pytest.param(lambda d: d["nodes"][1]["data"].update(group_params="x"), id="groups-not-list"),
    ],
)
def test_malformed_online_is_a_defect_not_a_crash(mutate) -> None:
    online = _local()
    mutate(online)
    with pytest.raises(CliError) as caught:
        diff(_local(), online)
    assert caught.value.exit_code == EXIT_DEFECT
    assert "线上" in caught.value.message

    local = _local()
    mutate(local)
    with pytest.raises(CliError) as caught:
        diff(local, _local())
    assert caught.value.exit_code == EXIT_LOCAL_INVALID
    assert "本地" in caught.value.message
