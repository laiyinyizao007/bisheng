"""Apply = the canvas the user sent plus the diff to the proposal (F007 design decisions 3 and 8).

``merge(sent, proposal)`` never replaces the canvas wholesale: the declaration
language cannot carry notes, the viewport or fields it does not know, and a
wholesale replacement would drop them every time (AC-08). Instead it walks
``diff(local=proposal, online=sent)`` and lands only what that diff lists:

- a node only in the proposal is added (and laid out, decision 8);
- a node only in the sent canvas is removed — notes never are (the diff keeps
  them apart anyway);
- a changed node keeps its sent object (position, React Flow state, fields the
  declaration cannot carry) and gets only the listed fields and parameter values.
  When that cannot be done faithfully — the type or tool changed, a parameter
  exists on one side only, a repeated key occurs a different number of times —
  the node's ``data`` is taken from the proposal (sent-only ``data`` fields still
  kept) and the node is reported in ``replaced``;
- edges: the sent ones minus those the diff removes, plus the proposal's new ones.

The diff returned is recomputed from the merged canvas, so the preview lists
exactly what "apply" lands (AC-17 / AC-18). Neither input is mutated.
"""

from __future__ import annotations

import copy
import json
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from bisheng_flow_dsl.diff import NOTE_TYPE, diff

#: Fallback node footprint when the canvas does not carry React Flow's ``measured`` size.
DEFAULT_NODE_SIZE = (360, 240)
_GAP_X = 120
_GAP_Y = 60
_START_TYPE = "start"
#: Fields whose change means "a different node", not "the same node, edited".
_IDENTITY_FIELDS = frozenset({"type", "tool_key", "id"})


@dataclass
class MergeResult:
    canvas: dict[str, Any]
    diff: dict[str, Any]
    replaced: list[str] = field(default_factory=list)


def merge(sent: dict[str, Any], proposal: dict[str, Any]) -> MergeResult:
    planned = diff(proposal, sent)
    proposal_nodes = {n["id"]: n for n in proposal.get("nodes") or []}
    changes = {entry["id"]: entry for entry in planned["nodes_changed"]}
    removed = {n["id"] for n in planned["nodes_only_online"]}
    added = [n["id"] for n in planned["nodes_only_local"]]

    replaced: list[str] = []
    nodes: list[dict[str, Any]] = []
    for node in sent.get("nodes") or []:
        node_id = node.get("id")
        if node_id in removed and not _is_note(node):
            continue
        if node_id in changes:
            merged, whole = _apply_change(node, proposal_nodes[node_id], changes[node_id])
            if whole:
                replaced.append(node_id)
            nodes.append(merged)
        else:
            nodes.append(copy.deepcopy(node))
    new_nodes = [copy.deepcopy(proposal_nodes[i]) for i in added]

    edges = _merge_edges(sent.get("edges") or [], proposal.get("edges") or [], planned, removed)
    _layout(nodes, new_nodes, edges)
    nodes += new_nodes

    canvas = {
        "nodes": nodes,
        "edges": edges,
        "viewport": copy.deepcopy(sent.get("viewport") or proposal.get("viewport")),
    }
    return MergeResult(canvas=canvas, diff=diff(canvas, sent), replaced=replaced)


# ---- nodes ---------------------------------------------------------------------------------


