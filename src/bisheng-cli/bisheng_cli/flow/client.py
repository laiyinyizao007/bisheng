"""The ``bisheng flow`` client layer: every platform call the flow commands make.

It sits on the existing ``PlatformClient`` (one httpx wrapper, one envelope
parser, one masker at the exit) and adds three things that are specific to
workflows.

**Node templates come from the platform, with a cache that is only a speed-up
(design decision 2, AC-05).** The cache stores the platform's own response
verbatim, keyed by normalised ``base_url``, with the CLI version, the fetch time
and a checksum over the content. An entry is used only while all four still
hold; anything else — expired, another platform, another CLI, content that no
longer matches its checksum, an explicit refresh — goes back to the platform.
The checksum catches corruption and careless hand edits; it is not a tamper
guard (it sits next to the data, and the local user is not a trust boundary). When the
platform cannot answer, the command **fails**: a stale entry is never promoted
to a fallback, because a stale template compiles into a node the platform no
longer recognises and nothing would say so.

The platform publishes no template version, so expiry is by age
(``TEMPLATE_CACHE_TTL``). Templates are a backend constant that only changes
with a platform upgrade; an hour bounds how long an upgrade can go unnoticed,
and ``refresh=True`` removes even that.

**Reading a workflow has three distinct failures (AC-24).** "There is no such
workflow / version", "this key's resource owner may not see it" and "the read
did not complete" each raise with their own exit code and next step. None of
them returns an empty definition: an empty baseline diffs as "everything is
new", which is a confident wrong answer.

**No permission judgement here (design constraint 3).** The CLI never decides
whether a call is allowed; it sends it and translates the platform's verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from bisheng_cli import __version__
from bisheng_cli.credentials import normalise_base_url
from bisheng_cli.errors import (
    EXIT_DEFECT,
    EXIT_FORBIDDEN,
    EXIT_LOCAL_INVALID,
    EXIT_PLATFORM_TOO_OLD,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    EXIT_WAIT_TIMEOUT,
    CliError,
)
from bisheng_cli.flow.compiler import resolve_tool
from bisheng_cli.http import UPLOAD_READ_TIMEOUT, PlatformClient, parse_envelope

TEMPLATES_PATH = "/api/v2/flow/node_templates"
TOOLS_PATH = "/api/v2/tools"
TOOL_SCHEMA_PATH = "/api/v2/tools/schema"
FLOWS_PATH = "/api/v2/flows"
INVOKE_PATH = "/api/v2/workflow/invoke"

#: Seconds a cached template entry is trusted (see the module docstring).
TEMPLATE_CACHE_TTL = 3600
_CACHE_FORMAT = 1
#: A cache file larger than this is a miss, not something to parse (12 templates are ~100 KB).
_CACHE_MAX_BYTES = 16 * 1024 * 1024
#: Default wait for one non-streaming invoke round (below nginx's 300 s, like uploads).
INVOKE_READ_TIMEOUT = UPLOAD_READ_TIMEOUT

#: Platform codes that mean "no such thing" on these endpoints:
#: ``NotFoundError`` (404, workflow) and ``NotFoundVersionError`` (10500).
_MISSING_CODES = frozenset({404, 10500})
#: ``UnAuthorizedError`` (403, the FGA check said no). ``AppWriteAuthError``
#: (10599) is raised only on v1 endpoints, never on the v2 face this client
#: calls, so it is not listed. A missing scope (26003) is forbidden too, but
#: keeps its own next step, which names the scope.
_DENIED_CODES = frozenset({403})
_SCOPE_MISSING = 26003
#: The platform's refusals for a key whose resource owner cannot act
#: (``open_endpoints/domain/utils.py``). They arrive as plain HTTP 404 / 500
#: envelopes, told apart from "no such workflow" only by their message — so they
#: are matched exactly and checked before the 404 is read as "missing" (AC-43).
_OWNER_MISSING_MESSAGES = frozenset(
    {"Open API resource owner is unavailable", "Open API execution has no compatible resource owner"}
)


class FlowClient:
    """Workflow-facing calls on top of one ``PlatformClient``."""

    def __init__(
        self,
        platform: PlatformClient,
        *,
        cache_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._platform = platform
        self._cache_path = Path(cache_path) if cache_path is not None else None
        self._clock = clock
        self._tools: list[dict[str, Any]] | None = None

    def __repr__(self) -> str:
        return f"FlowClient(base_url={self._platform.base_url!r})"

    # ---- node templates --------------------------------------------------------

    def node_templates(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The platform's node templates; cached per platform, never a fallback."""
        key = _cache_key(self._platform.base_url)
        if not refresh:
            cached = self._cached_templates(key)
            if cached is not None:
                return cached
        try:
            data = self._call("GET", TEMPLATES_PATH)
        except CliError as err:
            raise CliError(
                f"取不到节点模板：{err.message}",
                exit_code=err.exit_code,
                code=err.code,
                next_step=(err.next_step + " " if err.next_step else "")
                + "节点模板只从平台取，CLI 不带也不回落到任何副本；平台恢复后重跑。",
                platform_message=err.platform_message,
                details=err.details,
                hints=err.hints,
            ) from err
        if not _templates_well_formed(data):
            raise CliError(
                "取不到节点模板：平台返回的模板不是「带 type 的模板对象」组成的非空列表",
                exit_code=EXIT_DEFECT,
                next_step="这是平台与 CLI 之间的契约缺陷：请带上平台版本与本条输出报平台故障；CLI 不回落到任何模板副本。",
            )
        self._store_templates(key, data)
        return data

    def _cached_templates(self, key: str) -> list[dict[str, Any]] | None:
        store = self._read_cache()
        entry = store.get("entries", {}).get(key) if store else None
        if not isinstance(entry, dict) or not self._entry_fresh(entry):
            return None
        templates = entry.get("templates")
        if not _templates_well_formed(templates) or entry.get("sha256") != _checksum(templates):
            return None
        return templates

    def _entry_fresh(self, entry: dict[str, Any]) -> bool:
        fetched_at = entry.get("fetched_at")
        if entry.get("cli_version") != __version__:
            return False
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            return False
        age = self._clock() - fetched_at
        return 0 <= age < TEMPLATE_CACHE_TTL

    def _read_cache(self) -> dict[str, Any] | None:
        if self._cache_path is None:
            return None
        try:
            if self._cache_path.stat().st_size > _CACHE_MAX_BYTES:
                return None
            store = json.loads(self._cache_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(store, dict) or store.get("format") != _CACHE_FORMAT:
            return None
        if not isinstance(store.get("entries"), dict):
            return None
        return store

    def _store_templates(self, key: str, templates: list[dict[str, Any]]) -> None:
        if self._cache_path is None:
            return
        store = self._read_cache() or {"format": _CACHE_FORMAT, "entries": {}}
        entries = {k: v for k, v in store["entries"].items() if isinstance(v, dict) and self._entry_fresh(v)}
        entries[key] = {
            "cli_version": __version__,
            "fetched_at": self._clock(),
            "sha256": _checksum(templates),
            "templates": templates,
        }
        payload = json.dumps({"format": _CACHE_FORMAT, "entries": entries}, ensure_ascii=False)
        tmp: str | None = None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            # ``.bisheng/`` is committed (it holds the flow binding); its cache is not.
            ignore = self._cache_path.parent / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*\n", "utf-8")
            # A unique sibling per writer: two CLI runs never share a half-written file,
            # and ``os.replace`` (atomic on the same volume) installs a complete one.
            fd, tmp = tempfile.mkstemp(dir=self._cache_path.parent, prefix=self._cache_path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self._cache_path)
            tmp = None
        except OSError as exc:
            # A cache that cannot be written costs one round trip next time; it is
            # not a reason to fail a command whose templates are already in hand.
            if self._platform.emitter is not None:
                self._platform.emitter.debug(f"模板缓存未写入（{exc.__class__.__name__}）：{self._cache_path}")
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # ---- tools -------------------------------------------------------------------

    def tools(self) -> list[dict[str, Any]]:
        """``GET /api/v2/tools`` — fetched once per client."""
        if self._tools is None:
            data = self._read(TOOLS_PATH, "工具列表")
            if not isinstance(data, list) or not all(
                isinstance(t, dict)
                and isinstance(t.get("children") or [], list)
                and all(isinstance(c, dict) for c in t.get("children") or [])
                for t in data
            ):
                raise _shape_error("工具列表")
            self._tools = data
        return self._tools

    def tool_key(self, reference: str) -> str:
        """``operationId`` (or ``type/operationId``) → the ``tool_key`` a node carries."""
        template, issue = resolve_tool(self.tools(), reference)
        if template is not None:
            return template["tool_key"]
        kind = (issue or {}).get("kind")
        if kind == "ambiguous_tool":
            candidates = issue.get("candidates") or []
            raise CliError(
                f"工具 {reference} 有多个同名操作，无法确定是哪一个",
                exit_code=EXIT_LOCAL_INVALID,
                next_step=f"改用「工具类型/操作」的写法，可选：{'、'.join(candidates)}。",
                details={"tool": reference, "candidates": candidates},
            )
        if kind == "tool_without_template":
            raise CliError(
                f"平台没有给出工具 {reference} 的节点模板",
                exit_code=EXIT_DEFECT,
                next_step="这是平台缺陷（工具列表的每个操作都应带 node_template）：请报平台故障。",
                details={"tool": reference},
            )
        raise CliError(
            f"平台上找不到工具 {reference!s}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="用 bisheng flow tool list 查看可用的工具与操作；没注册过就先 bisheng flow tool register。",
            details={"tool": reference},
        )

    # ---- reading workflows -------------------------------------------------------

    def get_flow(self, flow_id: str) -> dict[str, Any]:
        """``GET /api/v2/flows/{id}`` — the workflow row; its ``data`` mirrors the current version."""
        fid = _flow_id(flow_id)
        row = self._read(f"{FLOWS_PATH}/{fid}", f"工作流 {fid}")
        if not isinstance(row, dict):
            raise _shape_error(f"工作流 {fid}")
        return row

    def current_definition(self, flow_id: str) -> dict[str, Any]:
        """The definition the platform runs now (AC-22's default baseline)."""
        fid = _flow_id(flow_id)
        return _definition(self.get_flow(fid).get("data"), f"工作流 {fid}")

    def list_versions(self, flow_id: str) -> list[dict[str, Any]]:
        fid = _flow_id(flow_id)
        page = self._read(f"{FLOWS_PATH}/{fid}/versions", f"工作流 {fid} 的版本列表")
        rows = page.get("data") if isinstance(page, dict) else None
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise _shape_error(f"工作流 {fid} 的版本列表")
        return rows

    def get_version(self, flow_id: str, version_id: int) -> dict[str, Any]:
        fid, vid = _flow_id(flow_id), _version_id(version_id)
        what = f"工作流 {fid} 的版本 {vid}"
        row = self._read(f"{FLOWS_PATH}/{fid}/versions/{vid}", what)
        if not isinstance(row, dict):
            raise _shape_error(what)
        return row

    def version_definition(self, flow_id: str, version_id: int) -> dict[str, Any]:
        """A historical version as a baseline (AC-22 with an explicit version)."""
        fid, vid = _flow_id(flow_id), _version_id(version_id)
        return _definition(self.get_version(fid, vid).get("data"), f"工作流 {fid} 的版本 {vid}")

    # ---- writing -----------------------------------------------------------------

    def create_flow(self, name: str, data: dict[str, Any], *, description: str | None = None) -> dict[str, Any]:
        body = _body(name=name, description=description, data=data)
        row = self._write("POST", FLOWS_PATH, f"工作流 {name}", json=body)
        row = _require_row(row, "id", str, f"新建工作流 {name} 的响应")
        # The platform creates the first version with the workflow and returns its id;
        # without it nothing confirms the definition landed as the current version.
        _require_row(row, "version_id", int, f"新建工作流 {row['id']} 的响应（缺当前版本）")
        return row

    def create_version(
        self, flow_id: str, name: str, data: dict[str, Any], *, description: str | None = None
    ) -> dict[str, Any]:
        """A new version row — never a rewrite of an existing one (AC-25)."""
        fid = _flow_id(flow_id)
        body = _body(name=name, description=description, data=data)
        row = self._write("POST", f"{FLOWS_PATH}/{fid}/versions", f"工作流 {fid}", json=body)
        # Push reports the new version's id and name (AC-29); a reply without them is not a success.
        return _require_row(row, "id", int, f"工作流 {fid} 新建版本的响应")

    def activate_version(self, flow_id: str, version_id: int) -> None:
        fid, vid = _flow_id(flow_id), _version_id(version_id)
        self._write("POST", f"{FLOWS_PATH}/{fid}/versions/{vid}/activate", f"工作流 {fid}")

    def parse_tool_schema(self, file_content: str) -> dict[str, Any]:
        draft = self._write("POST", TOOL_SCHEMA_PATH, "工具", json={"file_content": file_content})
        return _require_row(draft, None, None, "工具解析的响应")

    def register_tool(self, tool_type: dict[str, Any]) -> dict[str, Any]:
        row = self._write("POST", TOOLS_PATH, "工具", json=tool_type)
        return _require_row(row, None, None, "工具注册的响应")

    def invoke(
        self,
        workflow_id: str,
        *,
        session_id: str | None = None,
        message_id: int | None = None,
        inputs: dict[str, Any] | None = None,
        read_timeout: float | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """``POST /api/v2/workflow/invoke``, streamed: one round, to the next pause or the end.

        Returns ``{"session_id", "events"}``; ``on_event`` sees each event as it
        arrives. Streaming is what makes a long round continuable: the session id
        rides on every event, so it is known from the first one — the
        non-streaming reply carried it only once the whole round was over, and a
        round that outlived the read timeout was lost. ``read_timeout`` is the
        longest wait for the *next* event. Running out of it is a timeout of this
        round (the platform may still be executing it) that names the session,
        never "the platform is unreachable".

        A continuation carries **both** ``session_id`` and ``message_id`` — the
        message being the one that is waiting for input. Half of the pair is
        refused here rather than sent.
        """
        if (session_id is None) != (message_id is None):
            raise CliError(
                "续跑必须同时带上会话标识与等待输入的消息标识",
                exit_code=EXIT_USAGE,
                next_step="首次运行两者都不带；续跑时用上一轮返回的 session_id 与停住那条消息的 message_id。",
            )
        wid = _flow_id(workflow_id)
        body: dict[str, Any] = {"workflow_id": wid, "stream": True}
        if session_id is not None:
            body.update(session_id=session_id, message_id=message_id)
        if inputs is not None:
            body["input"] = inputs
        timeout = read_timeout if read_timeout is not None else INVOKE_READ_TIMEOUT
        round_: dict[str, Any] = {"session_id": session_id, "events": []}
        try:
            with self._platform.stream("POST", INVOKE_PATH, json=body, read_timeout=timeout) as resp:
                if "text/event-stream" not in resp.headers.get("content-type", ""):
                    # A refusal (or a platform that ignored ``stream``) is an ordinary envelope.
                    resp.read()
                    data = self._check(resp, "POST", INVOKE_PATH)
                    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
                        raise _shape_error(f"工作流 {wid} 的运行结果")
                    round_["session_id"] = data.get("session_id") or session_id
                    _deliver(data.get("events") or [], round_, on_event, wid)
                    return round_
                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        frame = json.loads(line[len("data:") :].strip())
                    except ValueError:
                        raise _shape_error(f"工作流 {wid} 的运行事件") from None
                    if not isinstance(frame, dict):
                        raise _shape_error(f"工作流 {wid} 的运行事件")
                    round_["session_id"] = frame.get("session_id") or round_["session_id"]
                    payload = frame.get("data")
                    _deliver(payload if isinstance(payload, list) else [payload], round_, on_event, wid)
        except CliError as err:
            raise _run_failure(err, wid, timeout) from err
        except httpx.TimeoutException:
            raise _stalled(wid, timeout, round_["session_id"]) from None
        except httpx.HTTPError as exc:
            raise CliError(
                f"运行工作流 {wid} 时与平台的连接中断（{exc.__class__.__name__}）",
                exit_code=EXIT_UNREACHABLE,
                next_step="工作流可能仍在平台上执行；稍后在平台上查看这次会话，或重新运行。",
                details={"session_id": round_["session_id"]},
            ) from None
        return round_

    # ---- plumbing ----------------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        """One request, envelope parsed — with a bare 404 told apart from "not found".

        Every v2 endpoint here answers "no such workflow / version" *inside* the
        envelope. A 404 without one means the route itself is missing: a platform
        that predates these endpoints, or a base URL pointing somewhere else. Those
        are not "check your workflow id", so they must not read like it.
        """
        return self._check(self._platform.request(method, path, **kwargs), method, path)

    def _check(self, resp: httpx.Response, method: str, path: str) -> Any:
        envelope = _envelope(resp)
        if envelope is not None and (
            isinstance(envelope["status_code"], bool) or not isinstance(envelope["status_code"], int)
        ):
            # ``parse_envelope`` only treats an *int* non-200 as a failure; anything
            # else would read as success. Here it must not: a write that "succeeded"
            # with no verdict is how a refused push gets reported as done.
            raise CliError(
                f"平台响应无法判定成败（{method} {path}）：信封里的 status_code 不是整数",
                exit_code=EXIT_DEFECT,
                platform_message=str(envelope.get("status_message") or ""),
                details=envelope.get("data"),
                next_step="这是平台与 CLI 之间的契约缺陷：请带上本条输出报平台故障；不要假定这次操作已生效。",
            )
        if resp.status_code == 404 and envelope is None:
            raise CliError(
                f"平台上没有这个接口（{method} {path}）",
                exit_code=EXIT_PLATFORM_TOO_OLD,
                code=404,
                next_step="确认平台地址指向毕昇本体（不是网关的其它路径），且平台版本已包含工作流即代码的 v2 接口；否则升级平台。",
            )
        return parse_envelope(resp)

    def _read(self, path: str, what: str) -> Any:
        try:
            return self._call("GET", path)
        except CliError as err:
            raise _read_failure(err, what) from err

    def _write(self, method: str, path: str, what: str, **kwargs: Any) -> Any:
        try:
            return self._call(method, path, **kwargs)
        except CliError as err:
            raise _write_failure(err, what) from err


# ---- helpers -------------------------------------------------------------------


def _flow_id(raw: Any) -> str:
    """A workflow id as the platform stores it (32 hex), or a usage error.

    It becomes a URL path segment, so it is parsed, not interpolated: ``../``
    or a stray slash never reaches the request line.
    """
    try:
        return uuid.UUID(str(raw)).hex
    except ValueError:
        raise CliError(
            f"工作流标识 {raw!r} 不是合法的工作流 id",
            exit_code=EXIT_USAGE,
            next_step="工作流 id 是 32 位十六进制（可带连字符），可在平台工作流详情页的地址栏里找到。",
        ) from None


def _version_id(raw: Any) -> int:
    text = str(raw)
    # ``isdigit`` accepts "²"; ``int("²")`` does not. ASCII decimal digits only.
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, str))
        or not (text.isascii() and text.isdecimal())
        or int(text) <= 0
    ):
        raise CliError(
            f"版本标识 {raw!r} 不是正整数",
            exit_code=EXIT_USAGE,
            next_step="用 bisheng flow diff --list-versions 查看可用的版本 id。",
        )
    return int(text)


#: Public spellings for the command layer, which records ids in the binding and in output.
parse_flow_id = _flow_id
parse_version_id = _version_id


def _cache_key(base_url: str) -> str:
    """The cache's per-platform key: a digest, so no address (or userinfo in one) lands in the file."""
    return hashlib.sha256(normalise_base_url(base_url).encode("utf-8")).hexdigest()


def _require_row(row: Any, key: str | None, kind: type | None, what: str) -> dict[str, Any]:
    if not isinstance(row, dict) or (
        key is not None and (isinstance(row.get(key), bool) or not isinstance(row.get(key), kind))
    ):
        raise CliError(
            f"{what}形状不对，无法确认操作已生效",
            exit_code=EXIT_DEFECT,
            next_step="这是平台与 CLI 之间的契约缺陷：请带上本条输出报平台故障；先到平台上核对这次操作是否已生效，再决定是否重试。",
        )
    return row


def _body(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def _definition(data: Any, what: str) -> dict[str, Any]:
    """``{nodes, edges, …}`` from a stored ``data``; ``None`` is a genuinely empty canvas."""
    if data is None:
        return {"nodes": [], "edges": []}
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
        raise _shape_error(what)
    if data.get("edges") is None:
        return {**data, "edges": []}
    if not isinstance(data["edges"], list):
        raise _shape_error(what)
    return data


def _templates_well_formed(data: Any) -> bool:
    return (
        isinstance(data, list)
        and bool(data)
        and all(isinstance(t, dict) and isinstance(t.get("type"), str) and t["type"] for t in data)
    )


def _checksum(templates: Any) -> str:
    canonical = json.dumps(templates, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _shape_error(what: str) -> CliError:
    return CliError(
        f"读取{what}失败：平台返回的数据形状不对",
        exit_code=EXIT_DEFECT,
        next_step="这是平台与 CLI 之间的契约缺陷：请带上平台版本与本条输出报平台故障；重试不会改变结果。",
    )


def _carry(err: CliError, message: str, exit_code: int, next_step: str) -> CliError:
    return CliError(
        message,
        exit_code=exit_code,
        code=err.code,
        next_step=next_step,
        platform_message=err.platform_message,
        details=err.details,
        hints=err.hints,
    )


def _envelope(resp: Any) -> dict[str, Any] | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) and "status_code" in body else None


def _deliver(
    events: list[Any], round_: dict[str, Any], on_event: Callable[[dict[str, Any]], None] | None, wid: str
) -> None:
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise _shape_error(f"工作流 {wid} 的运行事件")
        round_["events"].append(event)
        if on_event is not None:
            on_event(event)


def _stalled(wid: str, timeout: float, session_id: str | None) -> CliError:
    where = f"（会话 {session_id}）" if session_id else ""
    return CliError(
        f"运行工作流 {wid} 超时：{timeout:g} 秒内平台没有发来新的事件{where}，工作流可能仍在平台上执行",
        exit_code=EXIT_WAIT_TIMEOUT,
        next_step="单个节点执行得久时用 --wait-timeout 调大等待；也可以先到平台上查看这次会话的结果。",
        details={"session_id": session_id},
    )


def _owner_missing(err: CliError) -> CliError:
    return _carry(
        err,
        "这把密钥没有可用的资源归属人，平台拒绝以它的名义读写或运行工作流",
        EXIT_FORBIDDEN,
        "请平台管理员为该服务账号设置一位仍然有效的资源归属人（或改用有归属人的密钥）；工作流本身没有问题，不要重新新建。",
    )


def _run_failure(err: CliError, wid: str, timeout: float) -> CliError:
    """Invoke's own taxonomy: running needs the ``use`` permission, and a timeout is not "unreachable"."""
    if isinstance(err.__context__, httpx.TimeoutException) and not isinstance(err.__context__, httpx.ConnectTimeout):
        return _stalled(wid, timeout, None)
    if err.exit_code == EXIT_PLATFORM_TOO_OLD:
        return err
    if err.platform_message in _OWNER_MISSING_MESSAGES:
        return _owner_missing(err)
    if err.code in _MISSING_CODES:
        return _carry(
            err,
            f"线上不存在工作流 {wid}",
            EXIT_LOCAL_INVALID,
            "核对工作流 id（--flow-id 或 .bisheng/flow.json 里的绑定）。",
        )
    if err.code in _DENIED_CODES:
        return _carry(
            err,
            f"无权运行工作流 {wid}",
            EXIT_FORBIDDEN,
            "CLI 以这把密钥的资源归属人身份运行；该归属人需要对这个工作流有「使用」权限——请归属人或管理员授权，或改用归属人正确的密钥。",
        )
    if err.code == _SCOPE_MISSING:
        return _carry(err, f"无权运行工作流 {wid}：{err.message}", EXIT_FORBIDDEN, err.next_step)
    return err


def _read_failure(err: CliError, what: str) -> CliError:
    if err.exit_code == EXIT_PLATFORM_TOO_OLD:
        return err
    if err.platform_message in _OWNER_MISSING_MESSAGES:
        return _owner_missing(err)
    if err.code in _MISSING_CODES:
        return _carry(
            err,
            f"线上不存在{what}",
            EXIT_LOCAL_INVALID,
            "核对工作流 id（--flow-id 或 .bisheng/flow.json 里的绑定）与版本 id；工作流被删除后要重新 push --create。",
        )
    if err.code in _DENIED_CODES:
        return _carry(
            err,
            f"无权读取{what}",
            EXIT_FORBIDDEN,
            "CLI 以这把密钥的资源归属人身份读取；请工作流的归属人或管理员把该工作流授权给这位归属人，或改用归属人正确的密钥。",
        )
    if err.code == _SCOPE_MISSING or err.exit_code == EXIT_FORBIDDEN:
        return _carry(err, f"无权读取{what}：{err.message}", EXIT_FORBIDDEN, err.next_step)
    return _carry(
        err,
        f"读取{what}失败：{err.message}",
        err.exit_code,
        err.next_step or "稍后重跑同一条命令；持续失败请带上本条输出联系平台管理员。",
    )


def _write_failure(err: CliError, what: str) -> CliError:
    if err.exit_code == EXIT_PLATFORM_TOO_OLD:
        return err
    if err.platform_message in _OWNER_MISSING_MESSAGES:
        return _owner_missing(err)
    if err.code in _MISSING_CODES:
        return _carry(
            err,
            f"线上不存在{what}（或其中的版本）",
            EXIT_LOCAL_INVALID,
            "核对工作流 id 与版本 id；工作流被删除后要重新 push --create。",
        )
    if err.code in _DENIED_CODES:
        return _carry(
            err,
            f"无权修改{what}",
            EXIT_FORBIDDEN,
            "CLI 以这把密钥的资源归属人身份写入；该归属人需要对这个工作流有编辑权限——请归属人或管理员授权，或改用归属人正确的密钥。",
        )
    if err.code == _SCOPE_MISSING:
        return _carry(err, f"无权修改{what}：{err.message}", EXIT_FORBIDDEN, err.next_step)
    return err
