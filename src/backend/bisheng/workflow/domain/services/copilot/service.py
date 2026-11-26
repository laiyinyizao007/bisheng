# ruff: noqa: RUF001 -- Chinese issue copy with full-width punctuation
"""One copilot turn (F007 design decisions 2, 6 and 7).

``prepare`` runs before the response starts streaming, so what it raises (10562:
too large) becomes an ordinary JSON error. ``stream`` then yields the turn's
events:

=============  =====================================================================
``status``     ``stage``: ``generating`` / ``retrying`` / ``validating``, ``attempt``
``reasoning``  a reasoning model's thinking, as it streams — shown only, never part of
               the answer, the reply or what is fed back on a retry
``delta``      model text outside the ```yaml block, as it streams
``draft``      the block's body as it streams (fences and info line dropped) — progress
               only: what the model wrote from the masked canvas, before any check
``message``    the whole text reply (no YAML) — what the drawer keeps as history
``proposal``   ``canvas`` (merged), ``diff`` (of the merged canvas), ``needs_selection``,
               ``replaced``
``issues``     three attempts failed: the human-readable problems (AC-10)
``ask``        F008: the model asks first — ``questions`` ``[{id, question, options}]``
               (at most 3, 2 to 4 options each); the turn ends, nothing is proposed
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

Bounds and secrets (G8 round 1): the canvas is capped in nodes and edges before it
is decompiled; credentials in node parameters are replaced by a marker before the
prompt and put back from the sent canvas afterwards (``redact.py``); the answer is
capped in length, its YAML may not use aliases or nest deeply, and each attempt
has a deadline. Sent nodes the declaration could not carry are put back as they
were, and an existing tool node whose new tool does not resolve stays unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
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
from bisheng.common.errcode.flow import WorkflowCopilotModelCallError, WorkflowCopilotTooLargeError
from bisheng.common.utils.think_tags import strip_reasoning_tags
from bisheng.llm.domain.services.llm import LLMService
from bisheng.llm.domain.utils import extract_reasoning_content
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot.merge import merge
from bisheng.workflow.domain.services.copilot.prompt import build_messages, retry_messages
from bisheng.workflow.domain.services.copilot.redact import REDACTED, is_sensitive, mask, redact, restore
from bisheng.workflow.domain.services.copilot.resources import Catalogue, resolve
from bisheng_flow_dsl.compiler import compile_spec, describe
from bisheng_flow_dsl.decompile import decompile
from bisheng_flow_dsl.diff import diff
from bisheng_flow_dsl.dump import dump
from bisheng_flow_dsl.errors import DslError
from bisheng_flow_dsl.validate import validate

_UNRESOLVED_TYPE = "__copilot_unresolved__"
_CODE_TYPE = "code"
_END_TYPE = "end"
_START_TYPE = "start"
_TEMPERATURE = 0.2
_NOTE_TYPES = frozenset({"note", "noteNode"})
#: The canvas is decompiled before any length check can see it: bound the work first.
MAX_CANVAS_NODES = 500
MAX_CANVAS_EDGES = 2_000
#: An answer longer than this is cut off: the declaration cap plus room for the text around it.
MAX_ANSWER_CHARS = 100_000
#: Reasoning forwarded to the drawer per attempt (a reasoning model wrote ~34k on a real turn).
MAX_REASONING_CHARS = 40_000
#: Silence allowed between two chunks of a model stream, reasoning included (AC-27: a timeout
#: is a failed turn, not a hang). F008: a reasoning model thinks for minutes while streaming,
#: so a stream is cut off only when it stops producing anything — not after a fixed total.
IDLE_TIMEOUT_SECONDS = 60.0
#: The wait for a stream's first chunk: a provider that does not stream its thinking sends
#: nothing until it is done thinking (G8, F008).
FIRST_CHUNK_TIMEOUT_SECONDS = 180.0
#: All attempts of one turn together; and the most a model stream's cleanup may take.
TURN_TIMEOUT_SECONDS = 600.0
CLOSE_TIMEOUT_SECONDS = 5.0
#: How deep the answer's YAML may nest; nothing in a declaration comes close.
MAX_YAML_DEPTH = 32


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
        if len(sent["nodes"]) > MAX_CANVAS_NODES or len(sent["edges"]) > MAX_CANVAS_EDGES:
            raise WorkflowCopilotTooLargeError()
        try:
            base, _omitted = decompile(sent, NODE_TEMPLATES, catalogue.tools, name=flow_name or "workflow")
        except RecursionError as exc:  # a parameter value nested past the interpreter's limit
            raise WorkflowCopilotTooLargeError() from exc
        # The model sees the declaration with credentials hidden; ``base`` keeps the originals (AC-25).
        declaration = dump(redact(base)[0])
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
            turn_deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_SECONDS
            for attempt in range(1, cls.MAX_ATTEMPTS + 1):
                attempts = attempt
                yield {"type": "status", "stage": "generating" if attempt == 1 else "retrying", "attempt": attempt}
                fence = _FenceFilter()
                answer = shown = ""
                shown_reasoning = 0
                too_long = False
                # Each await on the model gets its own idle deadline (capped by the turn's);
                # the scope guards only that await, never a ``yield``: a timeout scope left open
                # across a yield would fire in the consumer's frame.
                loop = asyncio.get_running_loop()
                wait = FIRST_CHUNK_TIMEOUT_SECONDS
                stream = llm.astream(_as_langchain(messages))
                chunks = stream.__aiter__()
                try:
                    while True:
                        try:
                            async with asyncio.timeout_at(min(loop.time() + wait, turn_deadline)):
                                chunk = await chunks.__anext__()
                            wait = IDLE_TIMEOUT_SECONDS
                        except StopAsyncIteration:
                            break
                        # Reasoning models (MiniMax-M3: ~1 minute of it before the first word of
                        # the answer) stream their thinking beside the content; it is shown in
                        # the drawer like the chat's thinking box, and kept out of the answer, the
                        # answer cap, the reply, the retry feedback and the checked proposal.
                        # The platform's extractor covers every shape a provider uses; markers
                        # are stripped as in the chat's narration. Capped per attempt: past the
                        # cap the model keeps running, the drawer just stops receiving it.
                        reasoning = strip_reasoning_tags(extract_reasoning_content(chunk))
                        if reasoning and shown_reasoning < MAX_REASONING_CHARS:
                            reasoning = reasoning[: MAX_REASONING_CHARS - shown_reasoning]
                            shown_reasoning += len(reasoning)
                            yield {"type": "reasoning", "text": reasoning}
                        text = chunk.content if isinstance(chunk.content, str) else ""
                        answer += text
                        if len(answer) > MAX_ANSWER_CHARS:
                            too_long = True
                            break
                        for kind, piece in fence.feed(text):
                            shown += piece if kind == "text" else ""
                            yield {"type": _STREAM_EVENT[kind], "text": piece}
                    for kind, piece in fence.flush():
                        shown += piece if kind == "text" else ""
                        yield {"type": _STREAM_EVENT[kind], "text": piece}
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except Exception as exc:
                    outcome = "timeout" if isinstance(exc, TimeoutError) else "model_error"
                    yield {
                        "type": "error",
                        "code": WorkflowCopilotModelCallError.Code,
                        "message": f"{WorkflowCopilotModelCallError.Msg}（{type(exc).__name__}）",
                    }
                    return
                finally:
                    # Close what ``astream`` returned (its iterator may not be closable), and
                    # bound the cleanup too: a hanging close must not outlive the deadline.
                    aclose = getattr(stream, "aclose", None) or getattr(chunks, "aclose", None)
                    if aclose is not None:
                        with contextlib.suppress(Exception):
                            async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                                await aclose()

                if too_long:
                    outcome = "too_long"
                    yield {
                        "type": "issues",
                        "issues": [f"模型的回答超过 {MAX_ANSWER_CHARS} 字，已停止：请把需求拆小一些再试"],
                    }
                    return

                # The reply and the block come from the same split the stream showed.
                reply = _tidy(shown)
                block, questions, unparsed = _answer_parts(fence)
                if unparsed:
                    reply = _tidy(reply + "\n\n" + "\n\n".join(unparsed))
                if questions is not None:
                    reply = _tidy(reply + ("\n\n" + _TRIMMED if questions.trimmed else ""))
                if block is None and questions is not None:
                    # F008: the model asks first; the turn ends here, nothing to retry.
                    outcome = "ask"
                    yield {"type": "message", "text": reply}
                    yield {"type": "ask", "questions": questions.items}
                    return
                if questions is not None:  # a proposal came too: it wins, the questions become text
                    reply = _tidy(reply + "\n\n" + questions_text(questions.items))
                if block is None and attempt == 1:
                    outcome = "text"
                    yield {"type": "message", "text": reply}
                    return
                if block is None:
                    issues = ["回答里没有 ```yaml 代码块"]
                else:
                    yield {"type": "status", "stage": "validating", "attempt": attempt}
                    try:
                        proposal, issues = _check(block, turn)
                    except RecursionError:
                        proposal, issues = {}, ["声明嵌套过深，无法处理"]
                    except DslError as error:  # raised past compile (diff / merge): fed back like any issue
                        proposal, issues = {}, [error.message]
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
        spec = yaml.load(block, Loader=_AnswerLoader)  # a SafeLoader subclass: no tags, no aliases
    except _Rejected as exc:
        return {}, [exc.reason]
    except yaml.constructor.ConstructorError:
        return {}, ["不能使用 `!include` 等 YAML 标签：长文本请直接写进 YAML（多行用 `|`）"]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"（第 {mark.line + 1} 行）" if mark is not None else ""
        return {}, [f"YAML 格式错误{where}"]
    if not isinstance(spec, dict) or not isinstance(spec.get("nodes"), list):
        return {}, ["声明必须是一个映射，并且有 nodes 列表"]
    seen: set[Any] = set()
    repeated = []
    for node in spec["nodes"]:
        node_id = node.get("id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or not node_id:
            return {}, [f"每个节点都要有字符串 id（收到 {node_id!r}）"]
        if node_id in seen and node_id not in repeated:
            repeated.append(node_id)
        seen.add(node_id)
    if repeated:
        return {}, [f"节点 id {i} 重复：每个节点的 id 必须唯一" for i in repeated]
    hidden = restore(spec, turn.base)
    if hidden:
        return {}, hidden

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
    issues += _keep_uncarried(definition, turn)
    issues += _reachability(definition, turn.sent)
    if issues:
        return {}, issues

    # A tool node whose tool did not resolve: a new one is left out; an existing one
    # stays exactly as it was on the canvas (decision 4 — never removed by a name miss).
    sent_nodes = {n.get("id"): n for n in turn.sent["nodes"]}
    dropped = set(resolution.unresolved_tools)
    kept_back = {i for i in dropped if i in sent_nodes}
    left_out = dropped - kept_back
    definition["nodes"] = [
        copy.deepcopy(sent_nodes[n["id"]]) if n["id"] in kept_back else n
        for n in definition["nodes"]
        if n["id"] not in left_out
    ]
    definition["edges"] = [
        e for e in definition["edges"] if e.get("source") not in left_out and e.get("target") not in left_out
    ]
    merged = merge(turn.sent, definition)
    present = {n.get("id") for n in merged.canvas["nodes"]}
    needs = []
    for need in resolution.needs_selection:
        if need["node_id"] in kept_back:
            if need["kind"] == "tool":  # the node's other parameters went back with it
                needs.append({**need, "kind": "tool_unchanged"})
        elif need["node_id"] in present or (need["node_id"] in left_out and need["kind"] == "tool"):
            needs.append(need)
    return {
        "canvas": merged.canvas,
        "diff": _masked(merged.diff),
        "needs_selection": needs,
        "replaced": merged.replaced,
    }, []


def _keep_uncarried(definition: dict[str, Any], turn: PreparedTurn) -> list[str]:
    """Sent nodes the declaration could not carry (an unknown type, a tool the user cannot
    list) were never shown to the model, so their absence from the answer is not a
    removal: they go back into the definition as sent, with the sent edges between
    nodes that are still there (AC-08). Issues if the answer reused one of their ids."""
    carried = {n.get("id") for n in turn.base.get("nodes") or [] if isinstance(n, dict)}
    uncarried = {
        n.get("id"): n
        for n in turn.sent["nodes"]
        if n.get("id") not in carried
        and n.get("type") not in _NOTE_TYPES
        and (n.get("data") or {}).get("type") not in _NOTE_TYPES
    }
    if not uncarried:
        return []
    proposed = {n["id"] for n in definition["nodes"]}
    clashes = sorted(str(i) for i in proposed & set(uncarried))
    if clashes:
        return [f"节点 id {i} 已被画布上的其他节点占用，请换一个 id" for i in clashes]
    definition["nodes"] += [copy.deepcopy(n) for n in uncarried.values()]
    present = proposed | set(uncarried)
    definition["edges"] += [
        copy.deepcopy(e)
        for e in turn.sent["edges"]
        if (e.get("source") in uncarried or e.get("target") in uncarried)
        and e.get("source") in present
        and e.get("target") in present
    ]
    return []


def _masked(delta: dict[str, Any]) -> dict[str, Any]:
    """The preview never shows a credential's value (AC-25); the canvas itself keeps it."""
    out = copy.deepcopy(delta)
    for change in out.get("nodes_changed") or []:
        for entry in change.get("params") or []:
            for side in ("online", "local"):
                if side not in entry:
                    continue
                value = entry[side]
                if is_sensitive(entry.get("key")) and value not in (None, "", [], {}):
                    entry[side] = REDACTED
                else:
                    entry[side] = mask(value)
    return out


