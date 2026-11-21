"""Compile a declaration into a platform workflow definition (AC-06).

``compile_spec(spec, templates, tools)`` is a pure function. Its inputs are the
loaded declaration (``spec.load_spec``, optionally after ``overlay.apply_overlay``),
the node templates the platform publishes (``GET /api/v2/flow/node_templates``)
and the tool list (``GET /api/v2/tools``, each child carrying ``node_template``).
It sends no request — fetching those two lists is the client layer's job.

For each declared node it deep-copies the template skeleton of that type, fills
the declared ``params`` **by key** (never by position), and wraps it in the
platform's node shape. Tool nodes take their skeleton from the tool list's
``node_template``; condition nodes compile their ``cases`` into the
``condition`` parameter. Edges get the platform's handle/id shape.

What the compiler does **not** do: invent defaults (an undeclared parameter keeps
whatever value the platform's template ships), or judge whether the result
would run — that is ``validate``. It does refuse what it cannot place: an
unknown node type, a parameter key the template does not have, a tool reference
that does not resolve. Those are collected and reported together.

Layout (``position``) and ``viewport`` are deterministic filler: the diff ignores
both (design decision 5), so the same declaration always compiles to the same
definition (AC-09).
"""

from __future__ import annotations

import copy
from typing import Any

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError

NODE_SHAPE = "flowNode"
EDGE_SHAPE = "customEdge"
DEFAULT_SOURCE_HANDLE = "right_handle"
TARGET_HANDLE = "left_handle"
TOOL_TYPE = "tool"
CONDITION_TYPE = "condition"
CONDITION_PARAM = "condition"
#: Placeholder the platform templates carry where the UI substitutes i18n copy.
_TEMPLATE_PLACEHOLDER = "true"
_DEFAULT_LOGIC = "and"
#: ``ConditionCases.evaluate_conditions``: ``"and"`` is AND, *anything else* is OR — so ``AND``
#: would silently become OR. Only the two exact spellings are accepted.
_LOGICS = ("and", "or")
_VIEWPORT = {"x": 0, "y": 0, "zoom": 1}
_LAYOUT_STEP_X = 320

#: Keys a declared node may carry (design §6.5). ``tool`` is for tool nodes,
#: ``cases`` for condition nodes; ``position`` lets a developer pin the layout.
NODE_KEYS = frozenset({"id", "type", "name", "description", "params", "tab", "tool", "cases", "position"})
EDGE_KEYS = frozenset({"from", "to", "handle"})
TOP_LEVEL_KEYS = frozenset({"name", "description", "nodes", "edges"})
#: Condition DSL keys. The platform's own names (``left_var`` / ``comparison_operation`` /
#: ``operator``) are refused rather than ignored — ignoring them compiled ``null``s that
#: ``ConditionOne`` rejects at graph build.
CASE_KEYS = frozenset({"id", "logic", "conditions"})
CONDITION_KEYS = frozenset({"id", "left", "op", "value", "ref", "label"})


