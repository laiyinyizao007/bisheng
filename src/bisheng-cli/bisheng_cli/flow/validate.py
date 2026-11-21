"""Compile-time structural checks on a workflow definition (design §4.4, AC-12 … AC-18).

The platform stores a definition after checking only that it is a dict with
``nodes``/``edges``. Structural mistakes then surface when the workflow *runs*
— or, for a mistyped variable reference, never: ``PromptTemplateParser`` leaves
an unmatched ``{{#…#}}`` in the prompt verbatim. Each rule below mirrors one
place where the engine would fail, and each report names where (node id,
parameter key, reference, or edge ends + handle).

``validate(definition, templates, tools)`` takes the compiled
``{nodes, edges, viewport}`` plus the same template and tool lists the compiler
used. The set of node types comes from the templates — the CLI keeps no list of
its own (design decision 2).

References: every workflow variable is resolved by ``GraphState.get_variable_by_str``,
which splits on ``.`` — so a prefix-less ``{{#histories#}}`` raises ``IndexError``
at runtime. (``prompt_template.py``'s docstring still lists three "special keys";
no workflow node honours them.) Rule 8 therefore requires ``<node>.<var>``.

The checks only report what is certain (AC-18, design §5 pit 21). Deliberately
**not** judged:

* ``sourceHandle`` on anything but condition nodes — the engine ignores the
  handle there; ``output`` in choose mode branches on option keys.
* Parameter *values* beyond shape — with one exception: ``model_id`` on llm /
  agent / rag must be non-empty (``BishengLLM`` refuses an empty one). Whether a
  model id or knowledge base still *exists* is a runtime matter.
* References outside templated parameters (``TEMPLATED_PARAM_TYPES``): ``code``
  is executed and ``report`` is a file key, so ``{{#…#}}`` there is data.
* ``note`` nodes — canvas annotations the engine skips entirely.
* A condition case with no outgoing edge — the engine routes a matched case to
  its own (empty) target list and stops there, without error; that may well be
  intended, so it is not reported.
* ``.get``-guarded parameters (``temperature``, ``output_user``, agent
  ``sql_agent`` …) — absent is legal.
* A condition case's ``operator`` / ``conditions`` — both optional; a case with
  no conditions is the platform's else branch.
* A lone ``{{#`` with no closing ``}}`` on the same line — that is prose (a prompt
  may warn the model about the marker); the engine leaves it alone too.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

TOOL_TYPE = "tool"
START_TYPE = "start"
END_TYPE = "end"
CONDITION_TYPE = "condition"
#: Canvas annotations: ``graph_engine.init_nodes`` skips them, so no rule applies.
NOTE_TYPE = "note"
DEFAULT_SOURCE_HANDLE = "right_handle"
SINGLE_TAB = "single"

#: ``workflow/nodes/prompt_template.py`` — the only reference form the engine replaces.
_REFERENCE = re.compile(r"\{\{#([#a-zA-Z_][a-zA-Z0-9_]{0,29}|[#a-zA-Z0-9_]{1,50}\.[#a-zA-Z0-9_\.]{1,100})#\}\}")
_REFERENCE_START = "{{#"
#: ``{{#`` + 50-char node + ``.`` + 100-char variable + ``#}}`` — the longest form the regex accepts.
_MAX_REFERENCE_SPAN = 3 + 50 + 1 + 100 + 3

#: Parameters each node type's engine class reads with ``node_params["…"]`` (design §4.4,
#: rule 9). A tuple entry means "one of these" (``output``: new ``output_msg`` or legacy
#: ``message``). Tool nodes need ``tool_key`` on ``data``, checked separately.
REQUIRED_PARAMS: dict[str, tuple[str | tuple[str, ...], ...]] = {
    "input": ("user_input",),
    "output": ("output_result", ("output_msg", "message")),
    "llm": ("system_prompt", "user_prompt", "model_id", "batch_variable", "output"),
    "agent": (
        "system_prompt",
        "user_prompt",
        "model_id",
        "chat_history_flag",
        "tool_list",
        "knowledge_id",
        "batch_variable",
    ),
    "code": ("code_input", "code", "code_output"),
    "qa_retriever": ("user_question",),
    "rag": ("system_prompt", "user_prompt", "model_id"),
    "knowledge_retriever": ("user_question",),
    "report": ("report_info",),
    "condition": ("condition",),
}
#: Parameters that must also be *filled*: ``BishengLLM.__init__`` raises
#: ``NoLlmModelConfigError`` on an empty ``model_id`` (llm / agent / rag all build one).
#: Nothing else is value-checked — an empty prompt is a legal empty prompt.
NON_EMPTY_PARAMS: dict[str, tuple[str, ...]] = {"llm": ("model_id",), "agent": ("model_id",), "rag": ("model_id",)}
#: Parameter types the engine runs through variable substitution (prompts, output
#: messages, tool arguments, input form labels). ``code`` / ``report`` / selectors are
#: never templated, so a ``{{#…#}}`` string there is data, not a reference.
TEMPLATED_PARAM_TYPES = frozenset({"var_textarea", "var_textarea_file", "form"})
#: Node types whose ``batch`` tab indexes ``output[i]["key"]`` for each ``batch_variable[i]``.
BATCH_TYPES = frozenset({"llm", "agent"})
#: ``workflow/common/condition.py`` ``ComparisonType``.
COMPARISON_OPERATORS = frozenset(
    {
        "equals",
        "not_equals",
        "greater_than",
        "less_than",
        "greater_than_or_equal",
        "less_than_or_equal",
        "contains",
        "not_contains",
        "is_empty",
        "is_not_empty",
        "starts_with",
        "ends_with",
        "regex",
    }
)
#: ``ConditionOne`` fields declared with ``Field(...)`` (no default) — all ``str``.
CONDITION_FIELDS = ("id", "left_var", "comparison_operation", "right_value_type", "right_value")


def validate(
    definition: dict[str, Any], templates: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """All structural issues in ``definition``; empty means it passed. Never raises on bad input."""
    issues: list[dict[str, Any]] = []
    if not isinstance(definition, dict):
        return [_issue(1, "definition_invalid", "工作流定义必须是含 nodes / edges 的键值映射")]
    raw_nodes = definition.get("nodes") if isinstance(definition.get("nodes"), list) else []
    raw_edges = definition.get("edges") if isinstance(definition.get("edges"), list) else []
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            issues.append(_issue(3, "node_not_mapping", f"第 {index} 个节点不是键值映射", index=index))
    nodes = [n for n in raw_nodes if isinstance(n, dict) and _data(n).get("type") != NOTE_TYPE]
    edges = [e for e in raw_edges if isinstance(e, dict)]
    known_types = {t.get("type") for t in templates if isinstance(t, dict) and _sid(t.get("type"))} | {TOOL_TYPE}
    tool_keys = {
        child.get("tool_key")
        for tool_type in tools
        if isinstance(tool_type, dict)
        for child in tool_type.get("children") or []
        if isinstance(child, dict) and _sid(child.get("tool_key"))
    }

    # 1 / 2
    if not nodes:
        issues.append(_issue(1, "no_nodes", "工作流至少需要一个节点"))
        return issues
    if not any(_data(n).get("type") == START_TYPE for n in nodes):
        issues.append(_issue(2, "missing_start", "工作流缺少起始节点（type: start）"))

    # 3
    counts = Counter(_sid(n.get("id")) for n in nodes if _sid(n.get("id")))
    for index, item in enumerate(nodes):
        if not _sid(item.get("id")):
            issues.append(_issue(3, "node_id_missing", f"第 {index} 个节点没有 id", index=index))
    for node_id, count in counts.items():
        if count > 1:
            issues.append(_issue(3, "node_id_duplicate", f"节点 id {node_id} 出现了 {count} 次", node=node_id))
    node_ids = set(counts)
    by_id = {_sid(n.get("id")): n for n in nodes if _sid(n.get("id"))}

    # 4
    for item in nodes:
        node_type = _data(item).get("type")
        if not isinstance(node_type, str) or node_type not in known_types:
            issues.append(
                _issue(
                    4,
                    "unknown_type",
                    f"节点 {item.get('id')} 的类型 {node_type} 不是平台的节点类型",
                    node=item.get("id"),
                    type=node_type,
                )
            )

    # 5
    touched = {_sid(e.get("source")) for e in edges} | {_sid(e.get("target")) for e in edges}
    for item in nodes:
        if _data(item).get("type") != END_TYPE and _sid(item.get("id")) and _sid(item.get("id")) not in touched:
            issues.append(_issue(5, "node_unconnected", f"节点 {item.get('id')} 没有任何连线", node=item.get("id")))

    # 6 / 7
    for edge in edges:
        where = _edge(edge)
        if _sid(edge.get("target")) not in node_ids:
            issues.append(_issue(6, "edge_target_missing", f"连线 {_edge_text(where)} 指向不存在的节点", edge=where))
        if _sid(edge.get("source")) not in node_ids:
            issues.append(_issue(6, "edge_source_missing", f"连线 {_edge_text(where)} 来自不存在的节点", edge=where))
        source = by_id.get(_sid(edge.get("source")))
        if source is not None and _data(source).get("type") == CONDITION_TYPE:
            allowed = _case_ids(source) | {DEFAULT_SOURCE_HANDLE}
            if _sid(where["sourceHandle"]) not in allowed:
                issues.append(
                    _issue(
                        7,
                        "edge_handle_unknown",
                        f"连线 {_edge_text(where)} 的来源句柄不是条件节点 {source.get('id')} 声明的分支",
                        edge=where,
                        allowed=sorted(allowed),
                    )
                )

    for item in nodes:
        node_id = item.get("id")
        data = _data(item)
        node_type = _sid(data.get("type"))  # a non-string type was already reported by rule 4
        params = _params(item)
        # 8
        for key, value in _templated_params(item, node_type).items():
            for text in _strings(value):
                issues += _check_references(text, node_id, key, node_ids)
        for key, ref in _variable_slots(node_type, params):
            issues += _check_variable(ref, node_id, key, node_ids)
        if node_type == CONDITION_TYPE:
            issues += _check_condition_variables(node_id, params.get("condition"), node_ids)
        # 9
        for required in REQUIRED_PARAMS.get(node_type, ()):
            alternatives = required if isinstance(required, tuple) else (required,)
            if not any(key in params for key in alternatives):
                name = "|".join(alternatives)
                issues.append(
                    _issue(9, "param_missing", f"节点 {node_id} 缺少必填参数 {name}", node=node_id, param=name)
                )
        for key in NON_EMPTY_PARAMS.get(node_type, ()):
            if key in params and params[key] in (None, "", [], {}):
                issues.append(_issue(9, "param_empty", f"节点 {node_id} 的 {key} 不能为空", node=node_id, param=key))
        if node_type == "output":
            message_key = "output_msg" if "output_msg" in params else "message"
            message = params.get(message_key)
            if message_key in params and not (isinstance(message, dict) and {"msg", "files"} <= set(message)):
                issues.append(
                    _issue(
                        9,
                        "param_shape",
                        f"节点 {node_id} 的 {message_key} 必须含 msg 与 files",
                        node=node_id,
                        param=message_key,
                    )
                )
        if node_type == "code":
            for key, fields in (("code_input", ("key", "type", "value")), ("code_output", ("key",))):
                if key in params and not _list_of_records(params[key], fields):
                    issues.append(
                        _issue(
                            9,
                            "param_shape",
                            f"节点 {node_id} 的 {key} 必须是含 {'/'.join(fields)} 的条目列表",
                            node=node_id,
                            param=key,
                        )
                    )
        if node_type == "output" and "output_result" in params:
            result = params["output_result"]
            if not (isinstance(result, dict) and {"type", "value"} <= set(result)):
                issues.append(
                    _issue(
                        9,
                        "param_shape",
                        f"节点 {node_id} 的 output_result 必须含 type 与 value",
                        node=node_id,
                        param="output_result",
                    )
                )
        if node_type == TOOL_TYPE and not data.get("tool_key"):
            issues.append(
                _issue(9, "param_missing", f"工具节点 {node_id} 缺少 tool_key", node=node_id, param="tool_key")
            )
        # 10
        if node_type in BATCH_TYPES and _tab_value(data) != SINGLE_TAB:
            issues += _check_batch(node_id, params.get("batch_variable"), params.get("output"))
        # 11
        if node_type == TOOL_TYPE and data.get("tool_key") and _sid(data.get("tool_key")) not in tool_keys:
            issues.append(
                _issue(
                    11,
                    "tool_unresolved",
                    f"工具节点 {node_id} 的 tool_key 不在已注册工具里",
                    node=node_id,
                    tool_key=data.get("tool_key"),
                )
            )
        # 12
        if node_type == CONDITION_TYPE and "condition" in params:
            issues += _check_condition(node_id, params["condition"])
    return issues


# ---- rule helpers ---------------------------------------------------------------------


def _check_references(text: str, node_id: Any, key: str, node_ids: set[Any]) -> list[dict[str, Any]]:
    issues = []
    position = text.find(_REFERENCE_START)
    while position != -1:
        match = _REFERENCE.match(text, position)
        if match is None:
            closing = _attempted_reference_end(text, position)
            if closing is not None:
                snippet = text[position:closing]
                issues.append(
                    _issue(
                        8,
                        "reference_malformed",
                        f"节点 {node_id} 的参数 {key} 里的引用 {snippet} 格式不被平台识别",
                        node=node_id,
                        param=key,
                        reference=snippet,
                    )
                )
            position = text.find(_REFERENCE_START, position + len(_REFERENCE_START))
            continue
        body = match.group(1)
        if "." not in body:
            issues.append(
                _issue(
                    8,
                    "reference_without_node",
                    f"节点 {node_id} 的参数 {key} 里的引用 {match.group(0)} 没有节点前缀——工作流里每个变量都按"
                    " <节点>.<变量> 解析，运行时会报错",
                    node=node_id,
                    param=key,
                    reference=match.group(0),
                )
            )
        elif body.split(".", 1)[0] not in node_ids:
            issues.append(
                _issue(
                    8,
                    "reference_unknown_node",
                    f"节点 {node_id} 的参数 {key} 引用了不存在的节点：{match.group(0)}",
                    node=node_id,
                    param=key,
                    reference=match.group(0),
                )
            )
        position = text.find(_REFERENCE_START, match.end())
    return issues


def _attempted_reference_end(text: str, start: int) -> int | None:
    """End of a ``{{#`` that *tries* to be a reference, or ``None`` if it is prose.

    It counts as an attempt only when a closing ``}}`` follows on the same line,
    before any further ``{{#`` and within the longest legal reference. A lone
    ``{{#`` — e.g. a prompt warning the model not to write that marker — is left
    alone by the engine as well, so flagging it would block a correct workflow
    (AC-18). The price: a half-typed reference with no closing braces at all is
    not reported.
    """
    limit = min(len(text), start + _MAX_REFERENCE_SPAN)
    newline = text.find("\n", start, limit)
    if newline != -1:
        limit = newline
    following = text.find(_REFERENCE_START, start + len(_REFERENCE_START), limit)
    if following != -1:
        limit = following
    closing = text.find("}}", start, limit)
    return None if closing == -1 else closing + 2


def _check_condition_variables(node_id: Any, cases: Any, node_ids: set[Any]) -> list[dict[str, Any]]:
    issues = []
    for case in cases if isinstance(cases, list) else []:
        conditions = case.get("conditions") if isinstance(case, dict) else None
        for condition in conditions if isinstance(conditions, list) else []:
            if not isinstance(condition, dict):
                continue
            refs = [condition.get("left_var")]
            if condition.get("right_value_type") == "ref":
                refs.append(condition.get("right_value"))
            for ref in refs:
                if isinstance(ref, str) and "." in ref and ref.split(".", 1)[0] not in node_ids:
                    issues.append(
                        _issue(
                            8,
                            "reference_unknown_node",
                            f"条件节点 {node_id} 引用了不存在的节点：{ref}",
                            node=node_id,
                            param="condition",
                            reference=ref,
                        )
                    )
    return issues


def _check_batch(node_id: Any, batch: Any, output: Any) -> list[dict[str, Any]]:
    batch = batch if isinstance(batch, list) else []
    output = output if isinstance(output, list) else []
    issues = []
    for index in range(len(batch)):
        entry = output[index] if index < len(output) else None
        if not (isinstance(entry, dict) and entry.get("key")):
            issues.append(
                _issue(
                    10,
                    "batch_output_mismatch",
                    f"节点 {node_id} 的第 {index} 个批量变量没有对应的 output[{index}].key",
                    node=node_id,
                    param="output",
                    index=index,
                )
            )
    return issues


def _check_condition(node_id: Any, cases: Any) -> list[dict[str, Any]]:
    if not isinstance(cases, list):
        return [
            _issue(
                12, "condition_invalid", f"条件节点 {node_id} 的 condition 必须是列表", node=node_id, field="condition"
            )
        ]
    issues = []
    for case_index, case in enumerate(cases):
        if not isinstance(case, dict) or not _sid(case.get("id")):
            issues.append(
                _issue(
                    12,
                    "condition_invalid",
                    f"条件节点 {node_id} 的第 {case_index} 个分支没有 id",
                    node=node_id,
                    field="id",
                    case=case_index,
                )
            )
            continue
        conditions = case.get("conditions")
        if conditions is None:
            continue
        if not isinstance(conditions, list):
            issues.append(
                _issue(
                    12,
                    "condition_invalid",
                    f"条件节点 {node_id} 分支 {case['id']} 的 conditions 必须是列表",
                    node=node_id,
                    field="conditions",
                    case=case["id"],
                )
            )
            continue
        for condition in conditions:
            if not isinstance(condition, dict):
                issues.append(
                    _issue(
                        12,
                        "condition_invalid",
                        f"条件节点 {node_id} 分支 {case['id']} 有条件不是键值映射",
                        node=node_id,
                        field="conditions",
                        case=case["id"],
                    )
                )
                continue
            for field in CONDITION_FIELDS:
                if not isinstance(condition.get(field), str):
                    issues.append(
                        _issue(
                            12,
                            "condition_invalid",
                            f"条件节点 {node_id} 分支 {case['id']} 的条件缺少 {field}（或不是字符串）",
                            node=node_id,
                            field=field,
                            case=case["id"],
                        )
                    )
            operator = condition.get("comparison_operation")
            if isinstance(operator, str) and operator not in COMPARISON_OPERATORS:
                issues.append(
                    _issue(
                        12,
                        "condition_invalid",
                        f"条件节点 {node_id} 分支 {case['id']} 的比较符 {operator} 不是平台支持的比较符",
                        node=node_id,
                        field="comparison_operation",
                        case=case["id"],
                    )
                )
    return issues


# ---- small helpers -------------------------------------------------------------------


def _variable_slots(node_type: Any, params: dict[str, Any]):
    """``(param, reference)`` pairs where a param holds a bare ``node.var`` the engine resolves."""
    if node_type in BATCH_TYPES and isinstance(params.get("image_prompt"), list):
        for entry in params["image_prompt"]:
            yield "image_prompt", entry
    if node_type == "code" and isinstance(params.get("code_input"), list):
        for entry in params["code_input"]:
            if isinstance(entry, dict) and entry.get("type") == "ref":
                yield "code_input", entry.get("value")


def _check_variable(ref: Any, node_id: Any, key: str, node_ids: set[Any]) -> list[dict[str, Any]]:
    if not isinstance(ref, str) or not ref:
        return [
            _issue(
                8,
                "reference_malformed",
                f"节点 {node_id} 的参数 {key} 里有变量引用不是字符串",
                node=node_id,
                param=key,
                reference=repr(ref),
            )
        ]
    if "." not in ref:
        return [
            _issue(
                8,
                "reference_without_node",
                f"节点 {node_id} 的参数 {key} 里的变量 {ref} 没有节点前缀",
                node=node_id,
                param=key,
                reference=ref,
            )
        ]
    if ref.split(".", 1)[0] not in node_ids:
        return [
            _issue(
                8,
                "reference_unknown_node",
                f"节点 {node_id} 的参数 {key} 引用了不存在的节点：{ref}",
                node=node_id,
                param=key,
                reference=ref,
            )
        ]
    return []


def _list_of_records(value: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(value, list) and all(isinstance(e, dict) and set(fields) <= set(e) for e in value)


def _sid(value: Any) -> str | None:
    """An id usable as a key: a non-empty string, else ``None`` (lists / dicts are unhashable)."""
    return value if isinstance(value, str) and value else None


def _issue(rule: int, code: str, message: str, **where: Any) -> dict[str, Any]:
    return {"rule": rule, "code": code, "message": message, **where}


def _data(node: dict[str, Any]) -> dict[str, Any]:
    data = node.get("data")
    return data if isinstance(data, dict) else {}


def _param_entries(node: dict[str, Any]):
    """Every well-formed ``{key, …}`` param of a node; malformed groups/params are skipped."""
    groups = _data(node).get("group_params")
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("params") if isinstance(group, dict) else None
        for param in entries if isinstance(entries, list) else []:
            if isinstance(param, dict) and isinstance(param.get("key"), str):
                yield param


def _params(node: dict[str, Any]) -> dict[str, Any]:
    return {param["key"]: param.get("value") for param in _param_entries(node)}


def _tab_value(data: dict[str, Any]) -> Any:
    tab = data.get("tab")
    return tab.get("value", SINGLE_TAB) if isinstance(tab, dict) else SINGLE_TAB


def _templated_params(node: dict[str, Any], node_type: Any = None) -> dict[str, Any]:
    """The params whose values the engine substitutes variables into (rule 8's scope).

    By template type, plus two engine-side cases the type does not reveal: a tool
    node templates every argument by name (``parse_tool_input``), and an output
    node templates ``output_result.value`` when it asks for input.
    """
    if node_type == TOOL_TYPE:
        return {p["key"]: p.get("value") for p in _param_entries(node) if p["key"] != "output"}
    templated = {p["key"]: p.get("value") for p in _param_entries(node) if p.get("type") in TEMPLATED_PARAM_TYPES}
    result = _params(node).get("output_result")
    if node_type == "output" and isinstance(result, dict) and result.get("type") == "input":
        templated["output_result"] = result.get("value")
    return templated


def _case_ids(node: dict[str, Any]) -> set[Any]:
    cases = _params(node).get("condition")
    return (
        {_sid(c.get("id")) for c in cases if isinstance(c, dict) and _sid(c.get("id"))}
        if isinstance(cases, list)
        else set()
    )


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _edge(edge: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": edge.get("source"),
        "sourceHandle": edge.get("sourceHandle") or DEFAULT_SOURCE_HANDLE,
        "target": edge.get("target"),
    }


def _edge_text(edge: dict[str, Any]) -> str:
    return f"{edge['source']} →({edge['sourceHandle']}) {edge['target']}"
