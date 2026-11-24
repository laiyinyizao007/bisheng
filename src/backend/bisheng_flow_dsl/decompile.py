"""Canvas → declaration: the inverse of ``compiler.compile_spec`` (F007 design decisions 2 and 12).

``decompile(canvas, templates, tools, name=..., description=...)`` returns
``(declaration, omitted)``. The contract is a round trip: compiling the declaration
gives a definition with no ``diff`` against the canvas. Whatever the declaration
language cannot carry is never dropped silently — it goes into ``omitted``:

==========================  =========================================================
``kind``                    meaning
==========================  =========================================================
``viewport``                the canvas camera differs from the compiler's default
``note``                    a canvas annotation (the language has no note type)
``unknown_node_type``       ``data.type`` is neither a template nor ``tool``
``tool_unresolved``         a tool node whose ``tool_key`` is not in the tool list
``unknown_field``           a ``data`` field that is neither in the template nor declarable
``unknown_param``           a parameter key the node's template does not have
``tool_param_not_string``   a tool parameter the language requires to be a string
``data_id_mismatch``        ``data.id`` differs from the canvas node id
``edge_to_omitted_node``    an edge whose end was omitted (else it would dangle)
==========================  =========================================================

Node ids and positions are kept, so a caller diffing the declaration's next
version against this canvas sees unchanged nodes as unchanged. Only parameters
whose value differs from the template default are written: the compiler fills
the rest from the same template.

A canvas older than the current templates may lack a parameter; nothing is
written for it and recompiling adds the template default — that parameter is
then the one difference left (design pitfall 8).
"""

from __future__ import annotations

import copy
import json
from typing import Any

from bisheng_flow_dsl.compiler import (
    _VIEWPORT,
    CONDITION_PARAM,
    CONDITION_TYPE,
    DEFAULT_SOURCE_HANDLE,
    TOOL_TYPE,
    _compile_cases,
    _label,
    _real,
    _template_index,
    _tool_catalogue,
)
from bisheng_flow_dsl.diff import NOTE_TYPE

#: ``data`` fields the declaration sets itself (``tool_key`` comes with the tool's template).
_DECLARED_DATA_FIELDS = frozenset({"id", "type", "name", "description", "tab", "group_params", "tool_key"})
_NOTE_SHAPE = "noteNode"


