"""F005 T024 — compile-time structural checks (design §4.4, AC-12 … AC-18).

The platform barely validates a workflow definition (``Flow.data`` only has to
be a dict with ``nodes``/``edges``); structural mistakes surface when the
workflow *runs*, or — for variable references — never, because an unmatched
``{{#…#}}`` is silently left in the prompt. These checks close that gap, and
each one mirrors a specific place where the engine would fail.

Every rule has two tests: one where it must report, one where a valid
definition must pass. A check with only a positive test can be broken into
"always reports" or "never reports" without anything going red. Each report is
locatable — node id, parameter key, reference, or edge ends + handle.

Boundaries the checks keep (AC-18: nothing beyond what is certain):

* ``sourceHandle`` is only judged on condition nodes — other nodes ignore the
  handle at runtime (``output`` in choose mode branches on option keys, which
  is not a closed set here).
* Required parameters are *keys the engine indexes* (``node_params["x"]``);
  ``.get``-guarded ones (``sql_agent``, ``temperature`` …) are never required,
  and no value is judged for "is this model id still valid".
"""

from __future__ import annotations

import copy

import pytest

from bisheng_cli.flow.compiler import compile_spec
from bisheng_cli.flow.validate import validate
from tests.helpers.flow_fixtures import find_param, templates, tools

VALID_SPEC = {
    "name": "wf",
    "nodes": [
        {"id": "start_1", "type": "start"},
        {"id": "input_1", "type": "input"},
        {
            "id": "llm_1",
            "type": "llm",
            "params": {"model_id": 7, "system_prompt": "s", "user_prompt": "想法：{{#input_1.user_input#}}"},
        },
        {"id": "tool_1", "type": "tool", "tool": "submit_candidate", "params": {"source": "{{#llm_1.output#}}"}},
        {
            "id": "cond_1",
            "type": "condition",
            "cases": [{"id": "stop", "conditions": [{"left": "input_1.user_input", "op": "regex", "value": "^结束$"}]}],
        },
        {"id": "output_1", "type": "output", "params": {"output_result": {"type": "", "value": ""}}},
        {"id": "end_1", "type": "end"},
    ],
    "edges": [
        {"from": "start_1", "to": "input_1"},
        {"from": "input_1", "to": "cond_1"},
        {"from": "cond_1", "to": "end_1", "handle": "stop"},
        {"from": "cond_1", "to": "llm_1"},
        {"from": "llm_1", "to": "tool_1"},
        {"from": "tool_1", "to": "output_1"},
        {"from": "output_1", "to": "input_1"},
    ],
}


def valid():
    return compile_spec(copy.deepcopy(VALID_SPEC), templates(), tools())


def check(definition):
    return validate(definition, templates(), tools())


def node(definition, node_id):
    return next(n for n in definition["nodes"] if n["id"] == node_id)


def drop_param(definition, node_id, key):
    for group in node(definition, node_id)["data"]["group_params"]:
        group["params"] = [p for p in group["params"] if p["key"] != key]


def codes(issues):
    return [issue["code"] for issue in issues]


def test_valid_definition_passes() -> None:
    assert check(valid()) == []


# ---- 1. at least one node ---------------------------------------------------------


def test_reports_no_nodes() -> None:
    issues = check({"nodes": [], "edges": [], "viewport": {}})
    assert codes(issues) == ["no_nodes"]
    assert issues[0]["rule"] == 1


def test_accepts_valid_no_nodes() -> None:
    assert "no_nodes" not in codes(check(valid()))


# ---- 2. a start node --------------------------------------------------------------


def test_reports_missing_start() -> None:
    definition = valid()
    definition["nodes"] = [n for n in definition["nodes"] if n["id"] != "start_1"]
    definition["edges"] = [e for e in definition["edges"] if e["source"] != "start_1"]
    assert "missing_start" in codes(check(definition))


def test_accepts_valid_start() -> None:
    assert "missing_start" not in codes(check(valid()))


# ---- 3. node ids present and unique ---------------------------------------------------


def test_reports_empty_node_id() -> None:
    definition = valid()
    node(definition, "end_1")["id"] = ""
    issue = next(i for i in check(definition) if i["code"] == "node_id_missing")
    assert issue["index"] == 6


