"""Structural diff between a local definition and an online one (AC-19 – AC-23).

``diff(local, online)`` compares what changes how a workflow runs and nothing
else — design decision 5's comparison table, implemented as written:

=====================  ===============================================  ==========================================
compared               key                                              ignored
=====================  ===============================================  ==========================================
nodes                  ``node.id``                                      node order
node parameters        ``data.group_params[].params[].key`` → ``value``  ``label`` / ``desc`` / ``type`` / ``required``
                                                                        / ``placeholder`` / ``options`` / ``help``,
                                                                        group names, grouping, order
other node fields      ``data.type`` / ``name`` / ``description`` /      ``data.v``
                       ``tab`` (its ``value``) / ``tool_key``
edges                  ``(source, sourceHandle, target)``, as a          ``id`` / ``animated`` / ``type`` /
                       multiset                                          ``targetHandle``, edge order
top level              —                                                ``viewport``, node ``position`` and any
                                                                        other canvas state
=====================  ===============================================  ==========================================

Parameters are compared **by key, never by position**: adding one parameter
shifts every group after it, and a positional diff would report an untouched
workflow as entirely rewritten. Values are compared as canonical JSON, so
``7`` / ``"7"`` and ``True`` / ``1`` differ (the engine may treat them
differently) while dict key order does not.

The same structure feeds ``render`` — the human text is built from the
machine-readable diff and nothing else, so the two cannot disagree (AC-23).

A side that is not shaped like a definition is refused, not half-compared:
online it is a platform defect (``EXIT_DEFECT``), locally it is the
declaration's problem (``EXIT_LOCAL_INVALID``). A diff that silently skipped a
node it could not read would report "identical" for a workflow it never saw.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from bisheng_cli.errors import EXIT_DEFECT, EXIT_LOCAL_INVALID, CliError

#: Node ``data`` fields with meaning beyond the parameters (decision 5).
#: ``type`` is in on the "new fields are included" rule: same id, different node.
#: ``id`` is ``data.id`` — the key the engine builds its graph on (``graph_engine.init_nodes``),
#: which is not necessarily the canvas-level ``node.id`` this diff indexes by.
NODE_FIELDS = ("id", "type", "name", "description", "tab", "tool_key")
#: ``data.type`` of canvas annotations. The engine skips them (``graph_engine.init_nodes``)
#: and the declaration format has no note type, so they are reported apart from the
#: execution diff and never make two definitions "different".
NOTE_TYPE = "note"
SECTIONS = ("nodes_only_online", "nodes_only_local", "nodes_changed", "edges_only_online", "edges_only_local")
#: Longest value shown in the human rendering; the full value is always in ``--json``.
_SHOW_LIMIT = 200


def diff(local: Any, online: Any) -> dict[str, Any]:
    """The execution diff (``SECTIONS`` + ``identical``) plus ``notes_only_online`` / ``notes_only_local``.

    Notes are informational: they never set ``identical`` to false and are not
    counted, but a caller about to replace the online version can still say which
    annotations the new version will not carry.
    """
    local_nodes, local_edges, raw = _index(local, "local")
    online_nodes, online_edges, online_raw = _index(online, "online")
    raw = {**online_raw, **raw}
    local_notes, online_notes = _split_notes(local_nodes), _split_notes(online_nodes)

    only_online = [_summary(online_nodes[i]) for i in sorted(online_nodes.keys() - local_nodes.keys())]
    only_local = [_summary(local_nodes[i]) for i in sorted(local_nodes.keys() - online_nodes.keys())]
    changed = []
    for node_id in sorted(local_nodes.keys() & online_nodes.keys()):
        entry = _compare_node(node_id, local_nodes[node_id], online_nodes[node_id])
        if entry is not None:
            changed.append(entry)

    edges_online = _edge_delta(online_edges, local_edges, raw)
    edges_local = _edge_delta(local_edges, online_edges, raw)

    result: dict[str, Any] = {
        "nodes_only_online": only_online,
        "nodes_only_local": only_local,
        "nodes_changed": changed,
        "edges_only_online": edges_online,
        "edges_only_local": edges_local,
    }
    result["identical"] = not any(result[s] for s in SECTIONS)
    result["notes_only_online"] = [_summary(online_notes[i]) for i in sorted(online_notes.keys() - local_notes.keys())]
    result["notes_only_local"] = [_summary(local_notes[i]) for i in sorted(local_notes.keys() - online_notes.keys())]
    return result


def _split_notes(nodes: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Remove the annotation nodes from ``nodes`` (in place) and return them."""
    notes = {i: n for i, n in nodes.items() if n["data"].get("type") == NOTE_TYPE}
    for node_id in notes:
        del nodes[node_id]
    return notes