def compile_spec(spec: dict[str, Any], templates: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Declaration → ``{nodes, edges, viewport}``. Raises ``CliError`` listing every problem."""
    by_type = _template_index(templates)
    catalogue = _tool_catalogue(tools)
    issues: list[dict[str, Any]] = []
    for key in sorted(set(spec) - TOP_LEVEL_KEYS, key=str):
        issues.append({"kind": "unknown_top_level_field", "field": key})
    # Display names, for the canvas-style condition labels (``<node name>/<var>``).
    declared_nodes = spec.get("nodes") if isinstance(spec.get("nodes"), list) else []
    declared_edges = spec.get("edges") if isinstance(spec.get("edges"), list) else []
    names = {
        n.get("id"): n.get("name") or n.get("id")
        for n in declared_nodes
        if isinstance(n, dict) and isinstance(n.get("id"), str) and n.get("id")
    }
    nodes = []
    for index, declared in enumerate(declared_nodes):
        node = _compile_node(declared, index, by_type, catalogue, names, issues)
        if node is not None:
            nodes.append(node)
    edges = [edge for edge in (_compile_edge(e, issues) for e in declared_edges) if edge is not None]
    if issues:
        raise CliError(
            "声明无法编译：" + "；".join(describe(issue) for issue in issues),
            exit_code=EXIT_LOCAL_INVALID,
            next_step="按提示修正声明文件后重试。",
            details={"issues": issues},
        )
    return {"nodes": nodes, "edges": edges, "viewport": dict(_VIEWPORT)}


# ---- nodes ---------------------------------------------------------------------


def _compile_node(
    declared: Any,
    index: int,
    by_type: dict[str, dict[str, Any]],
    catalogue: dict[str, list[tuple[str, dict[str, Any]]]],
    names: dict[Any, Any],
    issues: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(declared, dict):
        issues.append({"kind": "node_not_mapping", "index": index})
        return None
    node_id = declared.get("id")
    node_type = declared.get("type")
    if not isinstance(node_id, str) or not node_id:
        issues.append({"kind": "node_id_invalid", "index": index, "id": repr(node_id)})
        return None
    for key in sorted(set(declared) - NODE_KEYS):
        issues.append({"kind": "unknown_field", "node": node_id, "field": key})

    if not isinstance(node_type, str):
        issues.append({"kind": "unknown_type", "node": node_id, "type": node_type})
        return None
    if node_type == TOOL_TYPE:
        template = _resolve_tool(node_id, declared.get("tool"), catalogue, issues)
    elif node_type in by_type:
        template = by_type[node_type]
        if "tool" in declared:
            issues.append({"kind": "unknown_field", "node": node_id, "field": "tool"})
    else:
        issues.append({"kind": "unknown_type", "node": node_id, "type": node_type})
        return None
    if template is None:
        return None

    data = copy.deepcopy(template)
    data["id"] = node_id
    data["name"] = declared.get("name") or _real(data.get("name")) or node_id
    data["description"] = declared.get("description") or _real(data.get("description")) or ""

    if "tab" in declared:
        if isinstance(data.get("tab"), dict):
            data["tab"]["value"] = declared["tab"]
        else:
            issues.append({"kind": "tab_not_supported", "node": node_id, "type": node_type})

    params = declared.get("params") or {}
    if not isinstance(params, dict):
        issues.append({"kind": "params_not_mapping", "node": node_id})
        params = {}
    if node_type == CONDITION_TYPE and "cases" in declared:
        if CONDITION_PARAM in params:
            issues.append({"kind": "cases_conflict", "node": node_id})
        else:
            params = {**params, CONDITION_PARAM: _compile_cases(node_id, declared["cases"], names, issues)}
    elif "cases" in declared:
        issues.append({"kind": "unknown_field", "node": node_id, "field": "cases"})

    slots = {param["key"]: param for group in data.get("group_params") or [] for param in group.get("params") or []}
    for key, value in params.items():
        if key not in slots:
            issues.append({"kind": "unknown_param", "node": node_id, "param": key})
            continue
        if node_type == TOOL_TYPE and not isinstance(value, str):
            issues.append({"kind": "tool_param_not_string", "node": node_id, "param": key})
            continue
        slots[key]["value"] = copy.deepcopy(value)

    position = declared.get("position")
    if not (isinstance(position, dict) and {"x", "y"} <= set(position)):
        position = {"x": index * _LAYOUT_STEP_X, "y": 0}
    return {"id": node_id, "type": NODE_SHAPE, "position": {"x": position["x"], "y": position["y"]}, "data": data}


def _real(value: Any) -> Any:
    return None if value == _TEMPLATE_PLACEHOLDER else value


def _compile_cases(
    node_id: Any, cases: Any, names: dict[Any, Any], issues: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(cases, list):
        issues.append({"kind": "cases_not_list", "node": node_id})
        return []
    compiled = []
    for case in cases:
        if not isinstance(case, dict):
            issues.append({"kind": "case_not_mapping", "node": node_id})
            continue
        case_id = case.get("id")
        if "logic" in case and case["logic"] not in _LOGICS:
            issues.append(
                {"kind": "condition_bad_logic", "node": node_id, "case": case_id, "logic": repr(case["logic"])}
            )
        if not isinstance(case.get("conditions") or [], list):
            issues.append({"kind": "conditions_not_list", "node": node_id, "case": case_id})
            continue
        for key in sorted(set(case) - CASE_KEYS, key=str):
            issues.append({"kind": "unknown_case_field", "node": node_id, "case": case_id, "field": key})
        conditions = []
        for position, condition in enumerate(case.get("conditions") or []):
            if not isinstance(condition, dict):
                issues.append({"kind": "condition_not_mapping", "node": node_id, "case": case_id})
                continue
            for key in sorted(set(condition) - CONDITION_KEYS, key=str):
                issues.append({"kind": "unknown_condition_field", "node": node_id, "case": case_id, "field": key})
            for key in ("left", "op"):
                if not condition.get(key):
                    issues.append({"kind": "condition_missing_field", "node": node_id, "case": case_id, "field": key})
            if "value" in condition and "ref" in condition:
                issues.append({"kind": "condition_value_and_ref", "node": node_id, "case": case_id})
            is_ref = "ref" in condition
            conditions.append(
                {
                    # A declared id is kept (an online flow already has one, and the diff
                    # would otherwise report it); else stable and unique within the node —
                    # same declaration -> same id (AC-09).
                    "id": condition.get("id") or f"{case_id}_c{position}",
                    "left_var": condition.get("left"),
                    "left_label": condition.get("label") or _label(condition.get("left"), names),
                    "comparison_operation": condition.get("op"),
                    "right_value_type": "ref" if is_ref else "input",
                    "right_value": condition.get("ref") if is_ref else _as_text(condition.get("value")),
                    "right_label": _label(condition.get("ref"), names) if is_ref else "",
                }
            )
        compiled.append({"id": case_id, "operator": case.get("logic") or _DEFAULT_LOGIC, "conditions": conditions})
    return compiled


def _label(reference: Any, names: dict[Any, Any]) -> str:
    """Canvas display label for ``node.var``: ``<node display name>/<var>`` (display only)."""
    if not isinstance(reference, str) or "." not in reference:
        return reference or ""
    node, var = reference.split(".", 1)
    return f"{names[node]}/{var}" if node in names else reference


def _as_text(value: Any) -> str:
    # ``ConditionOne.right_value`` is a required ``str``: YAML hands us 3 for "3", and
    # ``null`` / an omitted value (``is_empty`` needs none) must become "", not None.
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# ---- tools ---------------------------------------------------------------------


def _template_index(templates: Any) -> dict[str, dict[str, Any]]:
    """Index the platform's node templates by type; a malformed list is a platform error, not a crash."""
    if not isinstance(templates, list):
        raise _platform_error("节点模板")
    by_type: dict[str, dict[str, Any]] = {}
    for template in templates:
        if not isinstance(template, dict) or not isinstance(template.get("type"), str):
            raise _platform_error("节点模板")
        if not _well_formed_groups(template):
            raise _platform_error("节点模板")
        by_type[template["type"]] = template
    return by_type


def _well_formed_groups(template: dict[str, Any]) -> bool:
    """``group_params`` is a list of ``{params: [{key: str, …}]}`` — what the compiler fills by key."""
    groups = template.get("group_params", [])
    if not isinstance(groups, list):
        return False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("params", []), list):
            return False
        if not all(isinstance(p, dict) and isinstance(p.get("key"), str) for p in group.get("params", [])):
            return False
    return True


