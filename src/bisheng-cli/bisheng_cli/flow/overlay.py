"""The constrained Python overlay (design decision 6, AC-08 / AC-09).

An overlay is a Python file exporting ``apply(spec)``. It receives a deep copy of
the loaded declaration and either returns the new mapping or mutates the copy in
place (returning ``None``). It exists for *environment differences* — "the model
id is 7 on this platform" — without a second description of the structure.

**This is the only code ``bisheng flow`` ever executes.** The YAML declaration is
data and is never executed (``spec.py``); the overlay is code the developer names
explicitly with ``--overlay``, so it is trusted the same way their own scripts
are. It can ``import os``; nothing here pretends otherwise. What is constrained is
its **effect on the workflow**, checked after it runs:

=========================================  =========================================
allowed                                    refused (each item reported)
=========================================  =========================================
change the value of a declared parameter   add / remove a parameter key
append a node the YAML does not declare    remove a declared node
append an edge the YAML does not declare   remove a declared edge
—                                          change a declared edge's target / handle
—                                          change a declared node's id / type / any
                                           other field (``tool``, ``tab``, ``cases``)
—                                          change a top-level field (``name`` …)
—                                          append a node whose id already exists
=========================================  =========================================

The YAML stays the single source of truth, which is what keeps ``flow diff``
meaningful: an overlay that could rewrite declared structure would make the two
descriptions contradict each other.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError

DEFAULT_HANDLE = "right_handle"
_STRUCTURE_KEYS = ("nodes", "edges")


def apply_overlay(spec: dict[str, Any], overlay_path: str | Path) -> dict[str, Any]:
    """Run ``overlay_path``'s ``apply`` on a copy of ``spec`` and enforce the rules above."""
    apply = _load_apply(Path(overlay_path))
    working = copy.deepcopy(spec)
    try:
        returned = apply(working)
    except Exception as exc:
        raise CliError(
            f"覆盖层执行出错：{exc.__class__.__name__}: {exc}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="覆盖层是你自己的代码；修正后重试。",
            details={"overlay": str(overlay_path)},
        )
    result = working if returned is None else returned
    if not isinstance(result, dict):
        raise CliError(
            f"覆盖层的 apply() 必须返回声明映射或 None，实际返回了 {type(result).__name__}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="返回传入的 spec（或原地修改后不返回）。",
            details={"overlay": str(overlay_path)},
        )

    refused = check_overlay(spec, result)
    if refused:
        raise CliError(
            "覆盖层改动了 YAML 已声明的结构：" + "；".join(describe(item) for item in refused),
            exit_code=EXIT_LOCAL_INVALID,
            next_step="覆盖层只能改已声明参数的取值、追加未声明的节点与连线；结构改动请直接写进 YAML。",
            details={"overlay": str(overlay_path), "refused": refused},
        )
    return result


