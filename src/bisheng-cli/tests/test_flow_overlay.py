"""F005 T020 — the constrained Python overlay (design decision 6, AC-08 / AC-09).

The YAML is the single source of truth, so an overlay may only:

* override the **value** of a parameter the YAML already declares on a node,
* append nodes the YAML does not declare,
* append edges the YAML does not declare.

Everything else that touches declared structure is refused, and refused
**item by item** — which node, which parameter, which edge — never a bare "not
allowed". The overlay is trusted code the developer passes explicitly; what is
constrained is its effect on the workflow, not what the code can do.
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path

import pytest

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError
from bisheng_cli.flow.overlay import apply_overlay

SPEC = {
    "name": "wf",
    "nodes": [
        {"id": "start_1", "type": "start"},
        {"id": "llm_1", "type": "llm", "params": {"model_id": 1, "temperature": 0.3}},
        {"id": "end_1", "type": "end"},
    ],
    "edges": [
        {"from": "start_1", "to": "llm_1"},
        {"from": "llm_1", "to": "end_1"},
    ],
}


def overlay(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "overlay.py"
    path.write_text("def apply(spec):\n" + textwrap.indent(textwrap.dedent(body), "    "), encoding="utf-8")
    return path


def refused(tmp_path: Path, body: str) -> list[dict]:
    with pytest.raises(CliError) as excinfo:
        apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, body))
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    return excinfo.value.details["refused"]


# ---- allowed -----------------------------------------------------------------


def test_can_override_declared_param(tmp_path: Path) -> None:
    result = apply_overlay(
        copy.deepcopy(SPEC), overlay(tmp_path, "spec['nodes'][1]['params']['model_id'] = 7\nreturn spec\n")
    )
    assert result["nodes"][1]["params"] == {"model_id": 7, "temperature": 0.3}


def test_can_append_undeclared_node(tmp_path: Path) -> None:
    result = apply_overlay(
        copy.deepcopy(SPEC), overlay(tmp_path, "spec['nodes'].append({'id': 'out_1', 'type': 'output'})\nreturn spec\n")
    )
    assert [n["id"] for n in result["nodes"]] == ["start_1", "llm_1", "end_1", "out_1"]


def test_can_append_undeclared_edge(tmp_path: Path) -> None:
    result = apply_overlay(
        copy.deepcopy(SPEC),
        overlay(tmp_path, "spec['edges'].append({'from': 'start_1', 'to': 'end_1'})\nreturn spec\n"),
    )
    assert len(result["edges"]) == 3


def test_overlay_may_mutate_in_place_and_return_none(tmp_path: Path) -> None:
    result = apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, "spec['nodes'][1]['params']['temperature'] = 0\n"))
    assert result["nodes"][1]["params"]["temperature"] == 0


def test_caller_spec_is_not_mutated(tmp_path: Path) -> None:
    original = copy.deepcopy(SPEC)
    apply_overlay(original, overlay(tmp_path, "spec['nodes'][1]['params']['model_id'] = 7\n"))
    assert original == SPEC


def test_untouched_spec_passes_through(tmp_path: Path) -> None:
    assert apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, "return spec\n")) == SPEC


# ---- refused, item by item -----------------------------------------------------


def test_delete_node_refused_and_itemised(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'] = [n for n in spec['nodes'] if n['id'] != 'llm_1']\nreturn spec\n")
    assert {"kind": "node_deleted", "node": "llm_1"} in items


def test_delete_edge_refused_and_itemised(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['edges'].pop(1)\nreturn spec\n")
    assert {"kind": "edge_deleted", "edge": {"from": "llm_1", "to": "end_1", "handle": "right_handle"}} in items


def test_change_edge_handle_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['edges'][1]['handle'] = 'case_a'\nreturn spec\n")
    assert items == [
        {
            "kind": "edge_changed",
            "edge": {"from": "llm_1", "to": "end_1", "handle": "right_handle"},
            "now": {"from": "llm_1", "to": "end_1", "handle": "case_a"},
        }
    ]


def test_change_edge_target_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['edges'][0]['to'] = 'end_1'\nreturn spec\n")
    assert [item["kind"] for item in items] == ["edge_changed"]
    assert items[0]["now"]["to"] == "end_1"


def test_change_node_id_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'][1]['id'] = 'llm_renamed'\nreturn spec\n")
    assert {"kind": "node_id_changed", "node": "llm_1", "now": "llm_renamed"} in items


def test_change_node_type_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'][1]['type'] = 'rag'\nreturn spec\n")
    assert items == [{"kind": "node_type_changed", "node": "llm_1", "was": "llm", "now": "rag"}]


def test_new_undeclared_param_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'][1]['params']['max_tokens'] = 10\nreturn spec\n")
    assert items == [{"kind": "param_added", "node": "llm_1", "param": "max_tokens"}]


def test_removing_declared_param_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "del spec['nodes'][1]['params']['temperature']\nreturn spec\n")
    assert items == [{"kind": "param_deleted", "node": "llm_1", "param": "temperature"}]


def test_params_on_a_node_that_declared_none_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'][0]['params'] = {'guide_word': 'hi'}\nreturn spec\n")
    assert items == [{"kind": "param_added", "node": "start_1", "param": "guide_word"}]


def test_other_declared_node_fields_refused(tmp_path: Path) -> None:
    """Only parameter values are overridable — ``tool`` / ``tab`` / ``cases`` are structure."""
    items = refused(tmp_path, "spec['nodes'][1]['tab'] = 'x'\nreturn spec\n")
    assert items == [{"kind": "node_field_changed", "node": "llm_1", "field": "tab"}]


def test_top_level_fields_refused(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['name'] = 'other'\nreturn spec\n")
    assert items == [{"kind": "top_level_changed", "field": "name"}]


def test_all_violations_reported_together(tmp_path: Path) -> None:
    items = refused(
        tmp_path,
        """
        spec['nodes'][1]['params']['max_tokens'] = 10
        spec['nodes'][1]['type'] = 'rag'
        spec['edges'].pop(0)
        return spec
        """,
    )
    assert {item["kind"] for item in items} == {"param_added", "node_type_changed", "edge_deleted"}


def test_message_names_each_refused_item(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, "spec['nodes'][1]['params']['max_tokens'] = 1\n"))
    assert "llm_1" in excinfo.value.message and "max_tokens" in excinfo.value.message


# ---- the overlay file itself -----------------------------------------------------


def test_overlay_without_apply_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "overlay.py"
    path.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(CliError) as excinfo:
        apply_overlay(copy.deepcopy(SPEC), path)
    assert "apply" in excinfo.value.message


def test_overlay_returning_non_mapping_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, "return [1, 2]\n"))


def test_overlay_exception_is_reported_not_crashed(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        apply_overlay(copy.deepcopy(SPEC), overlay(tmp_path, "raise ValueError('bad env')\n"))
    assert "ValueError" in excinfo.value.message and "bad env" in excinfo.value.message


def test_missing_overlay_file_is_local_error(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        apply_overlay(copy.deepcopy(SPEC), tmp_path / "absent.py")
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID


def test_appended_node_reusing_declared_id_refused(tmp_path: Path) -> None:
    """Only *undeclared* nodes may be appended — a second ``llm_1`` is a duplicate, not an addition."""
    items = refused(
        tmp_path,
        "spec['nodes'].append({'id': 'llm_1', 'type': 'llm', 'params': {'model_id': 1, 'temperature': 0.3}})\n",
    )
    assert {"kind": "node_duplicated", "node": "llm_1"} in items


def test_appended_nodes_with_same_new_id_refused(tmp_path: Path) -> None:
    items = refused(
        tmp_path, "spec['nodes'] += [{'id': 'out_1', 'type': 'output'}, {'id': 'out_1', 'type': 'output'}]\n"
    )
    assert {"kind": "node_duplicated", "node": "out_1"} in items


def test_unhashable_ids_in_overlay_result_are_reported_not_raised(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'].append({'id': {'x': 1}, 'type': 'output'})\n")
    assert items == [{"kind": "node_id_invalid", "index": 3}]


def test_overlay_result_without_nodes_refuses_each_declared_node(tmp_path: Path) -> None:
    items = refused(tmp_path, "spec['nodes'] = None\n")
    assert {item["node"] for item in items if item["kind"] == "node_deleted"} == {"start_1", "llm_1", "end_1"}
