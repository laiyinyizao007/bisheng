"""Resources the model writes by name → ids the user can actually use (F007 design decision 4).

Two halves:

- ``load_catalogue`` gathers the usable set from the same services the canvas
  dropdowns use (knowledge and tools with ``action="use"``; online LLMs; for
  agent nodes the assistant model list, as the platform's own import does). It
  passes no tenant id: the request context carries it (constitution C3).
- ``resolve`` is pure. It walks the parameters the node templates mark as
  resources (by parameter *type*, never by key name) and turns names into ids.
  A unique match is filled in; no match / several matches leave a placeholder
  that still compiles (model ``0``, an empty list) plus a ``needs_selection``
  entry. It only touches what the model wrote or changed: a value equal to the
  one in the canvas the user sent is left alone, usable or not (AC-08).

Tool *nodes* are the exception: a tool node is its tool, so one whose tool does
not resolve cannot be left half-filled. It is reported in ``unresolved_tools``
and the caller drops it from the proposal (design decision 4).
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from bisheng.common.errcode.flow import WorkflowCopilotModelNotConfiguredError, WorkflowCopilotModelUnavailableError
from bisheng.knowledge.domain.models.knowledge import KnowledgeTypeEnum
from bisheng.knowledge.domain.services.knowledge_service import KnowledgeService
from bisheng.llm.domain.const import LLMModelType
from bisheng.llm.domain.services.llm import LLMService
from bisheng.tool.domain.services.tool import ToolServices
from bisheng_flow_dsl.compiler import TOOL_TYPE, resolve_tool

MODEL_PARAM = "bisheng_model"
AGENT_MODEL_PARAM = "agent_model"
KNOWLEDGE_PARAM = "knowledge_select_multi"  # {"type": "knowledge", "value": [{"key", "label"}]}
QA_KNOWLEDGE_PARAM = "qa_select_multi"  # [{"key", "label"}]
TOOL_LIST_PARAM = "add_tool"  # [{"key", "label"}]
RESOURCE_PARAMS = frozenset({MODEL_PARAM, AGENT_MODEL_PARAM, KNOWLEDGE_PARAM, QA_KNOWLEDGE_PARAM, TOOL_LIST_PARAM})
#: Node types whose model parameters get a default — the platform's own import rule
#: (frontend ``flowUtils.ts``); the optional "recommended model" on input nodes does not.
DEFAULT_MODEL_NODES = frozenset({"rag", "llm", "agent", "qa_retriever"})
#: Placeholder for a model left to the user: compiles, and the platform's own save /
#: run checks stop it if nobody picks one.
UNSET_MODEL = 0
MAX_CANDIDATES = 10
#: Upper bound on knowledge pages walked per type (page size below): a guard, not a quota.
_KNOWLEDGE_PAGE_SIZE = 50
_KNOWLEDGE_MAX_PAGES = 40
_MISSING = object()


@dataclass(frozen=True)
class Resource:
    id: int
    name: str
    aliases: tuple[str, ...] = ()

    def answers_to(self, wanted: str) -> bool:
        target = _norm(wanted)
        return bool(target) and any(_norm(n) == target for n in (self.name, *self.aliases))


@dataclass
class Catalogue:
    models: list[Resource]
    agent_models: list[Resource]
    knowledge: list[Resource]
    qa_knowledge: list[Resource]
    #: The tool list exactly as ``ToolServices.get_tool_list`` returns it (children carry
    #: ``node_template``) — the shape ``bisheng_flow_dsl`` compiles against.
    tools: list[dict[str, Any]]
    default_model_id: int
    default_agent_model_id: int | None

    def tool_children(self) -> list[Resource]:
        return [
            Resource(int(child["id"]), str(child.get("name") or ""))
            for tool_type in self.tools
            for child in tool_type.get("children") or []
            if isinstance(child, dict) and child.get("id") is not None
        ]


@dataclass
class Resolution:
    declaration: dict[str, Any]
    needs_selection: list[dict[str, Any]] = field(default_factory=list)
    #: node id → ``{"wanted", "candidates"}`` for tool nodes whose tool did not resolve.
    unresolved_tools: dict[str, dict[str, Any]] = field(default_factory=dict)


# ---- the usable set ------------------------------------------------------------------------


async def check_default_model() -> tuple[int, dict[int, Resource]]:
    """``(workflow default model id, online LLMs by id)``; raises 10560 / 10561 when the copilot's own model is unusable.

    Cheap on purpose (no knowledge or tool listing): the editor asks it when the drawer
    opens, so an unusable model disables sending without a generation request (AC-03).
    """
    workflow_cfg, _, _ = await LLMService.aget_workflow_llm_with_meta()
    default_model_id = getattr(workflow_cfg, "model_id", None)
    if not default_model_id:
        raise WorkflowCopilotModelNotConfiguredError()

    online_llms: dict[int, Resource] = {}
    for server in await LLMService.get_all_llm():
        for model in getattr(server, "models", None) or []:
            if model.model_type == LLMModelType.LLM.value and model.online and model.id is not None:
                online_llms[int(model.id)] = Resource(
                    int(model.id), model.name or model.model_name, (model.model_name,)
                )
    if int(default_model_id) not in online_llms:
        raise WorkflowCopilotModelUnavailableError()
    return int(default_model_id), online_llms


async def load_catalogue(*, request: Any, login_user: Any) -> Catalogue:
    """Everything the user can put into a node. Raises 10560 / 10561 when the copilot's own model is unusable."""
    default_model_id, online_llms = await check_default_model()

    assistant = await LLMService.get_assistant_llm()
    agent_models, default_agent = [], None
    for item in getattr(assistant, "llm_list", None) or []:
        resource = online_llms.get(int(item.model_id)) if item.model_id is not None else None
        if resource is None:
            continue
        agent_models.append(resource)
        if getattr(item, "default", False):
            default_agent = resource.id

    tools = [
        one.model_dump() if hasattr(one, "model_dump") else dict(one)
        for one in await ToolServices(login_user=login_user).get_tool_list(None, "use")
    ]
    return Catalogue(
        models=list(online_llms.values()),
        agent_models=agent_models,
        knowledge=await _knowledge(request, login_user, KnowledgeTypeEnum.NORMAL),
        qa_knowledge=await _knowledge(request, login_user, KnowledgeTypeEnum.QA),
        tools=tools,
        default_model_id=int(default_model_id),
        default_agent_model_id=default_agent,
    )


