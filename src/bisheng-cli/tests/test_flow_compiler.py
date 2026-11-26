"""F005 T022 — compiling a declaration into a platform workflow definition (AC-06).

The compiler is a pure function of (declaration, node templates, tool list): it
deep-copies the template skeleton for each node's type, fills declared params
**by key**, builds tool nodes from the ``node_template`` the tool list carries,
and emits ``{nodes, edges, viewport}``. It sends no request and fills nothing
the declaration does not say — except the platform's fixed shape (``flowNode``,
handles, a deterministic layout the diff ignores).

Anything it cannot place is a compile error naming the node and key, never a
silently dropped value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError
from bisheng_cli.flow.dsl import compile_spec
from bisheng_cli.flow.spec import load_spec
from tests.helpers.flow_fixtures import find_param, templates, tool_child, tools

ALL_TYPES = [t["type"] for t in templates()]


def compiled(spec):
    return compile_spec(spec, templates(), tools())


def node_by_id(result, node_id):
    return next(n for n in result["nodes"] if n["id"] == node_id)


def compile_error(spec) -> CliError:
    with pytest.raises(CliError) as excinfo:
        compiled(spec)
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    return excinfo.value


@pytest.mark.parametrize("node_type", ALL_TYPES)
def test_each_node_type_produces_full_skeleton(node_type) -> None:
    result = compiled({"name": "wf", "nodes": [{"id": "n1", "type": node_type}]})
    node = result["nodes"][0]
    template = next(t for t in templates() if t["type"] == node_type)

    assert node["id"] == "n1"
    assert node["type"] == "flowNode"
    assert set(node["position"]) == {"x", "y"}
    assert node["data"]["id"] == "n1"
    assert node["data"]["type"] == node_type
    assert node["data"]["v"] == template["v"]
    # Group structure comes from the template, key for key.
    assert [[p["key"] for p in g["params"]] for g in node["data"]["group_params"]] == [
        [p["key"] for p in g["params"]] for g in template["group_params"]
    ]


def test_template_placeholders_do_not_leak() -> None:
    """Templates carry ``"true"`` for name/description (i18n placeholders) — not a real name."""
    node = compiled({"name": "wf", "nodes": [{"id": "llm_1", "type": "llm"}]})["nodes"][0]
    assert node["data"]["name"] == "llm_1"
    assert node["data"]["description"] == ""
    named = compiled({"name": "wf", "nodes": [{"id": "llm_1", "type": "llm", "name": "写代码", "description": "d"}]})
    assert (named["nodes"][0]["data"]["name"], named["nodes"][0]["data"]["description"]) == ("写代码", "d")


def test_params_filled_into_matching_group() -> None:
    result = compiled(
        {
            "name": "wf",
            "nodes": [
                {
                    "id": "llm_1",
                    "type": "llm",
                    "params": {"model_id": 7, "temperature": 0.3, "user_prompt": "hi {{#input_1.user_input#}}"},
                }
            ],
        }
    )
    node = result["nodes"][0]
    assert find_param(node, "model_id")["value"] == 7
    assert find_param(node, "temperature")["value"] == 0.3
    assert find_param(node, "user_prompt")["value"] == "hi {{#input_1.user_input#}}"
    # Undeclared params keep the template's own value — the compiler fills no defaults of its own.
    template = next(t for t in templates() if t["type"] == "llm")
    expected = next(p for g in template["group_params"] for p in g["params"] if p["key"] == "system_prompt")
    assert find_param(node, "system_prompt")["value"] == expected["value"]


def test_unknown_param_key_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "llm_1", "type": "llm", "params": {"modle_id": 7}}]})
    assert {"node": "llm_1", "param": "modle_id", "kind": "unknown_param"} in error.details["issues"]


def test_tab_sets_tab_value() -> None:
    node = compiled({"name": "wf", "nodes": [{"id": "in_1", "type": "input", "tab": "form_input"}]})["nodes"][0]
    assert node["data"]["tab"]["value"] == "form_input"
    assert [o["key"] for o in node["data"]["tab"]["options"]] == ["dialog_input", "form_input"]


def test_tab_on_type_without_tab_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "e", "type": "end", "tab": "x"}]})
    assert error.details["issues"][0]["kind"] == "tab_not_supported"


def test_tool_node_resolves_tool_key() -> None:
    result = compiled(
        {
            "name": "wf",
            "nodes": [
                {
                    "id": "tool_submit",
                    "type": "tool",
                    "tool": "submit_candidate",
                    "params": {"name": "{{#llm_name.output#}}", "source": "{{#llm_code.output#}}"},
                }
            ],
        }
    )
    node = result["nodes"][0]
    assert node["type"] == "flowNode"
    assert node["data"]["type"] == "tool"
    assert node["data"]["id"] == "tool_submit"
    assert node["data"]["tool_key"] == "key_submit"
    assert find_param(node, "name")["value"] == "{{#llm_name.output#}}"
    assert find_param(node, "source")["value"] == "{{#llm_code.output#}}"
    assert find_param(node, "output")["key"] == "output"


def test_unknown_operation_id_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "t", "type": "tool", "tool": "no_such_op"}]})
    assert {"node": "t", "tool": "no_such_op", "kind": "unknown_tool"} in error.details["issues"]
    assert "no_such_op" in error.message


def test_tool_node_without_tool_reference_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "t", "type": "tool"}]})
    assert error.details["issues"][0]["kind"] == "missing_tool"


def test_ambiguous_operation_id_is_error_and_qualified_form_resolves() -> None:
    catalogue = [*tools(), {"id": 4, "name": "other", "children": [tool_child("submit_candidate", "key_x", ["name"])]}]
    spec = {"name": "wf", "nodes": [{"id": "t", "type": "tool", "tool": "submit_candidate"}]}
    with pytest.raises(CliError) as excinfo:
        compile_spec(spec, templates(), catalogue)
    assert excinfo.value.details["issues"][0]["kind"] == "ambiguous_tool"
    assert sorted(excinfo.value.details["issues"][0]["candidates"]) == [
        "invest/submit_candidate",
        "other/submit_candidate",
    ]

    spec["nodes"][0]["tool"] = "other/submit_candidate"
    assert compile_spec(spec, templates(), catalogue)["nodes"][0]["data"]["tool_key"] == "key_x"


def test_tool_params_must_be_strings() -> None:
    error = compile_error(
        {"name": "wf", "nodes": [{"id": "t", "type": "tool", "tool": "submit_candidate", "params": {"name": 3}}]}
    )
    assert {"node": "t", "param": "name", "kind": "tool_param_not_string"} in error.details["issues"]


def test_unknown_node_type_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "x", "type": "llmm"}]})
    assert {"node": "x", "type": "llmm", "kind": "unknown_type"} in error.details["issues"]


def test_condition_cases_compile_into_condition_param() -> None:
    result = compiled(
        {
            "name": "wf",
            "nodes": [
                {
                    "id": "cond_stop",
                    "type": "condition",
                    "cases": [
                        {
                            "id": "stopcase",
                            "logic": "and",
                            "conditions": [{"left": "input_idea.user_input", "op": "regex", "value": r"^\s*结束\s*$"}],
                        },
                        {"id": "ref_case", "conditions": [{"left": "a.x", "op": "equals", "ref": "b.y"}]},
                    ],
                }
            ],
        }
    )
    value = find_param(result["nodes"][0], "condition")["value"]
    assert value[0]["id"] == "stopcase"
    assert value[0]["operator"] == "and"
    one = value[0]["conditions"][0]
    assert (one["left_var"], one["comparison_operation"], one["right_value_type"], one["right_value"]) == (
        "input_idea.user_input",
        "regex",
        "input",
        r"^\s*结束\s*$",
    )
    assert one["id"]  # each condition gets a stable id
    ref = value[1]["conditions"][0]
    assert (ref["right_value_type"], ref["right_value"]) == ("ref", "b.y")
    assert value[1]["operator"] == "and"  # platform default when not declared


def test_declared_condition_id_is_kept() -> None:
    """A condition may carry its own ``id`` (an existing online flow has one); otherwise it is generated."""
    spec = {
        "name": "wf",
        "nodes": [
            {
                "id": "c",
                "type": "condition",
                "cases": [
                    {
                        "id": "k",
                        "conditions": [
                            {"id": "stopword", "left": "a.b", "op": "is_empty"},
                            {"left": "a.b", "op": "is_empty"},
                        ],
                    }
                ],
            }
        ],
    }
    conditions = find_param(compiled(spec)["nodes"][0], "condition")["value"][0]["conditions"]
    assert [one["id"] for one in conditions] == ["stopword", "k_c1"]


def test_condition_labels_follow_canvas_convention() -> None:
    """``left_label`` / ``right_label`` are display-only, but the canvas writes ``<node name>/<var>``.

    Matching it keeps a flow built here from diffing against the same flow edited in the UI.
    """
    spec = {
        "name": "wf",
        "nodes": [
            {"id": "input_idea", "type": "input", "name": "研究想法"},
            {
                "id": "c",
                "type": "condition",
                "cases": [
                    {
                        "id": "k",
                        "conditions": [
                            {"left": "input_idea.user_input", "op": "equals", "ref": "input_idea.user_input"},
                            {"left": "ghost.x", "op": "equals", "value": "1", "label": "自定义"},
                        ],
                    }
                ],
            },
        ],
    }
    first, second = find_param(node_by_id(compiled(spec), "c"), "condition")["value"][0]["conditions"]
    assert (first["left_label"], first["right_label"]) == ("研究想法/user_input", "研究想法/user_input")
    assert (second["left_label"], second["right_label"]) == ("自定义", "")


def test_condition_ids_are_deterministic() -> None:
    spec = {
        "name": "wf",
        "nodes": [
            {"id": "c", "type": "condition", "cases": [{"id": "k", "conditions": [{"left": "a.b", "op": "is_empty"}]}]}
        ],
    }
    assert compiled(spec) == compiled(spec)


def test_cases_and_raw_condition_param_conflict() -> None:
    error = compile_error(
        {"name": "wf", "nodes": [{"id": "c", "type": "condition", "cases": [], "params": {"condition": []}}]}
    )
    assert error.details["issues"][0]["kind"] == "cases_conflict"


def test_edges_compiled_with_platform_shape() -> None:
    result = compiled(
        {
            "name": "wf",
            "nodes": [{"id": "a", "type": "start"}, {"id": "c", "type": "condition"}, {"id": "e", "type": "end"}],
            "edges": [{"from": "a", "to": "c"}, {"from": "c", "to": "e", "handle": "stopcase"}],
        }
    )
    first, second = result["edges"]
    assert first == {
        "id": "xy-edge__aright_handle-cleft_handle",
        "source": "a",
        "sourceHandle": "right_handle",
        "target": "c",
        "targetHandle": "left_handle",
        "animated": True,
        "type": "customEdge",
    }
    assert (second["sourceHandle"], second["id"]) == ("stopcase", "xy-edge__cstopcase-eleft_handle")


def test_edge_missing_endpoint_is_error() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "a", "type": "start"}], "edges": [{"from": "a"}]})
    assert error.details["issues"][0]["kind"] == "edge_missing_endpoint"


def test_included_prompt_expanded(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "sys.md").write_text("系统提示词", encoding="utf-8")
    (tmp_path / "flow.yaml").write_text(
        "name: wf\nnodes:\n  - id: llm_1\n    type: llm\n    params:\n      system_prompt: !include prompts/sys.md\n",
        encoding="utf-8",
    )
    node = compiled(load_spec(tmp_path / "flow.yaml"))["nodes"][0]
    assert find_param(node, "system_prompt")["value"] == "系统提示词"


def test_output_has_nodes_edges_viewport() -> None:
    result = compiled({"name": "wf", "nodes": [{"id": "s", "type": "start"}]})
    assert set(result) == {"nodes", "edges", "viewport"}
    assert result["edges"] == []
    assert set(result["viewport"]) == {"x", "y", "zoom"}


def test_compile_is_deterministic_and_does_not_mutate_inputs() -> None:
    spec = {"name": "wf", "nodes": [{"id": "s", "type": "start"}, {"id": "e", "type": "end"}], "edges": []}
    tpl, catalogue = templates(), tools()
    before = (repr(spec), repr(tpl), repr(catalogue))
    assert compile_spec(spec, tpl, catalogue) == compile_spec(spec, tpl, catalogue)
    assert (repr(spec), repr(tpl), repr(catalogue)) == before


def test_unknown_node_field_is_error_not_dropped() -> None:
    """A typo like ``parms`` must not vanish silently with the values it carried."""
    error = compile_error({"name": "wf", "nodes": [{"id": "l", "type": "llm", "parms": {"model_id": 7}}]})
    assert {"kind": "unknown_field", "node": "l", "field": "parms"} in error.details["issues"]


def test_all_compile_errors_reported_together() -> None:
    error = compile_error(
        {
            "name": "wf",
            "nodes": [
                {"id": "x", "type": "llmm"},
                {"id": "l", "type": "llm", "params": {"nope": 1}},
                {"id": "t", "type": "tool", "tool": "missing"},
            ],
        }
    )
    assert {i["kind"] for i in error.details["issues"]} == {"unknown_type", "unknown_param", "unknown_tool"}


# ---- G8 round 1 -----------------------------------------------------------------


def _cond(conditions, **case):
    return {
        "name": "wf",
        "nodes": [{"id": "c", "type": "condition", "cases": [{"id": "k", "conditions": conditions, **case}]}],
    }


def test_engine_style_keys_in_cases_are_refused_not_dropped() -> None:
    """``left_var`` / ``comparison_operation`` are the *platform's* names; the DSL says ``left`` / ``op``.

    Silently ignoring them compiled ``left_var: null`` — a pydantic error at graph build.
    """
    error = compile_error(_cond([{"left_var": "a.b", "comparison_operation": "equals", "value": "1"}]))
    kinds = {(i["kind"], i.get("field")) for i in error.details["issues"]}
    assert ("unknown_condition_field", "left_var") in kinds
    assert ("unknown_condition_field", "comparison_operation") in kinds
    case_error = compile_error(_cond([], operator="or"))
    assert ("unknown_case_field", "operator") in {(i["kind"], i.get("field")) for i in case_error.details["issues"]}


@pytest.mark.parametrize("missing", ["left", "op"])
def test_condition_without_left_or_op_is_error(missing) -> None:
    condition = {"left": "a.b", "op": "equals", "value": "1"}
    del condition[missing]
    error = compile_error(_cond([condition]))
    assert {"kind": "condition_missing_field", "node": "c", "case": "k", "field": missing} in error.details["issues"]


def test_condition_value_and_ref_are_exclusive() -> None:
    error = compile_error(_cond([{"left": "a.b", "op": "equals", "value": "1", "ref": "a.c"}]))
    assert error.details["issues"][0]["kind"] == "condition_value_and_ref"


def test_null_value_compiles_to_empty_string() -> None:
    """``ConditionOne.right_value`` is a required ``str``; ``value: null`` (or none, for is_empty) → ``""``."""
    result = compiled(_cond([{"left": "a.b", "op": "is_empty", "value": None}, {"left": "a.b", "op": "is_empty"}]))
    values = [one["right_value"] for one in find_param(result["nodes"][0], "condition")["value"][0]["conditions"]]
    assert values == ["", ""]


def test_unknown_top_level_key_is_error() -> None:
    error = compile_error({"name": "wf", "descripton": "typo", "nodes": [{"id": "s", "type": "start"}]})
    assert {"kind": "unknown_top_level_field", "field": "descripton"} in error.details["issues"]


@pytest.mark.parametrize(
    ("templates_in", "tools_in"),
    [([{"group_params": []}], []), (["not-a-template"], []), ([], ["not-a-tool-type"]), ([], [{"children": ["x"]}])],
    ids=["template-without-type", "template-not-mapping", "tool-type-not-mapping", "child-not-mapping"],
)
def test_malformed_platform_lists_are_cli_errors(templates_in, tools_in) -> None:
    with pytest.raises(CliError) as excinfo:
        compile_spec({"name": "wf", "nodes": [{"id": "s", "type": "start"}]}, templates_in, tools_in)
    assert "平台" in excinfo.value.message


def test_non_string_node_type_is_reported_not_raised() -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "x", "type": ["llm"]}]})
    assert error.details["issues"][0]["kind"] == "unknown_type"


@pytest.mark.parametrize("bad_id", [["a"], {"x": 1}, 3, ""])
def test_non_string_node_id_is_reported_not_raised(bad_id) -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": bad_id, "type": "start"}]})
    assert error.details["issues"][0]["kind"] == "node_id_invalid"


@pytest.mark.parametrize("edge", [{"from": {"x": 1}, "to": "a"}, {"from": "a", "to": ["a"]}, {"from": "a", "to": 3}])
def test_non_string_edge_end_is_reported(edge) -> None:
    error = compile_error({"name": "wf", "nodes": [{"id": "a", "type": "start"}], "edges": [edge]})
    assert error.details["issues"][0]["kind"] == "edge_missing_endpoint"


# ---- G8 round 2 ------------------------------------------------------------------


@pytest.mark.parametrize("logic", ["AND", "Or", "xor", 1])
def test_unknown_logic_is_error(logic) -> None:
    """The engine treats anything but ``"and"`` as OR — ``AND`` would silently invert the case."""
    error = compile_error(_cond([{"left": "a.b", "op": "is_empty"}], logic=logic))
    assert error.details["issues"][0]["kind"] == "condition_bad_logic"


@pytest.mark.parametrize("logic", ["and", "or"])
def test_known_logic_compiles(logic) -> None:
    result = compiled(_cond([{"left": "a.b", "op": "is_empty"}], logic=logic))
    assert find_param(result["nodes"][0], "condition")["value"][0]["operator"] == logic


@pytest.mark.parametrize(
    "template",
    [
        {"type": "start", "group_params": ["not-a-group"]},
        {"type": "start", "group_params": [{"params": [{"no_key": 1}]}]},
        {"type": "start", "group_params": "x"},
    ],
    ids=["group-not-mapping", "param-without-key", "group_params-not-list"],
)
def test_malformed_template_internals_are_cli_errors(template) -> None:
    with pytest.raises(CliError) as excinfo:
        compile_spec({"name": "wf", "nodes": [{"id": "s", "type": "start"}]}, [template], [])
    assert "平台" in excinfo.value.message


def test_conditions_not_a_list_is_error() -> None:
    error = compile_error(_cond({"left": "a.b"}))
    assert error.details["issues"][0]["kind"] == "conditions_not_list"