def count(result: dict[str, Any]) -> int:
    """How many items the diff holds — one per node, edge, field or parameter."""
    per_node = sum(len(n["fields"]) + len(n["params"]) for n in result["nodes_changed"])
    return per_node + sum(len(result[s]) for s in SECTIONS if s != "nodes_changed")


def online_only_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """What the online side has that the local side would replace (AC-26's "线上独有内容").

    Not ``count()``: that also counts what exists only *locally*, which a push adds
    rather than loses. This is the list a push must show (and have confirmed)
    before the new version stops carrying it: online-only nodes and edges, and
    every online value — parameter or node field — that the local side does not
    reproduce. Notes are not here; they are reported apart (``notes_only_online``).
    """
    items: list[dict[str, Any]] = [{"kind": "node", **n} for n in result["nodes_only_online"]]
    for node in result["nodes_changed"]:
        for field in node["fields"]:
            items.append({"kind": "field", "node": node["id"], "field": field["field"], "online": field["online"]})
        for param in node["params"]:
            if param.get("only") != "local":
                items.append({"kind": "param", "node": node["id"], "key": param["key"], "online": param["online"]})
    items += [{"kind": "edge", **e} for e in result["edges_only_online"]]
    return items


def render(result: dict[str, Any]) -> list[str]:
    """The human rendering of ``diff``'s output: a summary line, then one line per item."""
    notes = _note_lines(result)
    if result["identical"]:
        return ["本地定义与线上一致（不计画布坐标与视图）", *notes]
    lines = [f"本地与线上有 {count(result)} 处差异（+ 只在线上，- 只在本地，~ 两边不同）："]
    if result["nodes_only_online"]:
        lines.append("线上独有的节点：")
        lines += [f"  + {_node_line(n)}" for n in result["nodes_only_online"]]
    if result["nodes_only_local"]:
        lines.append("本地独有的节点：")
        lines += [f"  - {_node_line(n)}" for n in result["nodes_only_local"]]
    if result["nodes_changed"]:
        lines.append("两边不同的节点：")
        for node in result["nodes_changed"]:
            for field in node["fields"]:
                lines.append(f"  ~ {node['id']} 字段 {field['field']}：{_pair(field['online'], field['local'])}")
            for param in node["params"]:
                lines.append(f"  ~ {node['id']} 参数 {param['key']}：{_param_text(param)}")
    if result["edges_only_online"]:
        lines.append("线上独有的连线：")
        lines += [f"  + {_edge_line(e)}" for e in result["edges_only_online"]]
    if result["edges_only_local"]:
        lines.append("本地独有的连线：")
        lines += [f"  - {_edge_line(e)}" for e in result["edges_only_local"]]
    return lines + notes


def _note_lines(result: dict[str, Any]) -> list[str]:
    """Annotations, said apart from the diff: not indented as items and not counted."""
    lines = []
    for key, where in (("notes_only_online", "线上"), ("notes_only_local", "本地")):
        ids = [n["id"] for n in result.get(key) or []]
        if ids:
            lines.append(f"提示：{where}有 {len(ids)} 个批注节点（不参与执行，不计入差异）：{'、'.join(ids)}")
    return lines


# ---- comparison ----------------------------------------------------------------