def test_reports_duplicate_node_id() -> None:
    definition = valid()
    definition["nodes"].append(copy.deepcopy(node(definition, "end_1")))
    issue = next(i for i in check(definition) if i["code"] == "node_id_duplicate")
    assert issue["node"] == "end_1"


def test_accepts_valid_node_ids() -> None:
    assert not {"node_id_missing", "node_id_duplicate"} & set(codes(check(valid())))


# ---- 4. node type is a platform type ------------------------------------------------


def test_reports_unknown_node_type() -> None:
    definition = valid()
    node(definition, "llm_1")["data"]["type"] = "llmm"
    issue = next(i for i in check(definition) if i["code"] == "unknown_type")
    assert (issue["node"], issue["type"]) == ("llm_1", "llmm")


def test_accepts_valid_node_types() -> None:
    """Every template type and ``tool`` are accepted; the list comes from the templates, not the CLI."""
    assert "unknown_type" not in codes(check(valid()))
    custom = [*templates(), {"type": "brand_new", "group_params": []}]
    definition = valid()
    node(definition, "llm_1")["data"] = {"id": "llm_1", "type": "brand_new", "group_params": []}
    assert "unknown_type" not in codes(validate(definition, custom, tools()))


# ---- 5. every non-end node has an edge ------------------------------------------------


def test_reports_unconnected_node() -> None:
    definition = valid()
    definition["nodes"].append(copy.deepcopy(node(definition, "llm_1")))
    definition["nodes"][-1]["id"] = definition["nodes"][-1]["data"]["id"] = "llm_orphan"
    issue = next(i for i in check(definition) if i["code"] == "node_unconnected")
    assert issue["node"] == "llm_orphan"


def test_accepts_valid_connections() -> None:
    """An ``end`` node with no edge is fine, and so is a node with only an incoming edge."""
    definition = valid()
    definition["nodes"].append(
        {"id": "end_2", "type": "flowNode", "position": {}, "data": {"id": "end_2", "type": "end", "group_params": []}}
    )
    definition["edges"] = [e for e in definition["edges"] if e["source"] != "output_1"]  # output_1: incoming only
    assert "node_unconnected" not in codes(check(definition))


# ---- 6. edge targets exist --------------------------------------------------------------


def test_reports_edge_to_missing_node() -> None:
    definition = valid()
    definition["edges"][0]["target"] = "ghost"
    issue = next(i for i in check(definition) if i["code"] == "edge_target_missing")
    assert issue["edge"] == {"source": "start_1", "sourceHandle": "right_handle", "target": "ghost"}


def test_accepts_valid_edge_targets() -> None:
    assert "edge_target_missing" not in codes(check(valid()))


# ---- 7. condition handles are declared branches ---------------------------------------


def test_reports_unknown_condition_handle() -> None:
    definition = valid()
    next(e for e in definition["edges"] if e["sourceHandle"] == "stop")["sourceHandle"] = "stopp"
    issue = next(i for i in check(definition) if i["code"] == "edge_handle_unknown")
    assert issue["edge"]["sourceHandle"] == "stopp"
    assert sorted(issue["allowed"]) == ["right_handle", "stop"]


def test_accepts_valid_handles() -> None:
    """Case ids and ``right_handle`` are fine; other node types are not judged on handles."""
    definition = valid()
    next(e for e in definition["edges"] if e["source"] == "llm_1")["sourceHandle"] = "anything"
    assert "edge_handle_unknown" not in codes(check(definition))


# ---- 8. variable references -----------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    ["{{#input_1.user_input}}", "{{#input 1.user_input#}}", "{{#" + "x" * 31 + "#}}", "{{#.user_input#}}"],
)
def test_reports_malformed_reference(prompt) -> None:
    definition = valid()
    find_param(node(definition, "llm_1"), "user_prompt")["value"] = f"前缀 {prompt} 后缀"
    issue = next(i for i in check(definition) if i["code"] == "reference_malformed")
    assert (issue["node"], issue["param"]) == ("llm_1", "user_prompt")
    assert issue["reference"].startswith("{{#")


def test_reports_reference_to_missing_node() -> None:
    definition = valid()
    find_param(node(definition, "llm_1"), "user_prompt")["value"] = "{{#ghost.output#}}"
    issue = next(i for i in check(definition) if i["code"] == "reference_unknown_node")
    assert (issue["node"], issue["param"], issue["reference"]) == ("llm_1", "user_prompt", "{{#ghost.output#}}")


