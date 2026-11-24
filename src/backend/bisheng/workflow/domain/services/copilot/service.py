# ruff: noqa: RUF001 -- Chinese issue copy with full-width punctuation
"""One copilot turn (F007 design decisions 2, 6 and 7).

``prepare`` runs before the response starts streaming, so what it raises (10562:
too large) becomes an ordinary JSON error. ``stream`` then yields the turn's
events:

=============  =====================================================================
``status``     ``stage``: ``generating`` / ``retrying`` / ``validating``, ``attempt``
``delta``      model text outside the ```yaml block, as it streams
``message``    the whole text reply (no YAML) — what the drawer keeps as history
``proposal``   ``canvas`` (merged), ``diff`` (of the merged canvas), ``needs_selection``,
               ``replaced``
``issues``     three attempts failed: the human-readable problems (AC-10)
``error``      ``code`` 10563 and the exception *class* — never its text (AC-25)
=============  =====================================================================

The model's answer must parse as plain YAML (``safe_load``: a tag such as
``!include`` is an arbitrary file read on a server), resolve its resources,
compile and validate under the shared rules, keep the code-node switch (the
compiler does not know it, AC-12) and the copilot-only reachability rule on what
it adds or changes (AC-05). Failing any of that, the problems are fed back and
the model tries again, three attempts in all.

The model call is awaited inside the generator, never in a thread or a shielded
task: when the client disconnects, starlette cancels the generator and the call
with it (AC-11; decision 7 explains the ASGI condition this relies on).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from loguru import logger

from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum
from bisheng.common.errcode.flow import WorkflowCopilotModelCallError
from bisheng.llm.domain.services.llm import LLMService
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot.merge import merge
from bisheng.workflow.domain.services.copilot.prompt import build_messages, retry_messages
from bisheng.workflow.domain.services.copilot.resources import Catalogue, resolve
from bisheng_flow_dsl.compiler import compile_spec, describe
from bisheng_flow_dsl.decompile import decompile
from bisheng_flow_dsl.diff import diff
from bisheng_flow_dsl.dump import dump
from bisheng_flow_dsl.errors import DslError
from bisheng_flow_dsl.validate import validate

_YAML_BLOCK = re.compile(r"```ya?ml[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_ANY_YAML_BLOCK = re.compile(r"```ya?ml\b.*?(```|\Z)", re.DOTALL | re.IGNORECASE)
_UNRESOLVED_TYPE = "__copilot_unresolved__"
_CODE_TYPE = "code"
_END_TYPE = "end"
_START_TYPE = "start"
_TEMPERATURE = 0.2


@dataclass
class PreparedTurn:
    flow_id: str
    flow_name: str
    user_id: Any
    sent: dict[str, Any]
    base: dict[str, Any]
    messages: list[dict[str, str]]
    catalogue: Catalogue
    code_node_enabled: bool
    declaration_chars: int


class WorkflowCopilotService:
    MAX_ATTEMPTS = 3

    @classmethod
    def prepare(
        cls,
        *,
        flow_id: str,
        flow_name: str,
        canvas: dict[str, Any],
        history: list[dict[str, Any]],
        message: str,
        catalogue: Catalogue,
        code_node_enabled: bool,
        user_id: Any,
    ) -> PreparedTurn:
        sent = {
            "nodes": list(canvas.get("nodes") or []),
            "edges": list(canvas.get("edges") or []),
            "viewport": canvas.get("viewport"),
        }
        base, _omitted = decompile(sent, NODE_TEMPLATES, catalogue.tools, name=flow_name or "workflow")
        declaration = dump(base)
        messages = build_messages(
            declaration_yaml=declaration,
            history=history,
            message=message,
            catalogue=catalogue,
            code_node_enabled=code_node_enabled,
        )
        return PreparedTurn(
            flow_id=flow_id,
            flow_name=flow_name,
            user_id=user_id,
            sent=sent,
            base=base,
            messages=messages,
            catalogue=catalogue,
            code_node_enabled=code_node_enabled,
            declaration_chars=len(declaration),
        )

    @classmethod
    async def stream(cls, turn: PreparedTurn) -> AsyncIterator[dict[str, Any]]:
        started, attempts, outcome = time.monotonic(), 0, "error"
        try:
            llm = await LLMService.get_bisheng_llm(
                model_id=turn.catalogue.default_model_id,
                temperature=_TEMPERATURE,
                streaming=True,
                app_id=turn.flow_id,
                app_name=turn.flow_name,
                app_type=ApplicationTypeEnum.WORKFLOW,
                user_id=turn.user_id,
            )
            messages = turn.messages
            for attempt in range(1, cls.MAX_ATTEMPTS + 1):
                attempts = attempt
                yield {"type": "status", "stage": "generating" if attempt == 1 else "retrying", "attempt": attempt}
                fence = _FenceFilter()
                answer = ""
                try:
                    async for chunk in llm.astream(_as_langchain(messages)):
                        text = chunk.content if isinstance(chunk.content, str) else ""
                        answer += text
                        visible = fence.feed(text)
                        if visible:
                            yield {"type": "delta", "text": visible}
                    tail = fence.flush()
                    if tail:
                        yield {"type": "delta", "text": tail}
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception as exc:
                    outcome = "model_error"
                    yield {
                        "type": "error",
                        "code": WorkflowCopilotModelCallError.Code,
                        "message": f"{WorkflowCopilotModelCallError.Msg}（{type(exc).__name__}）",
                    }
                    return

                reply = _text_only(answer)
                block = _yaml_block(answer)
                if block is None and attempt == 1:
                    outcome = "text"
                    yield {"type": "message", "text": reply}
                    return
                if block is None:
                    issues = ["回答里没有 ```yaml 代码块"]
                else:
                    yield {"type": "status", "stage": "validating", "attempt": attempt}
                    proposal, issues = _check(block, turn)
                    if not issues:
                        outcome = "proposal"
                        yield {"type": "message", "text": reply}
                        yield {"type": "proposal", **proposal}
                        return
                if attempt < cls.MAX_ATTEMPTS:
                    messages = retry_messages(messages, answer, issues)
                    continue
                outcome = "issues"
                yield {"type": "message", "text": reply}
                yield {"type": "issues", "issues": issues}
                return
        finally:
            # One line per turn, never the canvas or the dialog (AC-25).
            logger.info(
                f"workflow copilot turn flow={turn.flow_id} attempts={attempts} outcome={outcome} "
                f"elapsed_ms={int((time.monotonic() - started) * 1000)} declaration_chars={turn.declaration_chars}"
            )


# ---- checking one answer ---------------------------------------------------------------------


def _check(block: str, turn: PreparedTurn) -> tuple[dict[str, Any], list[str]]:
    """``(proposal fields, issues)``; the proposal is only meaningful when issues is empty."""
    try:
        spec = yaml.safe_load(block)
    except yaml.constructor.ConstructorError:
        return {}, ["不能使用 `!include` 等 YAML 标签：长文本请直接写进 YAML（多行用 `|`）"]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行）" if mark is not None else ""
        return {}, [f"YAML 格式错误{where}"]
    if not isinstance(spec, dict) or not isinstance(spec.get("nodes"), list):
        return {}, ["声明必须是一个映射，并且有 nodes 列表"]

    resolution = resolve(spec, turn.base, turn.catalogue, NODE_TEMPLATES)
    declaration, tools = _with_placeholders(resolution.declaration, turn.catalogue.tools, resolution.unresolved_tools)
    try:
        definition = compile_spec(declaration, NODE_TEMPLATES, tools)
    except DslError as error:
        found = (error.details or {}).get("issues") if isinstance(error.details, dict) else None
        return {}, [describe(issue) for issue in found] if found else [error.message]

    issues = [issue["message"] for issue in validate(definition, NODE_TEMPLATES, tools)]
    if not turn.code_node_enabled:
        issues += [
            f"节点 {n['id']} 是代码节点，但本平台已关闭代码节点，不能使用"
            for n in definition["nodes"]
            if (n.get("data") or {}).get("type") == _CODE_TYPE
        ]
    issues += _reachability(definition, turn.sent)
    if issues:
        return {}, issues

    dropped = set(resolution.unresolved_tools)
    definition["nodes"] = [n for n in definition["nodes"] if n["id"] not in dropped]
    definition["edges"] = [
        e for e in definition["edges"] if e.get("source") not in dropped and e.get("target") not in dropped
    ]
    merged = merge(turn.sent, definition)
    return {
        "canvas": merged.canvas,
        "diff": merged.diff,
        "needs_selection": resolution.needs_selection,
        "replaced": merged.replaced,
    }, []


def _with_placeholders(
    declaration: dict[str, Any], tools: list[dict[str, Any]], unresolved: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Let unresolved tool nodes through the checks (their wiring is still the model's
    intent to validate); they are dropped from the proposal afterwards (decision 4)."""
    if not unresolved:
        return declaration, tools
    declaration = {**declaration, "nodes": [dict(n) for n in declaration.get("nodes") or []]}
    children = []
    for node in declaration["nodes"]:
        if node.get("id") not in unresolved:
            continue
        params = node.get("params") if isinstance(node.get("params"), dict) else {}
        tool_key = f"{_UNRESOLVED_TYPE}:{node['id']}"
        children.append(
            {
                "id": -len(children) - 1,
                "name": node["id"],
                "tool_key": tool_key,
                "node_template": {
                    "tool_key": tool_key,
                    "type": "tool",
                    "name": node.get("name") or node["id"],
                    "group_params": [
                        {"params": [{"key": key, "value": ""} for key in params] + [{"key": "output", "value": ""}]}
                    ],
                },
            }
        )
        node["tool"] = f"{_UNRESOLVED_TYPE}/{node['id']}"
    return declaration, [*tools, {"id": -1, "name": _UNRESOLVED_TYPE, "children": children}]