def _apply_change(
    sent_node: dict[str, Any], proposed: dict[str, Any], change: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """``(merged node, replaced whole)``."""
    merged = copy.deepcopy(sent_node)
    data = merged["data"]
    # A parameter only the sent node has is one the template does not know (the
    # compiled proposal carries every template parameter): the declaration could not
    # carry it, so it is kept, never "removed" (AC-08).
    kept_only_sent = [p["key"] for p in change["params"] if p.get("only") == "online"]
    edited = [p for p in change["params"] if p.get("only") != "online"]
    whole = any(f["field"] in _IDENTITY_FIELDS for f in change["fields"]) or any(
        p.get("only") == "local" for p in edited
    )
    if not whole:
        for entry in edited:
            if not _set_param(data, proposed["data"], entry["key"]):
                whole = True
                break
    if whole:
        merged["data"] = {**copy.deepcopy(sent_node["data"]), **copy.deepcopy(proposed["data"])}
        _carry_params(merged["data"], sent_node["data"], kept_only_sent)
        return merged, True
    for entry in change["fields"]:
        name = entry["field"]
        if name == "tab" and isinstance(data.get("tab"), dict):
            data["tab"]["value"] = copy.deepcopy(entry["local"])
        else:
            data[name] = copy.deepcopy(entry["local"])
    return merged, False


def _set_param(data: dict[str, Any], proposed_data: dict[str, Any], key: str) -> bool:
    """Copy ``key``'s value(s) from the proposal, occurrence by occurrence; False if the counts differ."""
    targets = _slots(data, key)
    sources = _slots(proposed_data, key)
    if len(targets) != len(sources) or not targets:
        return False
    for target, source in zip(targets, sources, strict=True):
        target["value"] = copy.deepcopy(source.get("value"))
    return True


def _carry_params(data: dict[str, Any], sent_data: dict[str, Any], keys: list[str]) -> None:
    """Put the sent node's template-less parameters back into a replaced node, in a group of the same name."""
    groups = data.setdefault("group_params", [])
    for sent_group in sent_data.get("group_params") or []:
        carried = [copy.deepcopy(p) for p in (sent_group or {}).get("params") or [] if p.get("key") in keys]
        if not carried:
            continue
        target = next((g for g in groups if g.get("name") == sent_group.get("name")), None)
        if target is None:
            target = {"name": sent_group.get("name"), "params": []}
            groups.append(target)
        target.setdefault("params", []).extend(carried)


def _slots(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [
        param
        for group in data.get("group_params") or []
        for param in (group or {}).get("params") or []
        if isinstance(param, dict) and param.get("key") == key
    ]


def _is_note(node: dict[str, Any]) -> bool:
    return (node.get("data") or {}).get("type") == NOTE_TYPE or node.get("type") == "noteNode"


# ---- edges ---------------------------------------------------------------------------------


def _key(edge: dict[str, Any]) -> tuple[str, str, str]:
    return (
        json.dumps(edge.get("source"), ensure_ascii=False),
        json.dumps(edge.get("sourceHandle"), ensure_ascii=False),
        json.dumps(edge.get("target"), ensure_ascii=False),
    )


def _merge_edges(
    sent: list[dict[str, Any]], proposed: list[dict[str, Any]], planned: dict[str, Any], removed_nodes: set[str]
) -> list[dict[str, Any]]:
    drop = Counter(_key(e) for e in planned["edges_only_online"])
    kept = []
    for edge in sent:
        k = _key(edge)
        if drop[k] > 0:
            drop[k] -= 1
            continue
        if edge.get("source") in removed_nodes or edge.get("target") in removed_nodes:
            continue
        kept.append(copy.deepcopy(edge))
    add = Counter(_key(e) for e in planned["edges_only_local"])
    for edge in proposed:
        k = _key(edge)
        if add[k] > 0:
            add[k] -= 1
            kept.append(copy.deepcopy(edge))
    return kept


# ---- layout (decision 8) -----------------------------------------------------------------------


def _size(node: dict[str, Any]) -> tuple[float, float]:
    measured = node.get("measured") or {}
    return float(measured.get("width") or DEFAULT_NODE_SIZE[0]), float(measured.get("height") or DEFAULT_NODE_SIZE[1])


def _layout(existing: list[dict[str, Any]], new_nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    """New nodes in columns right of the existing canvas, one column per BFS depth from start."""
    if not new_nodes:
        return
    placed = [n for n in existing if isinstance(n.get("position"), dict)]
    if placed:
        left = max(n["position"]["x"] + _size(n)[0] for n in placed) + _GAP_X
        top = min(n["position"]["y"] for n in placed)
    else:
        left, top = 0.0, 0.0

    depth = _depths(existing + new_nodes, edges)
    unreachable = max(depth.values(), default=0) + 1
    columns: dict[int, list[dict[str, Any]]] = {}
    for node in new_nodes:
        columns.setdefault(depth.get(node["id"], unreachable), []).append(node)

    x = left
    for level in sorted(columns):
        column = columns[level]
        y = top
        for node in column:
            node["position"] = {"x": x, "y": y}
            y += _size(node)[1] + _GAP_Y
        x += max(_size(n)[0] for n in column) + _GAP_X


def _depths(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, int]:
    targets: dict[str, list[str]] = {}
    for edge in edges:
        targets.setdefault(edge.get("source"), []).append(edge.get("target"))
    starts = [n["id"] for n in nodes if (n.get("data") or {}).get("type") == _START_TYPE]
    depth = dict.fromkeys(starts, 0)
    queue = deque(starts)
    while queue:
        current = queue.popleft()
        for nxt in targets.get(current, []):
            if nxt not in depth:
                depth[nxt] = depth[current] + 1
                queue.append(nxt)
    return depth