def _compare_node(node_id: str, local: dict[str, Any], online: dict[str, Any]) -> dict[str, Any] | None:
    local_data, online_data = local["data"], online["data"]
    fields = []
    for field in NODE_FIELDS:
        if field not in local_data and field not in online_data:
            continue
        mine, theirs = _field(local_data, field), _field(online_data, field)
        if _canon(mine) != _canon(theirs):
            fields.append({"field": field, "online": theirs, "local": mine})

    local_params, online_params = local["params"], online["params"]
    params = []
    for key in sorted(local_params.keys() | online_params.keys()):
        if key not in local_params:
            params.append({"key": key, "online": _one(online_params[key]), "only": "online"})
        elif key not in online_params:
            params.append({"key": key, "local": _one(local_params[key]), "only": "local"})
        elif _canon(local_params[key]) != _canon(online_params[key]):
            params.append({"key": key, "online": _one(online_params[key]), "local": _one(local_params[key])})

    if not fields and not params:
        return None
    return {"id": node_id, "fields": fields, "params": params}


def _field(data: dict[str, Any], field: str) -> Any:
    value = data.get(field)
    if field == "tab" and isinstance(value, dict):
        # ``options`` is template metadata; the selected tab is the fact.
        return value.get("value")
    return value


def _one(values: list[Any]) -> Any:
    """A parameter's value — or every value, if the key occurs more than once in the node."""
    return values[0] if len(values) == 1 else values