class _Rejected(yaml.YAMLError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _AnswerLoader(yaml.SafeLoader):
    """``safe_load`` minus aliases (a few lines of ``*ref`` expand to gigabytes) and capped in depth."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise _Rejected("不能使用 YAML 别名（`*名称` / `<<: *名称`）：请把内容直接写出来")
        depth = getattr(self, "_copilot_depth", 0) + 1
        if depth > MAX_YAML_DEPTH:
            raise _Rejected(f"YAML 嵌套超过 {MAX_YAML_DEPTH} 层")
        self._copilot_depth = depth
        try:
            return super().compose_node(parent, index)
        finally:
            self._copilot_depth = depth - 1


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


_STREAM_EVENT = {"text": "delta", "draft": "draft"}
#: F008 limits on a clarification (design §4).
MAX_QUESTIONS = 3
MAX_OPTIONS = 4
MAX_QUESTION_CHARS = 200
_TRIMMED = "（问题较多，只列出前 3 个。）"


def _is_yaml(info: str) -> bool:
    return info.split()[:1] in (["yaml"], ["yml"])


@dataclass
class _Questions:
    items: list[dict[str, Any]]
    trimmed: bool


def _questions(body: str) -> _Questions | None:
    """A clarification block as the drawer's card takes it, or None when it is not one."""
    try:
        loaded = yaml.load(body, Loader=_AnswerLoader)
    except (yaml.YAMLError, RecursionError):
        return None
    if not isinstance(loaded, list):
        return None
    items: list[dict[str, Any]] = []
    trimmed = False
    for raw in loaded:
        if not isinstance(raw, dict) or not isinstance(raw.get("question"), str) or not raw["question"].strip():
            continue
        options: list[str] = []
        for option in raw.get("options") or []:
            text = _one_line(str(option))[:MAX_QUESTION_CHARS] if isinstance(option, (str, int, float)) else ""
            if text and text not in options:
                options.append(text)
        options = options[:MAX_OPTIONS]
        if len(options) < 2:
            continue
        items.append(
            {
                "question": _one_line(raw["question"])[:MAX_QUESTION_CHARS],
                "options": options,
            }
        )
    if not items:
        return None
    if len(items) > MAX_QUESTIONS:
        items, trimmed = items[:MAX_QUESTIONS], True
    # Ids are the server's: the drawer keys its state by them, so a model-chosen id
    # (duplicated, or one like ``__proto__``) never reaches it (G8, F008).
    return _Questions([{"id": f"q{index}", **item} for index, item in enumerate(items, start=1)], trimmed)


def _one_line(text: str) -> str:
    """Whitespace collapsed: model-written text must not forge extra history lines (G8, F008)."""
    return " ".join(text.split())


def _looks_like_questions(body: str) -> bool:
    """A yaml block that is a list of ``question`` items — a clarification, valid or not."""
    try:
        loaded = yaml.load(body, Loader=_AnswerLoader)
    except (yaml.YAMLError, RecursionError):
        return False
    return isinstance(loaded, list) and bool(loaded) and all(isinstance(i, dict) and "question" in i for i in loaded)


def questions_text(items: list[dict[str, Any]]) -> str:
    """The questions as plain text: for a reply that also proposes, and for the history."""
    return "\n".join(f"问：{q['question']}（选项：{' / '.join(q['options'])}）" for q in items)


def _answer_parts(fence: _FenceFilter) -> tuple[str | None, _Questions | None, list[str]]:
    """F008 design §4, in order: questions dressed as yaml are questions; a declaration
    written inside an ask block is still a proposal; the proposal wins over the questions;
    an ask block that does not parse is kept as text (returned in the third slot)."""
    yaml_bodies: list[str] = []
    ask_bodies: list[str] = []
    for info, body in fence.blocks:
        if _is_yaml(info):
            (ask_bodies if _looks_like_questions(body) else yaml_bodies).append(body)
        elif info.split()[:1] == ["ask"]:
            ask_bodies.append(body)
    block = yaml_bodies[0] if yaml_bodies else None
    questions: _Questions | None = None
    unparsed: list[str] = []
    for body in ask_bodies:
        # Cut any nested declaration out first: it is a proposal candidate, and what is left
        # of the body is still the clarification (G8 round 2: the questions must not vanish).
        inner = _FenceFilter()
        rest = "".join(text for kind, text in [*inner.feed(body), *inner.flush()] if kind == "text")
        nested = inner.yaml_block()
        if nested is not None:
            block = block or nested
        source = rest if nested is not None else body
        parsed = _questions(source)
        if parsed is not None:
            questions = questions or parsed
        elif source.strip():
            unparsed.append(source.strip())
    return block, questions, unparsed


_BACKTICKS = re.compile(r"`{3,}")
# What could still turn into a fence once the next chunk comes: indentation and backticks.
_TRAILING_TICKS = re.compile(r" {0,3}`{0,2}\Z")


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _FenceFilter:
    """Splits the streamed answer into ``("text", …)`` outside fences and ``("draft", …)`` inside.

    One rule for the stream, the reply and the block that is validated (G8 on the
    ``draft`` change): a fence is a run of three or more backticks at the start of a line,
    indented by at most three spaces (CommonMark; models fence inside list items), closed by
    a run at least as long, placed the same way; backticks mid-line are text. A yaml block's
    fences and info line (```yaml) go to neither side and its body is the draft; any other
    block stays in the reply verbatim. Each block is kept in ``blocks`` with its info string.
    Indentation and backticks at the end of a chunk that might still become a fence are held
    back until the next chunk decides them, so the split does not depend on where the
    model's chunks happen to break.
    """

    def __init__(self) -> None:
        self._buffer = ""
        # "text" | "info" (the opening fence's line) | "draft" (a yaml block) | "hidden" (an ask
        # block, F008: kept for parsing, never streamed) | "code" (any other)
        self._state = "text"
        self._line_start = True  # whether the buffer starts at the start of a line
        self._open = 0  # the opening run's length while inside a block
        self._fence = ""  # the opening run itself, re-emitted for a non-yaml block
        self._info = ""
        self._body = ""
        self.blocks: list[tuple[str, str]] = []

    def yaml_block(self) -> str | None:
        """The first closed block tagged yaml / yml, as the model wrote it."""
        return next((body for info, body in self.blocks if _is_yaml(info)), None)

    def _indent_start(self, index: int) -> int | None:
        """Where the line holding ``index`` starts, if at most three spaces precede it there."""
        start = index
        while start > 0 and index - start < 3 and self._buffer[start - 1] == " ":
            start -= 1
        at_line_start = self._line_start if start == 0 else self._buffer[start - 1] == "\n"
        return start if at_line_start else None

    def _emit(self, pieces: list[tuple[str, str]], text: str) -> None:
        if not text:
            return
        # Only a yaml block is a draft; an ask block is kept but not streamed; any other block
        # stays in the reply, fences and all.
        if self._state != "hidden":
            pieces.append(("draft" if self._state == "draft" else "text", text))
        if self._state in ("draft", "hidden"):
            self._body += text
        self._line_start = text.endswith("\n")

    def _next_fence(self) -> tuple[re.Match[str] | None, int]:
        """The next fence and where its indentation starts, or None and how much is safe to emit."""
        for run in _BACKTICKS.finditer(self._buffer):
            start = self._indent_start(run.start())
            if start is None:
                continue
            if run.end() == len(self._buffer):  # the run may still grow
                return None, start
            if self._state == "text":
                return run, start
            # A closing fence is long enough and has nothing after it on its line: ```yaml
            # inside a block is content, never the block's end (CommonMark).
            if len(run.group()) < self._open:
                continue
            eol = self._buffer.find("\n", run.end())
            if self._buffer[run.end() : eol if eol >= 0 else None].strip(" \t\r"):
                continue
            if eol < 0:  # the line may still turn out to carry text after the run
                return None, start
            return run, start
        tail = _TRAILING_TICKS.search(self._buffer)
        if tail and tail.group() and self._indent_start(tail.start()) == tail.start():
            return None, tail.start()
        return None, len(self._buffer)

    def _open_block(self, pieces: list[tuple[str, str]], line: str) -> None:
        self._info = line.strip().lower()
        if _is_yaml(self._info):
            self._state, self._line_start, self._body = "draft", True, ""
        elif self._info.split()[:1] == ["ask"]:
            self._state, self._line_start, self._body = "hidden", True, ""
        else:
            self._state = "code"
            self._emit(pieces, self._fence + line)

    def _close_block(self, pieces: list[tuple[str, str]], run: str) -> None:
        if self._state in ("draft", "hidden"):
            self.blocks.append((self._info, self._body))
        else:
            self._emit(pieces, run)
        self._state, self._line_start = "text", False

    def feed(self, text: str) -> list[tuple[str, str]]:
        self._buffer += text
        pieces: list[tuple[str, str]] = []
        while True:
            if self._state == "info":
                end = self._buffer.find("\n")
                if end < 0:
                    return pieces
                line, self._buffer = self._buffer[: end + 1], self._buffer[end + 1 :]
                self._open_block(pieces, line)
                continue
            fence, safe = self._next_fence()
            self._emit(pieces, self._buffer[:safe])
            if fence is None:
                self._buffer = self._buffer[safe:]
                return pieces
            raw, self._buffer = self._buffer[safe : fence.end()], self._buffer[fence.end() :]
            if self._state == "text":
                self._state, self._open, self._fence = "info", len(fence.group()), raw
            else:
                self._close_block(pieces, raw)

    def flush(self) -> list[tuple[str, str]]:
        """What was held back once the stream has ended.

        A held run that closes the open block closes it (answers often end right on the
        fence); a yaml block the stream cut off is still kept for checking, as before the
        split was shared. An unfinished opening fence line is dropped.
        """
        pieces: list[tuple[str, str]] = []
        tail, self._buffer = self._buffer, ""
        inside = self._state in ("draft", "hidden", "code")
        closing = re.fullmatch(r" {0,3}(`+)[ \t]*", tail) if inside and self._line_start else None
        if closing and len(closing.group(1)) >= self._open:
            self._close_block(pieces, tail)
        elif self._state != "info":
            self._emit(pieces, tail)
            if self._state in ("draft", "hidden"):
                self.blocks.append((self._info, self._body))
        return pieces


def _as_langchain(messages: list[dict[str, str]]) -> list[Any]:
    kinds = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [kinds[m["role"]](content=m["content"]) for m in messages]