async def _knowledge(request: Any, login_user: Any, knowledge_type: KnowledgeTypeEnum) -> list[Resource]:
    found: list[Resource] = []
    cursor = None
    for _ in range(_KNOWLEDGE_MAX_PAGES):
        page = await KnowledgeService.get_knowledge(
            request, login_user, knowledge_type, cursor=cursor, page_size=_KNOWLEDGE_PAGE_SIZE, action="use"
        )
        found += [Resource(int(row.id), str(row.name or "")) for row in page.data]
        if not page.has_more or not page.next_cursor:
            break
        cursor = page.next_cursor
    return found


# ---- names → ids ------------------------------------------------------------------------------


def resolve(
    declaration: dict[str, Any], base: dict[str, Any], catalogue: Catalogue, templates: list[dict[str, Any]]
) -> Resolution:
    """See the module docstring. ``base`` is the declaration of the canvas the user sent."""
    out = Resolution(declaration=copy.deepcopy(declaration))
    by_type = {t.get("type"): t for t in templates if isinstance(t, dict)}
    base_nodes = {n.get("id"): n for n in base.get("nodes") or [] if isinstance(n, dict)}

    for node in out.declaration.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id, node_type = node.get("id"), node.get("type")
        before = base_nodes.get(node_id)
        if node_type == TOOL_TYPE:
            _tool_node(node, before, catalogue, out)
            continue
        template = by_type.get(node_type)
        if template is None:
            continue  # compile reports unknown types
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        before_params = (before or {}).get("params") if isinstance((before or {}).get("params"), dict) else {}
        for slot in _resource_slots(template):
            key = slot["key"]
            value = params.get(key, _MISSING)
            previous = before_params.get(key, _MISSING) if before is not None else _MISSING
            if before is not None and _canon(value) == _canon(previous):
                continue  # untouched by this request (AC-08)
            resolved = _one_param(node_id, node_type, slot, value, previous, catalogue, out)
            if resolved is not _MISSING:
                params[key] = resolved
        if params:
            node["params"] = params
    return out


def _resource_slots(template: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        param
        for group in template.get("group_params") or []
        for param in (group or {}).get("params") or []
        if isinstance(param, dict) and param.get("type") in RESOURCE_PARAMS and isinstance(param.get("key"), str)
    ]