def _reachability(definition: dict[str, Any], sent: dict[str, Any]) -> list[str]:
    """AC-05, copilot-only: what the proposal adds or changes is reachable from start, and ends or goes on."""
    delta = diff(definition, sent)
    touched = [n["id"] for n in delta["nodes_only_local"]] + [c["id"] for c in delta["nodes_changed"]]
    if not touched:
        return []
    nodes = {n["id"]: n for n in definition["nodes"]}
    targets: dict[str, list[str]] = {}
    for edge in definition["edges"]:
        targets.setdefault(edge.get("source"), []).append(edge.get("target"))
    reached = {i for i, n in nodes.items() if (n.get("data") or {}).get("type") == _START_TYPE}
    queue = deque(reached)
    while queue:
        for nxt in targets.get(queue.popleft(), []):
            if nxt not in reached:
                reached.add(nxt)
                queue.append(nxt)
    issues = []
    for node_id in touched:
        node_type = (nodes[node_id].get("data") or {}).get("type")
        if node_id not in reached:
            issues.append(f"节点 {node_id} 从开始节点沿连线走不到")
        if node_type != _END_TYPE and not targets.get(node_id):
            issues.append(f"节点 {node_id} 没有出边：除结束节点外，新增或改动的节点都要连到下一个节点")
    return issues