def _edge_delta(
    mine: Counter, theirs: Counter, raw: dict[tuple[str, str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    delta = mine - theirs
    rows = []
    for key in sorted(delta):
        rows += [dict(raw[key]) for _ in range(delta[key])]
    return rows


# ---- indexing and shape checks ---------------------------------------------------


def _index(definition: Any, side: str) -> tuple[dict[str, dict[str, Any]], Counter, dict[tuple[str, str, str], dict]]:
    """Nodes by id, edges as a multiset of canonical keys, and each key's raw ``{source, sourceHandle, target}``."""
    if not isinstance(definition, dict) or not isinstance(definition.get("nodes"), list):
        raise _shape(side, "缺少节点列表 nodes")
    edges = definition.get("edges")
    if edges is None:
        edges = []
    if not isinstance(edges, list):
        raise _shape(side, "连线 edges 不是列表")

    nodes: dict[str, dict[str, Any]] = {}
    for position, node in enumerate(definition["nodes"]):
        if not isinstance(node, dict):
            raise _shape(side, f"第 {position + 1} 个节点不是对象")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise _shape(side, f"第 {position + 1} 个节点没有 id")
        if node_id in nodes:
            raise _shape(side, f"节点 id {node_id} 出现了不止一次")
        data = node.get("data")
        if not isinstance(data, dict):
            raise _shape(side, f"节点 {node_id} 的 data 不是对象")
        _finite(data, side, f"节点 {node_id}")
        nodes[node_id] = {"id": node_id, "data": data, "params": _params(data, node_id, side)}

    counted: Counter = Counter()
    raw_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for position, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise _shape(side, f"第 {position + 1} 条连线不是对象")
        raw = {"source": edge.get("source"), "sourceHandle": edge.get("sourceHandle"), "target": edge.get("target")}
        _finite(raw, side, f"第 {position + 1} 条连线")
        key = (_canon(raw["source"]), _canon(raw["sourceHandle"]), _canon(raw["target"]))
        raw_by_key.setdefault(key, raw)
        counted[key] += 1
    return nodes, counted, raw_by_key


def _params(data: dict[str, Any], node_id: str, side: str) -> dict[str, list[Any]]:
    # ``BaseNodeData.group_params`` is Optional: ``null`` is a node with no parameters.
    groups = data.get("group_params") or []
    if not isinstance(groups, list):
        raise _shape(side, f"节点 {node_id} 的 group_params 不是列表")
    params: dict[str, list[Any]] = {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("params", []), list):
            raise _shape(side, f"节点 {node_id} 的参数分组形状不对")
        for param in group.get("params", []):
            if not isinstance(param, dict) or not isinstance(param.get("key"), str):
                raise _shape(side, f"节点 {node_id} 有参数没有字符串 key")
            params.setdefault(param["key"], []).append(param.get("value"))
    return params


def _finite(value: Any, side: str, where: str) -> None:
    """NaN / Infinity cannot be written as JSON (RFC 8259), and the diff is written as JSON."""
    try:
        json.dumps(value, allow_nan=False, default=repr)
    except ValueError:
        raise _shape(side, f"{where} 含 NaN 或 Infinity，无法以合法 JSON 比较与输出") from None
    except TypeError:
        pass  # non-string dict keys: handled by ``_canon``'s repr fallback


def _shape(side: str, reason: str) -> CliError:
    if side == "online":
        return CliError(
            f"线上定义形状不对，无法比较：{reason}",
            exit_code=EXIT_DEFECT,
            next_step="线上数据不是平台画布会产生的形状：请带上工作流 id 与本条输出报平台故障；不要据此 push。",
            details={"side": side, "reason": reason},
        )
    return CliError(
        f"本地定义形状不对，无法比较：{reason}",
        exit_code=EXIT_LOCAL_INVALID,
        next_step="先用 bisheng flow compile 确认声明能编译通过，再做 diff。",
        details={"side": side, "reason": reason},
    )


# ---- canonical values and display --------------------------------------------------


def _canon(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)
    except TypeError:
        # Mixed-type dict keys (possible from YAML) cannot be sorted; fall back to a
        # stable repr rather than crash. It can only make two values *differ*.
        return repr(value)


#: Characters of common prefix kept before the first difference of two long values.
_SHOW_CONTEXT = 40


def _text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=repr)
    except TypeError:
        return repr(value)


def _show(value: Any) -> str:
    text = _text(value)
    if len(text) > _SHOW_LIMIT:
        return f"{text[:_SHOW_LIMIT]}…（共 {len(text)} 字符，完整值见 --json）"
    return text


def _pair(online: Any, local: Any) -> str:
    """Both sides of a changed value; long ones are cut *around the first difference*.

    Cutting both at the start would show two identical prefixes of a long prompt
    and hide the sentence that actually changed.
    """
    shown_a, shown_b = _text(online), _text(local)
    if len(shown_a) <= _SHOW_LIMIT and len(shown_b) <= _SHOW_LIMIT:
        return f"线上 {shown_a}，本地 {shown_b}"
    # Two strings are located in their own characters (what a person counts in the
    # prompt), then escaped; anything else is located in its JSON text.
    both_text = isinstance(online, str) and isinstance(local, str)
    a, b = (online, local) if both_text else (shown_a, shown_b)
    first = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b)))
    start = max(0, first - _SHOW_CONTEXT)

    def cut(text: str) -> str:
        piece = text[start : start + _SHOW_LIMIT]
        shown = json.dumps(piece, ensure_ascii=False) if both_text else piece
        return ("…" if start else "") + shown + ("…" if start + _SHOW_LIMIT < len(text) else "")

    return f"线上 {cut(a)}，本地 {cut(b)}（第 {first + 1} 个字符起不同；共 {len(a)} / {len(b)} 字符，完整值见 --json）"


def _param_text(param: dict[str, Any]) -> str:
    only = param.get("only")
    if only == "online":
        return f"只在线上，值 {_show(param['online'])}"
    if only == "local":
        return f"只在本地，值 {_show(param['local'])}"
    return _pair(param["online"], param["local"])


def _summary(node: dict[str, Any]) -> dict[str, Any]:
    return {"id": node["id"], "type": node["data"].get("type"), "name": node["data"].get("name")}


def _node_line(node: dict[str, Any]) -> str:
    return f"{node['id']}（类型 {node['type']}，名称 {node['name']}）"


def _edge_line(edge: dict[str, Any]) -> str:
    return f"{edge['source']} —[{edge['sourceHandle']}]→ {edge['target']}"