def _one_param(
    node_id: Any, node_type: Any, slot: dict[str, Any], value: Any, previous: Any, catalogue: Catalogue, out: Resolution
) -> Any:
    kind = slot["type"]
    key = slot["key"]
    if kind in (MODEL_PARAM, AGENT_MODEL_PARAM):
        agent = kind == AGENT_MODEL_PARAM
        pool = catalogue.agent_models if agent else catalogue.models
        if value is _MISSING or value in ("", None):
            if node_type not in DEFAULT_MODEL_NODES:
                return _MISSING
            default = catalogue.default_agent_model_id if agent else catalogue.default_model_id
            if default is not None:
                return default
            _need(out, node_id, key, "model", "", pool)
            return UNSET_MODEL
        return _model(node_id, key, value, pool, out)

    if kind == KNOWLEDGE_PARAM:
        if not isinstance(value, dict):
            return _MISSING
        items = _items(
            node_id, key, value.get("value"), _items_of(previous, nested=True), catalogue.knowledge, "knowledge", out
        )
        return {**value, "type": value.get("type") or "knowledge", "value": items}
    pool, need_kind = (
        (catalogue.qa_knowledge, "knowledge") if kind == QA_KNOWLEDGE_PARAM else (catalogue.tool_children(), "tool")
    )
    if not isinstance(value, list):
        return _MISSING
    return _items(node_id, key, value, _items_of(previous, nested=False), pool, need_kind, out)


def _model(node_id: Any, key: str, value: Any, pool: list[Resource], out: Resolution) -> int:
    ids = {r.id for r in pool}
    as_id = _as_int(value)
    if as_id is not None:
        if as_id in ids:
            return as_id
        _need(out, node_id, key, "model", str(value), pool)
        return UNSET_MODEL
    wanted = str(value).strip()
    matches = [r for r in pool if r.answers_to(wanted)]
    if len(matches) == 1:
        return matches[0].id
    _need(out, node_id, key, "model", wanted, matches or pool)
    return UNSET_MODEL


def _items(
    node_id: Any,
    key: str,
    items: Any,
    kept: set[str],
    pool: list[Resource],
    kind: str,
    out: Resolution,
) -> list[dict[str, Any]]:
    by_id = {str(r.id): r for r in pool}
    resolved = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        item_key, label = item.get("key"), item.get("label")
        if item_key is not None and str(item_key) in kept:
            resolved.append(item)  # already on the canvas the user sent
            continue
        if item_key is not None:
            hit = by_id.get(str(item_key))
            if hit is not None:
                resolved.append({"key": hit.id, "label": hit.name})
            else:
                _need(out, node_id, key, kind, str(label or item_key), pool)
            continue
        wanted = str(label or "").strip()
        matches = [r for r in pool if r.answers_to(wanted)]
        if len(matches) == 1:
            resolved.append({"key": matches[0].id, "label": matches[0].name})
        else:
            _need(out, node_id, key, kind, wanted, matches or pool)
    return resolved


def _items_of(value: Any, *, nested: bool) -> set[str]:
    if value is _MISSING:
        return set()
    items = value.get("value") if nested and isinstance(value, dict) else value
    return (
        {str(i.get("key")) for i in items or [] if isinstance(i, dict) and i.get("key") is not None}
        if isinstance(items, list)
        else set()
    )


def _tool_node(node: dict[str, Any], before: dict[str, Any] | None, catalogue: Catalogue, out: Resolution) -> None:
    reference = node.get("tool")
    if before is not None and before.get("tool") == reference:
        return
    _, issue = resolve_tool(catalogue.tools, reference)
    if issue is None:
        return
    candidates = issue.get("candidates") or [r.name for r in catalogue.tool_children()]
    out.unresolved_tools[node.get("id")] = {"wanted": str(reference or ""), "candidates": candidates[:MAX_CANDIDATES]}
    out.needs_selection.append(
        {
            "node_id": node.get("id"),
            "param_key": "tool",
            "kind": "tool",
            "wanted": str(reference or ""),
            "candidates": candidates[:MAX_CANDIDATES],
        }
    )


def _need(out: Resolution, node_id: Any, key: str, kind: str, wanted: str, candidates: list[Resource]) -> None:
    out.needs_selection.append(
        {
            "node_id": node_id,
            "param_key": key,
            "kind": kind,
            "wanted": wanted,
            "candidates": [r.name for r in candidates[:MAX_CANDIDATES]],
        }
    )


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _norm(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def _canon(value: Any) -> str:
    if value is _MISSING:
        return "<missing>"
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