def decompile(
    canvas: dict[str, Any],
    templates: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    name: str,
    description: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    by_type = _template_index(templates)
    tool_templates = _tool_templates(tools)
    omitted: list[dict[str, Any]] = []

    viewport = canvas.get("viewport")
    if isinstance(viewport, dict) and _canon(viewport) != _canon(_VIEWPORT):
        omitted.append({"kind": "viewport"})

    kept: list[dict[str, Any]] = []
    dropped: set[str] = set()
    for node in canvas.get("nodes") or []:
        node_id = node.get("id")
        data = node.get("data") or {}
        node_type = data.get("type")
        if node_type == NOTE_TYPE or node.get("type") == _NOTE_SHAPE:
            omitted.append({"kind": "note", "node": node_id})
            dropped.add(node_id)
            continue
        if node_type == TOOL_TYPE:
            found = tool_templates.get(data.get("tool_key"))
            if found is None:
                omitted.append({"kind": "tool_unresolved", "node": node_id, "tool_key": data.get("tool_key")})
                dropped.add(node_id)
                continue
            reference, template = found
        elif node_type in by_type:
            reference, template = None, by_type[node_type]
        else:
            omitted.append({"kind": "unknown_node_type", "node": node_id, "type": node_type})
            dropped.add(node_id)
            continue
        kept.append({"node": node, "data": data, "type": node_type, "template": template, "tool": reference})

    names = {item["node"]["id"]: item["data"].get("name") or item["node"]["id"] for item in kept}
    nodes = [_node(item, names, omitted) for item in kept]

    edges = []
    for edge in canvas.get("edges") or []:
        ends = [end for end in (edge.get("source"), edge.get("target")) if end in dropped]
        if ends:
            omitted.append(
                {
                    "kind": "edge_to_omitted_node",
                    "node": ends[0],
                    "source": edge.get("source"),
                    "target": edge.get("target"),
                }
            )
            continue
        declared = {"from": edge.get("source"), "to": edge.get("target")}
        handle = edge.get("sourceHandle")
        if handle and handle != DEFAULT_SOURCE_HANDLE:
            declared["handle"] = handle
        edges.append(declared)

    declaration: dict[str, Any] = {"name": name}
    if description:
        declaration["description"] = description
    declaration["nodes"] = nodes
    declaration["edges"] = edges
    return declaration, omitted


def _node(item: dict[str, Any], names: dict[str, Any], omitted: list[dict[str, Any]]) -> dict[str, Any]:
    node, data, node_type, template = item["node"], item["data"], item["type"], item["template"]
    node_id = node["id"]
    declared: dict[str, Any] = {"id": node_id, "type": node_type}
    if item["tool"] is not None:
        declared["tool"] = item["tool"]

    if data.get("id", node_id) != node_id:
        omitted.append({"kind": "data_id_mismatch", "node": node_id, "data_id": data.get("id")})
    for field in sorted(set(data) - _DECLARED_DATA_FIELDS - set(template), key=str):
        omitted.append({"kind": "unknown_field", "node": node_id, "key": field})

    if data.get("name"):
        declared["name"] = data["name"]
    default_description = _real(template.get("description")) or ""
    if data.get("description") and data["description"] != default_description:
        declared["description"] = data["description"]

    tab, template_tab = data.get("tab"), template.get("tab")
    if isinstance(tab, dict) and isinstance(template_tab, dict) and tab.get("value") != template_tab.get("value"):
        declared["tab"] = tab.get("value")

    position = node.get("position")
    if isinstance(position, dict) and {"x", "y"} <= set(position):
        declared["position"] = {"x": position["x"], "y": position["y"]}

    defaults = _param_values(template)
    params: dict[str, Any] = {}
    for key, value in _param_values(data).items():
        if key not in defaults:
            omitted.append({"kind": "unknown_param", "node": node_id, "key": key})
            continue
        if _canon(value) == _canon(defaults[key]):
            continue
        if node_type == TOOL_TYPE and not isinstance(value, str):
            omitted.append({"kind": "tool_param_not_string", "node": node_id, "key": key})
            continue
        params[key] = copy.deepcopy(value)

    if node_type == CONDITION_TYPE and CONDITION_PARAM in params:
        cases = _cases(params[CONDITION_PARAM], names)
        if cases is not None:
            del params[CONDITION_PARAM]
            declared["cases"] = cases
    if params:
        declared["params"] = params
    return declared


def _cases(value: Any, names: dict[str, Any]) -> list[dict[str, Any]] | None:
    """The ``cases`` spelling of a condition value — or ``None`` if it would not compile back to it."""
    if not isinstance(value, list):
        return None
    cases = []
    for case in value:
        if not isinstance(case, dict):
            return None
        conditions = []
        for one in case.get("conditions") or []:
            if not isinstance(one, dict):
                return None
            condition: dict[str, Any] = {
                "id": one.get("id"),
                "left": one.get("left_var"),
                "op": one.get("comparison_operation"),
            }
            if one.get("right_value_type") == "ref":
                condition["ref"] = one.get("right_value")
            elif one.get("right_value") not in (None, ""):
                condition["value"] = one.get("right_value")
            if one.get("left_label") != _label(one.get("left_var"), names):
                condition["label"] = one.get("left_label")
            conditions.append(condition)
        declared_case: dict[str, Any] = {"id": case.get("id")}
        if case.get("operator") != "and":
            declared_case["logic"] = case.get("operator")
        declared_case["conditions"] = conditions
        cases.append(declared_case)
    # Only if compiling the cases reproduces the stored value exactly (stale labels,
    # extra keys, non-string values... all fall back to the raw parameter).
    issues: list[dict[str, Any]] = []
    compiled = _compile_cases("_", cases, names, issues)
    if issues or _canon(compiled) != _canon(value):
        return None
    return cases


def _param_values(data: dict[str, Any]) -> dict[str, Any]:
    """``key → value`` in the order the canvas shows them (the last one wins, as in the compiler)."""
    values: dict[str, Any] = {}
    for group in data.get("group_params") or []:
        for param in (group or {}).get("params") or []:
            if isinstance(param, dict) and isinstance(param.get("key"), str):
                values[param["key"]] = param.get("value")
    return values


def _tool_templates(tools: list[dict[str, Any]]) -> dict[Any, tuple[str, dict[str, Any]]]:
    """``tool_key → (reference, node_template)``; the reference is the shortest name that resolves uniquely."""
    catalogue = _tool_catalogue(tools)
    found: dict[Any, tuple[str, dict[str, Any]]] = {}
    for tool_type in tools:
        for child in tool_type.get("children") or []:
            template = child.get("node_template")
            if not isinstance(template, dict) or not template.get("tool_key"):
                continue
            short = child.get("name")
            reference = short if len(catalogue.get(short) or []) == 1 else f"{tool_type.get('name')}/{short}"
            found.setdefault(template["tool_key"], (reference, template))
    return found


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