def check_overlay(declared: dict[str, Any], result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every way ``result`` departs from what the overlay may do to ``declared``."""
    refused: list[dict[str, Any]] = []
    for key in sorted(set(declared) | set(result)):
        if key in _STRUCTURE_KEYS:
            continue
        if declared.get(key, _MISSING) != result.get(key, _MISSING):
            refused.append({"kind": "top_level_changed", "field": key})
    refused += _check_nodes(_as_list(declared.get("nodes")), _as_list(result.get("nodes")))
    refused += _check_edges(_as_list(declared.get("edges")), _as_list(result.get("edges")))
    return refused


def _check_nodes(declared: list[Any], result: list[Any]) -> list[dict[str, Any]]:
    refused: list[dict[str, Any]] = []
    for index, node in enumerate(result):
        if not _hashable(_get(node, "id")):
            refused.append({"kind": "node_id_invalid", "index": index})
    result = [node for node in result if _hashable(_get(node, "id"))]
    by_id = {_get(node, "id"): node for node in result}
    declared_ids = {_h(_get(node, "id")) for node in declared}
    for index, node in enumerate(declared):
        node_id = _get(node, "id")
        now = by_id.get(_h(node_id))
        if now is None:
            # Same slot holding a node with a new id reads as a rename; otherwise it is gone.
            slot = result[index] if index < len(result) else None
            slot_id = _get(slot, "id")
            if slot is not None and _h(slot_id) not in declared_ids:
                refused.append({"kind": "node_id_changed", "node": node_id, "now": slot_id})
            else:
                refused.append({"kind": "node_deleted", "node": node_id})
            continue
        if _get(now, "type") != _get(node, "type"):
            refused.append(
                {"kind": "node_type_changed", "node": node_id, "was": _get(node, "type"), "now": _get(now, "type")}
            )
        refused += _check_params(node_id, _params(node), _params(now))
        for field in sorted((set(_keys(node)) | set(_keys(now))) - {"id", "type", "params"}):
            if _get(node, field, _MISSING) != _get(now, field, _MISSING):
                refused.append({"kind": "node_field_changed", "node": node_id, "field": field})
    # Only *undeclared* nodes may be appended: a second node carrying an id that is
    # already in the result (declared or not) is a duplicate, not an addition.
    counts = Counter(_get(node, "id") for node in result)
    refused += [{"kind": "node_duplicated", "node": node_id} for node_id, count in counts.items() if count > 1]
    return refused


def _check_params(node_id: Any, declared: dict[str, Any], now: dict[str, Any]) -> list[dict[str, Any]]:
    refused = [{"kind": "param_added", "node": node_id, "param": key} for key in now if key not in declared]
    refused += [{"kind": "param_deleted", "node": node_id, "param": key} for key in declared if key not in now]
    return refused


def _check_edges(declared: list[Any], result: list[Any]) -> list[dict[str, Any]]:
    refused: list[dict[str, Any]] = []
    declared_keys = [_edge_key(edge) for edge in declared]
    result_keys = [_edge_key(edge) for edge in result]
    present = set(result_keys)
    for index, key in enumerate(declared_keys):
        if key in present:
            continue
        # A declared edge whose slot now holds a different, undeclared edge from the
        # same source was re-pointed (target / handle); otherwise it was removed.
        slot = result_keys[index] if index < len(result_keys) else None
        if slot is not None and slot not in declared_keys and slot[0] == key[0]:
            refused.append({"kind": "edge_changed", "edge": _edge_dict(key), "now": _edge_dict(slot)})
        else:
            refused.append({"kind": "edge_deleted", "edge": _edge_dict(key)})
    return refused


def describe(item: dict[str, Any]) -> str:
    """One human-readable line per refused item (the machine form is ``details.refused``)."""
    kind = item["kind"]
    if kind == "top_level_changed":
        return f"改了顶层字段 {item['field']}"
    if kind == "node_deleted":
        return f"删除了节点 {item['node']}"
    if kind == "node_id_changed":
        return f"把节点 {item['node']} 的 id 改成了 {item['now']}"
    if kind == "node_type_changed":
        return f"把节点 {item['node']} 的类型从 {item['was']} 改成了 {item['now']}"
    if kind == "node_id_invalid":
        return f"第 {item['index']} 个节点的 id 不是合法的值"
    if kind == "node_duplicated":
        return f"出现了多个 id 为 {item['node']} 的节点（只能追加 YAML 未声明的新节点）"
    if kind == "node_field_changed":
        return f"改了节点 {item['node']} 的 {item['field']}"
    if kind == "param_added":
        return f"给节点 {item['node']} 新增了未声明的参数 {item['param']}"
    if kind == "param_deleted":
        return f"删除了节点 {item['node']} 的已声明参数 {item['param']}"
    if kind == "edge_deleted":
        return f"删除了连线 {_edge_text(item['edge'])}"
    if kind == "edge_changed":
        return f"把连线 {_edge_text(item['edge'])} 改成了 {_edge_text(item['now'])}"
    return kind


# ---- helpers -------------------------------------------------------------------

_MISSING = object()


def _load_apply(path: Path) -> Any:
    if not path.is_file():
        raise CliError(
            f"覆盖层文件不存在：{path}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="检查 --overlay 的路径。",
        )
    module_name = f"_bisheng_flow_overlay_{abs(hash(str(path.resolve())))}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    if module_spec is None or module_spec.loader is None:
        raise CliError(f"无法作为 Python 模块加载覆盖层：{path}", exit_code=EXIT_LOCAL_INVALID)
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:
        raise CliError(
            f"覆盖层加载出错：{exc.__class__.__name__}: {exc}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="修正覆盖层文件后重试。",
            details={"overlay": str(path)},
        )
    finally:
        sys.modules.pop(module_name, None)
    apply = getattr(module, "apply", None)
    if not callable(apply):
        raise CliError(
            f"覆盖层必须定义函数 apply(spec)：{path}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="在覆盖层里定义 def apply(spec): ...，修改后返回 spec。",
        )
    return apply


def _h(value: Any) -> Any:
    """A dict/set key for any id-like value: itself if hashable, else its repr."""
    return value if _hashable(value) else repr(value)


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _keys(node: Any) -> list[Any]:
    return list(node) if isinstance(node, dict) else []


def _get(node: Any, key: str, default: Any = None) -> Any:
    return node.get(key, default) if isinstance(node, dict) else default


def _params(node: Any) -> dict[str, Any]:
    params = _get(node, "params")
    return params if isinstance(params, dict) else {}


def _edge_key(edge: Any) -> tuple[Any, Any, Any]:
    return (_h(_get(edge, "from")), _h(_get(edge, "to")), _h(_get(edge, "handle") or DEFAULT_HANDLE))


def _edge_dict(key: tuple[Any, Any, Any]) -> dict[str, Any]:
    return {"from": key[0], "to": key[1], "handle": key[2]}


def _edge_text(edge: dict[str, Any]) -> str:
    return f"{edge['from']} →({edge['handle']}) {edge['to']}"