def test_reports_condition_variable_to_missing_node() -> None:
    definition = valid()
    find_param(node(definition, "cond_1"), "condition")["value"][0]["conditions"][0]["left_var"] = "ghost.x"
    issue = next(i for i in check(definition) if i["code"] == "reference_unknown_node")
    assert issue["node"] == "cond_1" and issue["reference"] == "ghost.x"


def test_accepts_literal_reference_marker_in_prose() -> None:
    """An unclosed ``{{#`` is prose, not a reference — the engine leaves it alone too.

    Taken from a real, working prompt (invest ``build_v3``): it *warns* the model not
    to write that marker. Flagging it would block a correct workflow (AC-18).
    """
    definition = valid()
    find_param(node(definition, "llm_1"), "system_prompt")["value"] = (
        "6. 代码和注释里不要出现 `{{#` 字样（工作流会把它当变量标记改写）。\n7. 下一条规则"
    )
    assert "reference_malformed" not in codes(check(definition))


def test_accepts_valid_references() -> None:
    definition = valid()
    find_param(node(definition, "llm_1"), "user_prompt")["value"] = (
        "{{#input_1.user_input#}} {{#llm_1.output.a.b#}} {{#input_1.files#0#}} 纯文本 {{ 不是引用 }}"
    )
    assert not {"reference_malformed", "reference_unknown_node"} & set(codes(check(definition)))


# ---- 9. required parameters per node type ------------------------------------------------


@pytest.mark.parametrize(
    ("node_id", "key"),
    [("input_1", "user_input"), ("llm_1", "model_id"), ("llm_1", "system_prompt"), ("output_1", "output_result")],
)
def test_reports_missing_required_param(node_id, key) -> None:
    definition = valid()
    drop_param(definition, node_id, key)
    issue = next(i for i in check(definition) if i["code"] == "param_missing")
    assert (issue["node"], issue["param"]) == (node_id, key)


def test_reports_output_without_message_or_output_msg() -> None:
    definition = valid()
    drop_param(definition, "output_1", "message")
    issue = next(i for i in check(definition) if i["code"] == "param_missing")
    assert (issue["node"], issue["param"]) == ("output_1", "output_msg|message")


def test_reports_output_result_without_type_or_value() -> None:
    definition = valid()
    find_param(node(definition, "output_1"), "output_result")["value"] = {"type": ""}
    issue = next(i for i in check(definition) if i["code"] == "param_shape")
    assert (issue["node"], issue["param"]) == ("output_1", "output_result")


def test_reports_tool_node_without_tool_key() -> None:
    definition = valid()
    del node(definition, "tool_1")["data"]["tool_key"]
    issue = next(i for i in check(definition) if i["code"] == "param_missing")
    assert (issue["node"], issue["param"]) == ("tool_1", "tool_key")