def _platform_error(what: str) -> CliError:
    return CliError(
        f"平台返回的{what}格式无法识别",
        exit_code=EXIT_LOCAL_INVALID,
        next_step="确认平台版本与 CLI 匹配（平台可能过旧或过新）；加 --verbose 查看请求。",
    )


def _tool_catalogue(tools: Any) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """``operationId`` → [(qualified name, child)], plus each ``type/operationId`` → its single entry."""
    if not isinstance(tools, list):
        raise _platform_error("工具列表")
    catalogue: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for tool_type in tools:
        if not isinstance(tool_type, dict) or not isinstance(tool_type.get("children") or [], list):
            raise _platform_error("工具列表")
        for child in tool_type.get("children") or []:
            if not isinstance(child, dict):
                raise _platform_error("工具列表")
            qualified = f"{tool_type.get('name')}/{child.get('name')}"
            catalogue.setdefault(child.get("name"), []).append((qualified, child))
            catalogue.setdefault(qualified, []).append((qualified, child))
    return catalogue


def _resolve_tool(
    node_id: Any, reference: Any, catalogue: dict[str, list[tuple[str, dict[str, Any]]]], issues: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not isinstance(reference, str) or not reference:
        issues.append({"kind": "missing_tool", "node": node_id})
        return None
    matches = catalogue.get(reference) or []
    if not matches:
        issues.append({"kind": "unknown_tool", "node": node_id, "tool": reference})
        return None
    if len(matches) > 1:
        issues.append(
            {"kind": "ambiguous_tool", "node": node_id, "tool": reference, "candidates": [q for q, _ in matches]}
        )
        return None
    _, child = matches[0]
    template = child.get("node_template")
    if not isinstance(template, dict) or not template.get("tool_key") or not _well_formed_groups(template):
        issues.append({"kind": "tool_without_template", "node": node_id, "tool": reference})
        return None
    return template


# ---- edges ---------------------------------------------------------------------


def _compile_edge(declared: Any, issues: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(declared, dict):
        issues.append({"kind": "edge_not_mapping"})
        return None
    source, target = declared.get("from"), declared.get("to")
    for key in sorted(set(declared) - EDGE_KEYS):
        issues.append({"kind": "unknown_edge_field", "edge": {"from": source, "to": target}, "field": key})
    if not (isinstance(source, str) and source and isinstance(target, str) and target):
        issues.append({"kind": "edge_missing_endpoint", "edge": {"from": source, "to": target}})
        return None
    handle = declared.get("handle") or DEFAULT_SOURCE_HANDLE
    return {
        "id": f"xy-edge__{source}{handle}-{target}{TARGET_HANDLE}",
        "source": source,
        "sourceHandle": handle,
        "target": target,
        "targetHandle": TARGET_HANDLE,
        "animated": True,
        "type": EDGE_SHAPE,
    }


# ---- messages ------------------------------------------------------------------


def describe(issue: dict[str, Any]) -> str:
    kind = issue["kind"]
    node = issue.get("node")
    messages = {
        "node_not_mapping": lambda: f"第 {issue.get('index')} 个节点不是键值映射",
        "unknown_field": lambda: f"节点 {node} 有不认识的字段 {issue.get('field')}",
        "unknown_type": lambda: f"节点 {node} 的类型 {issue.get('type')} 不是平台的节点类型",
        "tab_not_supported": lambda: f"节点 {node}（{issue.get('type')}）没有 tab 可选",
        "params_not_mapping": lambda: f"节点 {node} 的 params 不是键值映射",
        "cases_conflict": lambda: f"条件节点 {node} 同时写了 cases 与 params.condition，只能二选一",
        "cases_not_list": lambda: f"条件节点 {node} 的 cases 不是列表",
        "case_not_mapping": lambda: f"条件节点 {node} 的某个 case 不是键值映射",
        "condition_not_mapping": lambda: f"条件节点 {node} 的 case {issue.get('case')} 里有条件不是键值映射",
        "unknown_param": lambda: f"节点 {node} 的模板里没有参数 {issue.get('param')}",
        "tool_param_not_string": lambda: f"工具节点 {node} 的参数 {issue.get('param')} 必须是字符串",
        "missing_tool": lambda: f"工具节点 {node} 没写 tool: <operationId>",
        "unknown_tool": lambda: f"工具节点 {node} 引用的 {issue.get('tool')} 不在已注册工具里",
        "ambiguous_tool": lambda: (
            f"工具节点 {node} 引用的 {issue.get('tool')} 对应多个工具："
            f"{'、'.join(issue.get('candidates') or [])}，请写成 <工具名>/<operationId>"
        ),
        "tool_without_template": lambda: f"工具 {issue.get('tool')} 没有节点模板（平台版本过旧？）",
        "edge_not_mapping": lambda: "某条连线不是键值映射",
        "unknown_edge_field": lambda: f"连线 {issue['edge']} 有不认识的字段 {issue.get('field')}",
        "edge_missing_endpoint": lambda: f"连线 {issue['edge']} 的 from / to 缺失或不是字符串",
        "node_id_invalid": lambda: f"第 {issue.get('index')} 个节点的 id {issue.get('id')} 必须是非空字符串",
        "unknown_top_level_field": lambda: f"声明顶层有不认识的字段 {issue.get('field')}",
        "unknown_case_field": lambda: (
            f"条件节点 {node} 分支 {issue.get('case')} 有不认识的字段 {issue.get('field')}（写 id / logic / conditions）"
        ),
        "unknown_condition_field": lambda: (
            f"条件节点 {node} 分支 {issue.get('case')} 的条件有不认识的字段 {issue.get('field')}"
            "（写 left / op / value 或 ref / id / label）"
        ),
        "condition_missing_field": lambda: f"条件节点 {node} 分支 {issue.get('case')} 的条件缺少 {issue.get('field')}",
        "condition_bad_logic": lambda: (
            f"条件节点 {node} 分支 {issue.get('case')} 的 logic {issue.get('logic')} 只能是 and 或 or（小写）"
        ),
        "conditions_not_list": lambda: f"条件节点 {node} 分支 {issue.get('case')} 的 conditions 必须是列表",
        "condition_value_and_ref": lambda: f"条件节点 {node} 分支 {issue.get('case')} 的条件同时写了 value 与 ref",
    }
    render = messages.get(kind)
    return render() if render else kind