# ---- text handling ---------------------------------------------------------------------------


def _yaml_block(answer: str) -> str | None:
    found = _YAML_BLOCK.search(answer)
    return found.group(1) if found else None


def _text_only(answer: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", _ANY_YAML_BLOCK.sub("", answer)).strip()


class _FenceFilter:
    """Streams the text outside ```yaml blocks; holds back a tail that might start a fence."""

    _OPEN = "```"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, text: str) -> str:
        self._buffer += text
        shown = ""
        while True:
            if self._inside:
                end = self._buffer.find(self._OPEN)
                if end < 0:
                    self._buffer = self._buffer[-(len(self._OPEN) - 1) :]
                    return shown
                self._buffer = self._buffer[end + len(self._OPEN) :]
                self._inside = False
                continue
            start = self._buffer.find(self._OPEN)
            if start < 0:
                keep = len(self._OPEN) - 1
                shown += self._buffer[:-keep] if len(self._buffer) > keep else ""
                self._buffer = self._buffer[-keep:] if len(self._buffer) > keep else self._buffer
                return shown
            shown += self._buffer[:start]
            self._buffer = self._buffer[start + len(self._OPEN) :]
            self._inside = True

    def flush(self) -> str:
        """The held-back tail once the stream has ended (nothing, if it ended inside a block)."""
        tail, self._buffer = ("" if self._inside else self._buffer), ""
        return tail


def _as_langchain(messages: list[dict[str, str]]) -> list[Any]:
    kinds = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [kinds[m["role"]](content=m["content"]) for m in messages]