def test_accepts_valid_required_params() -> None:
    """Template-complete nodes pass; ``output_msg`` alone satisfies output; agent ``sql_agent`` is never required."""
    assert "param_missing" not in codes(check(valid()))
    definition = valid()
    output = node(definition, "output_1")
    find_param(output, "message")["key"] = "output_msg"
    agent = compile_spec({"name": "x", "nodes": [{"id": "agent_1", "type": "agent"}]}, templates(), tools())["nodes"][0]
    drop_param({"nodes": [agent]}, "agent_1", "sql_agent")
    definition["nodes"].append(agent)
    definition["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "agent_1"})
    assert not {"param_missing", "param_shape"} & set(codes(check(definition)))


# ---- 10. batch_variable ↔ output -------------------------------------------------------


def _batch(definition, batch, output):
    llm = node(definition, "llm_1")
    llm["data"]["tab"]["value"] = "batch"
    find_param(llm, "batch_variable")["value"] = batch
    find_param(llm, "output")["value"] = output


def test_reports_batch_output_mismatch() -> None:
    definition = valid()
    _batch(definition, ["input_1.a", "input_1.b"], [{"key": "out_a"}])
    issue = next(i for i in check(definition) if i["code"] == "batch_output_mismatch")
    assert issue["node"] == "llm_1" and issue["index"] == 1


def test_reports_batch_output_without_key() -> None:
    definition = valid()
    _batch(definition, ["input_1.a"], [{"label": "x"}])
    assert "batch_output_mismatch" in codes(check(definition))


def test_accepts_valid_batch() -> None:
    """Paired lists pass; single mode never looks at the batch lists."""
    definition = valid()
    _batch(definition, ["input_1.a", "input_1.b"], [{"key": "a"}, {"key": "b"}])
    assert "batch_output_mismatch" not in codes(check(definition))
    single = valid()
    find_param(node(single, "llm_1"), "batch_variable")["value"] = ["input_1.a"]
    assert "batch_output_mismatch" not in codes(check(single))


# ---- 11. tool_key resolves ---------------------------------------------------------------


def test_reports_unresolved_tool_key() -> None:
    definition = valid()
    node(definition, "tool_1")["data"]["tool_key"] = "key_gone"
    issue = next(i for i in check(definition) if i["code"] == "tool_unresolved")
    assert (issue["node"], issue["tool_key"]) == ("tool_1", "key_gone")


def test_accepts_valid_tool_key() -> None:
    assert "tool_unresolved" not in codes(check(valid()))


# ---- 12. condition structure ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "where"),
    [
        (lambda v: v.clear() or v.append({"conditions": []}), "id"),
        (lambda v: v[0]["conditions"][0].pop("left_var"), "left_var"),
        (lambda v: v[0]["conditions"][0].pop("right_value"), "right_value"),
        (lambda v: v[0]["conditions"][0].__setitem__("comparison_operation", "eq"), "comparison_operation"),
    ],
    ids=["case-without-id", "no-left_var", "no-right_value", "unknown-operator"],
)
def test_reports_condition_structure(mutate, where) -> None:
    definition = valid()
    mutate(find_param(node(definition, "cond_1"), "condition")["value"])
    issue = next(i for i in check(definition) if i["code"] == "condition_invalid")
    assert issue["node"] == "cond_1" and issue["field"] == where


def test_reports_condition_not_a_list() -> None:
    definition = valid()
    find_param(node(definition, "cond_1"), "condition")["value"] = {"id": "x"}
    assert "condition_invalid" in codes(check(definition))


def test_accepts_valid_condition() -> None:
    """``operator`` and ``conditions`` are optional on a case — an empty case is the platform's else branch."""
    definition = valid()
    find_param(node(definition, "cond_1"), "condition")["value"].append({"id": "else_case"})
    assert "condition_invalid" not in codes(check(definition))


# ---- shape of the report ---------------------------------------------------------------


def test_every_issue_is_locatable_and_has_a_message() -> None:
    definition = valid()
    node(definition, "llm_1")["data"]["type"] = "llmm"
    definition["edges"][0]["target"] = "ghost"
    for issue in check(definition):
        assert issue["message"] and isinstance(issue["rule"], int)
        assert {"node", "edge", "index"} & set(issue) or issue["code"] in {"no_nodes", "missing_start"}


def test_validate_does_not_mutate_input() -> None:
    definition = valid()
    before = copy.deepcopy(definition)
    check(definition)
    assert definition == before


# ---- G8 round 1 -----------------------------------------------------------------


@pytest.mark.parametrize("node_type", ["llm", "agent", "rag"])
def test_reports_empty_model_id(node_type) -> None:
    """``BishengLLM.__init__`` raises ``NoLlmModelConfigError`` on an empty ``model_id``.

    The compiler materialises the template default (``""``), so key presence alone
    would never fire for a node declared without a model.
    """
    definition = valid()
    extra = compile_spec({"name": "x", "nodes": [{"id": "m", "type": node_type}]}, templates(), tools())["nodes"][0]
    definition["nodes"].append(extra)
    definition["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "m"})
    issue = next(i for i in check(definition) if i["code"] == "param_empty")
    assert (issue["node"], issue["param"]) == ("m", "model_id")


def test_accepts_filled_model_id_and_empty_prompt() -> None:
    """Only ``model_id`` must be non-empty — an empty prompt is a legal empty prompt."""
    definition = valid()
    find_param(node(definition, "llm_1"), "system_prompt")["value"] = ""
    assert "param_empty" not in codes(check(definition))


@pytest.mark.parametrize("field", ["left_var", "right_value", "comparison_operation", "id"])
def test_reports_condition_none_fields(field) -> None:
    """``ConditionOne`` fields are non-None strings, ``id`` included — ``None`` fails at graph build."""
    definition = valid()
    condition = find_param(node(definition, "cond_1"), "condition")["value"][0]["conditions"][0]
    condition[field] = None
    issue = next(i for i in check(definition) if i["code"] == "condition_invalid")
    assert issue["field"] == field


def test_reports_condition_without_id() -> None:
    definition = valid()
    del find_param(node(definition, "cond_1"), "condition")["value"][0]["conditions"][0]["id"]
    issue = next(i for i in check(definition) if i["code"] == "condition_invalid")
    assert issue["field"] == "id"


def test_reports_edge_from_missing_node() -> None:
    """A dangling source is a broken edge too (``parse_fan_in_node`` KeyErrors on fan-in)."""
    definition = valid()
    definition["edges"].append({"source": "ghost", "sourceHandle": "right_handle", "target": "output_1"})
    issue = next(i for i in check(definition) if i["code"] == "edge_source_missing")
    assert issue["edge"]["source"] == "ghost"


def test_literal_marker_in_untemplated_param_is_not_flagged() -> None:
    """``code.code`` is executed, never templated — a ``{{#x.y#}}`` string there is just code."""
    definition = valid()
    extra = compile_spec({"name": "x", "nodes": [{"id": "code_1", "type": "code"}]}, templates(), tools())["nodes"][0]
    find_param(extra, "code")["value"] = "s = '{{#ghost.value#}}'  # not a reference\nt = '{{#bad ref}}'"
    definition["nodes"].append(extra)
    definition["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "code_1"})
    assert not {"reference_malformed", "reference_unknown_node"} & set(codes(check(definition)))


@pytest.mark.parametrize(("node_id", "key"), [("output_1", "message"), ("tool_1", "source")])
def test_templated_params_are_still_scanned(node_id, key) -> None:
    definition = valid()
    param = find_param(node(definition, node_id), key)
    param["value"] = {"msg": "{{#ghost.x#}}", "files": []} if key == "message" else "{{#ghost.x#}}"
    issue = next(i for i in check(definition) if i["code"] == "reference_unknown_node")
    assert (issue["node"], issue["param"]) == (node_id, key)


def test_note_nodes_are_exempt() -> None:
    """Canvas notes are skipped by the engine (``init_nodes``): no type, wiring or param rules apply."""
    definition = valid()
    definition["nodes"].append({"id": "note_1", "type": "flowNode", "data": {"id": "note_1", "type": "note"}})
    assert check(definition) == []


@pytest.mark.parametrize("bad", [None, [], "x", {"nodes": [1, "a"]}])
def test_malformed_definition_is_reported_not_raised(bad) -> None:
    issues = check(bad)
    assert issues and all(issue["message"] for issue in issues)


def test_non_mapping_node_entry_is_reported() -> None:
    definition = valid()
    definition["nodes"].append("oops")
    issue = next(i for i in check(definition) if i["code"] == "node_not_mapping")
    assert issue["index"] == len(definition["nodes"]) - 1


@pytest.mark.parametrize("bad_id", [["a"], {"x": 1}, 3])
def test_non_string_node_id_is_reported_not_raised(bad_id) -> None:
    definition = valid()
    definition["nodes"].append({"id": bad_id, "type": "flowNode", "data": {"type": "end", "group_params": []}})
    issue = next(i for i in check(definition) if i["code"] == "node_id_missing")
    assert issue["index"] == len(definition["nodes"]) - 1


@pytest.mark.parametrize("end", ["source", "target"])
def test_unhashable_edge_end_is_reported_not_raised(end) -> None:
    definition = valid()
    edge = {"source": "llm_1", "sourceHandle": "right_handle", "target": "tool_1"}
    edge[end] = ["llm_1"]
    definition["edges"].append(edge)
    assert f"edge_{end}_missing" in codes(check(definition))


# ---- G8 round 2 ------------------------------------------------------------------


@pytest.mark.parametrize("ref", ["{{#histories#}}", "{{#query#}}", "{{#context#}}", "{{#anything#}}"])
def test_reports_single_key_reference(ref) -> None:
    """Every workflow variable resolves through ``get_variable_by_str``, which splits on ``.``:
    a prefix-less key raises ``IndexError`` at runtime (verified against the engine)."""
    definition = valid()
    find_param(node(definition, "llm_1"), "user_prompt")["value"] = f"x {ref} y"
    issue = next(i for i in check(definition) if i["code"] == "reference_without_node")
    assert (issue["node"], issue["param"], issue["reference"]) == ("llm_1", "user_prompt", ref)


@pytest.mark.parametrize("value", ["hi", {"msg": "hi"}, {"files": []}, None])
def test_reports_output_message_shape(value) -> None:
    definition = valid()
    find_param(node(definition, "output_1"), "message")["value"] = value
    issue = next(i for i in check(definition) if i["code"] == "param_shape")
    assert (issue["node"], issue["param"]) == ("output_1", "message")


@pytest.mark.parametrize(
    ("key", "value"), [("code_output", "x"), ("code_output", [{"type": "str"}]), ("code_input", "x")]
)
def test_reports_code_io_shape(key, value) -> None:
    definition = valid()
    extra = compile_spec({"name": "x", "nodes": [{"id": "code_1", "type": "code"}]}, templates(), tools())["nodes"][0]
    find_param(extra, key)["value"] = value
    definition["nodes"].append(extra)
    definition["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "code_1"})
    issue = next(i for i in check(definition) if i["code"] == "param_shape")
    assert (issue["node"], issue["param"]) == ("code_1", key)


def test_accepts_template_shaped_code_and_output() -> None:
    definition = valid()
    extra = compile_spec({"name": "x", "nodes": [{"id": "code_1", "type": "code"}]}, templates(), tools())["nodes"][0]
    definition["nodes"].append(extra)
    definition["edges"].append({"source": "llm_1", "sourceHandle": "right_handle", "target": "code_1"})
    assert "param_shape" not in codes(check(definition))


@pytest.mark.parametrize("case_id", [123, ["k"], True])
def test_reports_non_string_case_id(case_id) -> None:
    definition = valid()
    find_param(node(definition, "cond_1"), "condition")["value"][0]["id"] = case_id
    issue = next(i for i in check(definition) if i["code"] == "condition_invalid")
    assert issue["field"] == "id"


def test_reports_reference_in_interactive_output_value() -> None:
    """``output_result.value`` is templated when ``type == "input"`` (``output.py``)."""
    definition = valid()
    find_param(node(definition, "output_1"), "output_result")["value"] = {"type": "input", "value": "{{#ghost.x#}}"}
    issue = next(i for i in check(definition) if i["code"] == "reference_unknown_node")
    assert (issue["node"], issue["param"]) == ("output_1", "output_result")


def test_output_value_not_scanned_when_not_interactive() -> None:
    definition = valid()
    find_param(node(definition, "output_1"), "output_result")["value"] = {"type": "choose", "value": "{{#ghost.x#}}"}
    assert "reference_unknown_node" not in codes(check(definition))


def test_tool_params_scanned_regardless_of_type() -> None:
    """The engine templates every tool argument by name (``parse_tool_input``)."""
    definition = valid()
    param = find_param(node(definition, "tool_1"), "source")
    param.pop("type")
    param["value"] = "{{#ghost.x#}}"
    issue = next(i for i in check(definition) if i["code"] == "reference_unknown_node")
    assert (issue["node"], issue["param"]) == ("tool_1", "source")


@pytest.mark.parametrize("entry", ["no_dot", "ghost.files"])
def test_reports_bad_image_prompt_entry(entry) -> None:
    definition = valid()
    find_param(node(definition, "llm_1"), "image_prompt")["value"] = [entry]
    issue = next(i for i in check(definition) if i["param"] == "image_prompt" and i["rule"] == 8)
    assert issue["node"] == "llm_1"


def test_accepts_valid_image_prompt() -> None:
    definition = valid()
    find_param(node(definition, "llm_1"), "image_prompt")["value"] = ["input_1.dialog_image_files"]
    assert not [i for i in check(definition) if i.get("param") == "image_prompt"]


def test_unhashable_platform_template_type_does_not_raise() -> None:
    weird = [*templates(), {"type": ["x"], "group_params": []}, "not-a-template"]
    assert validate(valid(), weird, tools()) == []
