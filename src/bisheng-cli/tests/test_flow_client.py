"""T026 — the ``bisheng flow`` client layer (``bisheng_cli/flow/client.py``).

What this file pins, in the order a reader is likely to doubt it:

* **The template cache is a speed-up, never a copy (AC-05).** A fresh entry
  saves the round trip; anything else — expired, another platform, another CLI
  version, content that no longer matches its own checksum, an explicit
  refresh — goes back to the platform, and when the platform cannot answer the
  command fails. A stale entry is never served as a fallback.
* **"Not there", "not yours" and "could not read" are three answers (AC-24).**
  Each raises with its own exit code and next step; none of them may ever look
  like an empty workflow, because an empty baseline diffs as "everything is
  new" — or, worse, an empty local side diffs as "no differences".
* **Platform errors keep their payload (AC-28).** ``details`` / ``hints`` reach
  the five-tuple untouched; a missing scope names the scope.
* **The key never leaves through a side door (AC-41).** Not into the cache
  file, not into an error message, not into the emitted output.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import httpx
import pytest

from bisheng_cli import __version__
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
from bisheng_cli.flow import client as client_module
from bisheng_cli.flow.client import TEMPLATE_CACHE_TTL, FlowClient
from bisheng_cli.http import PlatformClient
from bisheng_cli.output import Emitter
from tests.helpers.flow_fixtures import templates, tool_child, tools
from tests.helpers.platform_mock import FAKE_KEY, PlatformMock, v2_error, v2_ok

BASE = "http://platform.test"
FLOW_ID = "1194f8540b9944fea4353b07f0493cbf"
TEMPLATES_PATH = "/api/v2/flow/node_templates"
TOOLS_PATH = "/api/v2/tools"
FLOW_PATH = f"/api/v2/flows/{FLOW_ID}"
VERSIONS_PATH = f"/api/v2/flows/{FLOW_ID}/versions"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _client(mock: PlatformMock, cache: Path | None = None, *, base: str = BASE, clock: Clock | None = None):
    platform = PlatformClient(base, FAKE_KEY, transport=mock.transport)
    return FlowClient(platform, cache_path=cache, clock=clock or Clock())


def _count(mock: PlatformMock, path: str) -> int:
    return mock.paths_called().count(path)


def _definition(*node_ids: str) -> dict:
    return {
        "nodes": [{"id": i, "type": "flowNode", "position": {"x": 0, "y": 0}, "data": {"id": i}} for i in node_ids],
        "edges": [],
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }


# ---- node templates: cache ------------------------------------------------------


def test_templates_cached_and_invalidated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / ".bisheng" / "cache" / "node_templates.json"
    mock = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    clock = Clock()

    first = _client(mock, cache, clock=clock).node_templates()
    assert first == templates()
    assert _count(mock, TEMPLATES_PATH) == 1
    assert cache.is_file(), "a successful fetch must populate the cache"

    # A fresh entry is served without touching the platform — by a *new* client,
    # so this is the file cache and not an in-memory memo.
    assert _client(mock, cache, clock=clock).node_templates() == templates()
    assert _count(mock, TEMPLATES_PATH) == 1

    # Still inside the TTL: no request.
    clock.now += TEMPLATE_CACHE_TTL - 1
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 1

    # Past it: refetched.
    clock.now += 2
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 2

    # Another platform never reads this platform's entry.
    other = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    _client(other, cache, base="http://other.test", clock=clock).node_templates()
    assert _count(other, TEMPLATES_PATH) == 1
    # ...and adding it did not evict the first platform's entry.
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 2

    # Spelling of the base URL does not matter (same normalisation as credentials).
    _client(mock, cache, base=BASE.upper() + "/", clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 2

    # An explicit refresh bypasses a fresh entry.
    _client(mock, cache, clock=clock).node_templates(refresh=True)
    assert _count(mock, TEMPLATES_PATH) == 3

    # A different CLI version does not trust the entry.
    monkeypatch.setattr(client_module, "__version__", __version__ + ".dev9")
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 4
    monkeypatch.undo()
    # Back on the real version: that entry (written by ".dev9") is not trusted
    # either, so this refetches and leaves a fresh entry for the next step.
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 5
    _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 5, "the entry is fresh again"

    # Content edited behind the CLI's back (checksum no longer matches) is refetched,
    # and the edit is never served. Only the content changes: version and age are
    # still valid, so the checksum is the only thing that can catch it.
    store = json.loads(cache.read_text("utf-8"))
    for entry in store["entries"].values():
        entry["templates"][0]["type"] = "tampered"
    cache.write_text(json.dumps(store), "utf-8")
    served = _client(mock, cache, clock=clock).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 6
    assert all(t["type"] != "tampered" for t in served)

    # A corrupt file is a miss, not a crash.
    cache.write_text("{not json", "utf-8")
    assert _client(mock, cache, clock=clock).node_templates() == templates()
    assert _count(mock, TEMPLATES_PATH) == 7


def test_no_cache_path_means_every_call_fetches() -> None:
    mock = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    client = _client(mock, None)
    client.node_templates()
    client.node_templates()
    assert _count(mock, TEMPLATES_PATH) == 2


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(v2_error(26030, "auth down", http_status=503), id="platform-503"),
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
        pytest.param(v2_ok([]), id="empty-list"),
        pytest.param(v2_ok({"nodes": []}), id="not-a-list"),
        pytest.param(v2_ok([{"name": "no type"}]), id="entry-without-type"),
    ],
)
def test_template_fetch_failure_has_no_fallback(tmp_path: Path, failure) -> None:
    cache = tmp_path / "node_templates.json"
    clock = Clock()
    # Seed a valid entry, then let it expire.
    _client(PlatformMock().get(TEMPLATES_PATH, v2_ok(templates())), cache, clock=clock).node_templates()
    clock.now += TEMPLATE_CACHE_TTL + 1

    mock = PlatformMock().get(TEMPLATES_PATH, failure)
    with pytest.raises(CliError) as caught:
        _client(mock, cache, clock=clock).node_templates()
    assert caught.value.exit_code in {EXIT_UNREACHABLE, EXIT_DEFECT}
    assert "模板" in caught.value.message
    assert caught.value.next_step, "a failure must say what to do next"

    # And with no cache at all, the same failure is a failure too.
    with pytest.raises(CliError):
        _client(PlatformMock().get(TEMPLATES_PATH, failure), None).node_templates()


def test_malformed_templates_are_not_cached(tmp_path: Path) -> None:
    cache = tmp_path / "node_templates.json"
    with pytest.raises(CliError):
        _client(PlatformMock().get(TEMPLATES_PATH, v2_ok([{"name": "x"}])), cache).node_templates()
    assert not cache.exists()


def test_package_ships_no_template_copy() -> None:
    """AC-05 from the other side: there is nothing to fall back *to*."""
    package = Path(client_module.__file__).resolve().parents[1]
    data_files = [p for p in package.rglob("*") if p.is_file() and p.suffix in {".json", ".yaml", ".yml"}]
    assert not [p for p in data_files if "group_params" in p.read_text("utf-8", errors="ignore")]


# ---- tools ----------------------------------------------------------------------


def test_operation_id_resolves_to_tool_key() -> None:
    catalogue = tools()
    catalogue.append({"id": 4, "name": "other", "children": [tool_child("get_feedback", "key_x", [], child_id=9)]})
    mock = PlatformMock().get(TOOLS_PATH, v2_ok(catalogue))
    client = _client(mock)

    assert client.tool_key("submit_candidate") == "key_submit"
    assert client.tool_key("invest/get_feedback") == "key_feedback"
    assert client.tool_key("other/get_feedback") == "key_x"
    assert _count(mock, TOOLS_PATH) == 1, "the tool list is fetched once per client"

    with pytest.raises(CliError) as unknown:
        client.tool_key("nope")
    assert unknown.value.exit_code == EXIT_LOCAL_INVALID
    assert "nope" in unknown.value.message

    with pytest.raises(CliError) as ambiguous:
        client.tool_key("get_feedback")
    assert ambiguous.value.exit_code == EXIT_LOCAL_INVALID
    assert set(ambiguous.value.details["candidates"]) == {"invest/get_feedback", "other/get_feedback"}


def test_tools_list_shape_is_checked() -> None:
    with pytest.raises(CliError) as caught:
        _client(PlatformMock().get(TOOLS_PATH, v2_ok({"children": []}))).tools()
    assert caught.value.exit_code == EXIT_DEFECT


# ---- reading a workflow: three distinguishable failures -------------------------


def test_reads_current_definition_from_flow() -> None:
    mock = PlatformMock().get(FLOW_PATH, v2_ok({"id": FLOW_ID, "name": "研究", "data": _definition("a", "b")}))
    definition = _client(mock).current_definition(FLOW_ID)
    assert [n["id"] for n in definition["nodes"]] == ["a", "b"]


def test_flow_with_no_data_reads_as_empty_definition() -> None:
    """A workflow created without a canvas genuinely has no nodes — that is a baseline, not a failure."""
    mock = PlatformMock().get(FLOW_PATH, v2_ok({"id": FLOW_ID, "name": "空", "data": None}))
    assert _client(mock).current_definition(FLOW_ID) == {"nodes": [], "edges": []}


def test_reads_historical_version() -> None:
    path = f"{VERSIONS_PATH}/42"
    mock = PlatformMock().get(path, v2_ok({"id": 42, "flow_id": FLOW_ID, "name": "v3", "data": _definition("x")}))
    definition = _client(mock).version_definition(FLOW_ID, 42)
    assert [n["id"] for n in definition["nodes"]] == ["x"]


def test_lists_versions() -> None:
    rows = [{"id": 1, "name": "v0", "is_current": 0}, {"id": 2, "name": "v1", "is_current": 1}]
    mock = PlatformMock().get(VERSIONS_PATH, v2_ok({"data": rows, "total": 3}))
    assert _client(mock).list_versions(FLOW_ID) == rows


@pytest.mark.parametrize(
    ("response", "exit_code", "word"),
    [
        # The platform answers "not found" in the body with HTTP 200 (``raise NotFoundError()``)...
        pytest.param(
            v2_error(404, "This resource does not exist", http_status=200),
            EXIT_LOCAL_INVALID,
            "不存在",
            id="missing-body",
        ),
        # ...or with a real 404 *and* the envelope (the open-API handler's status mapping).
        pytest.param(
            v2_error(404, "This resource does not exist", http_status=404),
            EXIT_LOCAL_INVALID,
            "不存在",
            id="missing-http",
        ),
        pytest.param(
            v2_error(403, "No permission to operate", http_status=200), EXIT_FORBIDDEN, "无权", id="forbidden-body"
        ),
        pytest.param(
            v2_error(26003, "scope missing", required="workflow:read"), EXIT_FORBIDDEN, "无权", id="forbidden-scope"
        ),
        pytest.param(v2_error(26030, "auth down", http_status=503), EXIT_UNREACHABLE, "读取", id="failed-503"),
        pytest.param(httpx.ConnectError("refused"), EXIT_UNREACHABLE, "读取", id="failed-unreachable"),
        pytest.param(v2_ok({"id": FLOW_ID, "data": {"nodes": "x"}}), EXIT_DEFECT, "读取", id="failed-malformed"),
        pytest.param(v2_ok(None), EXIT_DEFECT, "读取", id="failed-no-payload"),
    ],
)
def test_missing_workflow_distinguished_from_forbidden(response, exit_code: int, word: str) -> None:
    mock = PlatformMock().get(FLOW_PATH, response)
    with pytest.raises(CliError) as caught:
        _client(mock).current_definition(FLOW_ID)
    err = caught.value
    assert err.exit_code == exit_code
    assert word in err.message
    assert FLOW_ID in err.message, "the reader has to see which workflow"
    assert err.next_step


@pytest.mark.parametrize(
    "bare",
    [
        pytest.param(httpx.Response(404, text="<html>404 Not Found</html>"), id="html"),
        # What FastAPI itself answers for an unmounted route: JSON, but not an envelope.
        pytest.param(httpx.Response(404, json={"detail": "Not Found"}), id="fastapi-json"),
    ],
)
@pytest.mark.parametrize("call", ["read", "write", "templates"])
def test_bare_404_is_a_missing_route_not_a_missing_workflow(call: str, bare: httpx.Response) -> None:
    """No envelope = the route is not there (old platform / wrong base URL), not "check your id"."""
    mock = PlatformMock().get(FLOW_PATH, bare).post(VERSIONS_PATH, bare).get(TEMPLATES_PATH, bare)
    client = _client(mock)
    action = {
        "read": lambda: client.current_definition(FLOW_ID),
        "write": lambda: client.create_version(FLOW_ID, "v", _definition()),
        "templates": lambda: client.node_templates(),
    }[call]
    with pytest.raises(CliError) as caught:
        action()
    assert caught.value.exit_code == EXIT_PLATFORM_TOO_OLD
    assert "不存在" not in caught.value.message
    assert "接口" in caught.value.message


def test_v1_only_code_keeps_the_global_translation() -> None:
    """10599 is raised on v1 endpoints only, so the client adds no flow-specific wording.

    It still reads as "no write permission" through the global table (T041).
    """
    mock = PlatformMock().post(VERSIONS_PATH, v2_error(10599, "no write", http_status=200))
    with pytest.raises(CliError) as caught:
        _client(mock).create_version(FLOW_ID, "v", _definition())
    assert caught.value.code == 10599
    assert caught.value.exit_code == EXIT_FORBIDDEN
    assert "无权修改" not in caught.value.message


def test_concurrent_cache_writers_never_share_a_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each write goes through its own temp file, and none is left behind."""
    cache = tmp_path / "cache" / "node_templates.json"
    seen: list[str] = []
    real = client_module.os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real(src, dst)

    monkeypatch.setattr(client_module.os, "replace", spy)
    mock = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    _client(mock, cache).node_templates(refresh=True)
    _client(mock, cache).node_templates(refresh=True)
    assert len(seen) == 2 and len(set(seen)) == 2
    assert sorted(p.name for p in cache.parent.iterdir()) == [".gitignore", "node_templates.json"]


def test_failed_cache_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "cache" / "node_templates.json"

    def boom(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(client_module.os, "replace", boom)
    served = _client(PlatformMock().get(TEMPLATES_PATH, v2_ok(templates())), cache).node_templates()
    assert served == templates(), "a cache that cannot be written does not fail the command"
    assert [p.name for p in cache.parent.iterdir()] == [".gitignore"]


def test_tool_children_must_be_objects() -> None:
    with pytest.raises(CliError) as caught:
        _client(PlatformMock().get(TOOLS_PATH, v2_ok([{"name": "t", "children": ["x"]}]))).tool_key("x")
    assert caught.value.exit_code == EXIT_DEFECT


@pytest.mark.parametrize("status", ["500", None, 200.0, True])
def test_envelope_without_int_verdict_is_never_success(status) -> None:
    """``parse_envelope`` would return ``data`` for these; a write that "succeeded" with no verdict is a lie."""
    response = httpx.Response(200, json={"status_code": status, "status_message": "boom", "data": None})
    mock = PlatformMock().post(VERSIONS_PATH, response).get(FLOW_PATH, response)
    for action in (
        lambda c: c.create_version(FLOW_ID, "v", _definition()),
        lambda c: c.current_definition(FLOW_ID),
    ):
        with pytest.raises(CliError) as caught:
            action(_client(mock))
        assert caught.value.exit_code == EXIT_DEFECT


@pytest.mark.parametrize(
    ("call", "row"),
    [
        ("create_version", None),
        ("create_version", {"name": "v"}),
        ("create_version", {"id": "43", "name": "v"}),
        ("create_version", {"id": True, "name": "v"}),
        ("create_flow", {"name": "x"}),
        # the platform returns the new workflow with its current version (FlowService.create_workflow)
        ("create_flow", {"id": FLOW_ID, "name": "x"}),
        ("parse_tool_schema", [1]),
        ("register_tool", None),
    ],
)
def test_write_reply_without_its_identity_is_not_success(call: str, row) -> None:
    mock = (
        PlatformMock()
        .post(VERSIONS_PATH, v2_ok(row))
        .post("/api/v2/flows", v2_ok(row))
        .post("/api/v2/tools/schema", v2_ok(row))
        .post("/api/v2/tools", v2_ok(row))
    )
    client = _client(mock)
    action = {
        "create_version": lambda: client.create_version(FLOW_ID, "v", _definition()),
        "create_flow": lambda: client.create_flow("x", _definition()),
        "parse_tool_schema": lambda: client.parse_tool_schema("openapi: 3.0.0"),
        "register_tool": lambda: client.register_tool({"name": "t"}),
    }[call]
    with pytest.raises(CliError) as caught:
        action()
    assert caught.value.exit_code == EXIT_DEFECT


def test_invoke_timeout_is_a_round_timeout_not_unreachable() -> None:
    mock = PlatformMock().post("/api/v2/workflow/invoke", httpx.ReadTimeout("slow"))
    with pytest.raises(CliError) as caught:
        _client(mock).invoke(FLOW_ID, read_timeout=5)
    assert caught.value.exit_code == EXIT_WAIT_TIMEOUT
    assert "超时" in caught.value.message and "不可达" not in caught.value.message
    assert "5" in caught.value.message

    # A connect failure is still "unreachable".
    mock = PlatformMock().post("/api/v2/workflow/invoke", httpx.ConnectError("refused"))
    with pytest.raises(CliError) as caught:
        _client(mock).invoke(FLOW_ID)
    assert caught.value.exit_code == EXIT_UNREACHABLE


def test_invoke_timeout_is_passed_to_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    mock = PlatformMock().post("/api/v2/workflow/invoke", v2_ok({"session_id": "s", "events": []}))
    client = _client(mock)
    real = client._platform.stream

    def spy(method, path, **kwargs):
        seen.append(kwargs.get("read_timeout"))
        return real(method, path, **kwargs)

    monkeypatch.setattr(client._platform, "stream", spy)
    client.invoke(FLOW_ID)
    client.invoke(FLOW_ID, read_timeout=900)
    assert seen == [client_module.INVOKE_READ_TIMEOUT, 900]


@pytest.mark.parametrize(
    ("response", "exit_code", "phrase"),
    [
        (v2_error(403, "No permission to operate", http_status=403), EXIT_FORBIDDEN, "使用"),
        (v2_error(404, "This resource does not exist", http_status=404), EXIT_LOCAL_INVALID, "不存在"),
    ],
)
def test_invoke_failures_speak_about_running(response, exit_code: int, phrase: str) -> None:
    mock = PlatformMock().post("/api/v2/workflow/invoke", response)
    with pytest.raises(CliError) as caught:
        _client(mock).invoke(FLOW_ID)
    err = caught.value
    assert err.exit_code == exit_code
    assert phrase in err.message + err.next_step
    assert "修改" not in err.message and "编辑" not in err.next_step and "push --create" not in err.next_step


@pytest.mark.parametrize("bad", ["²", "١٢", "-1", "0", "1.5", True])
def test_version_id_is_ascii_positive_integer(bad) -> None:
    with pytest.raises(CliError) as caught:
        _client(PlatformMock()).version_definition(FLOW_ID, bad)
    assert caught.value.exit_code == EXIT_USAGE


def test_null_edges_read_as_none() -> None:
    mock = PlatformMock().get(FLOW_PATH, v2_ok({"id": FLOW_ID, "data": {"nodes": [], "edges": None}}))
    assert _client(mock).current_definition(FLOW_ID)["edges"] == []


def test_cache_file_holds_no_address_and_ignores_itself(tmp_path: Path) -> None:
    cache = tmp_path / ".bisheng" / "cache" / "node_templates.json"
    secret = "s3cr3t-in-url"
    platform = PlatformClient(
        f"https://dev:{secret}@platform.test",
        FAKE_KEY,
        transport=PlatformMock().get(TEMPLATES_PATH, v2_ok(templates())).transport,
    )
    FlowClient(platform, cache_path=cache, clock=Clock()).node_templates()
    text = cache.read_text("utf-8")
    assert secret not in text and "platform.test" not in text
    assert (cache.parent / ".gitignore").read_text("utf-8") == "*\n"


def test_oversized_cache_file_is_a_miss(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "node_templates.json"
    mock = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    _client(mock, cache).node_templates()
    monkeypatch.setattr(client_module, "_CACHE_MAX_BYTES", 10)
    _client(mock, cache).node_templates()
    assert _count(mock, TEMPLATES_PATH) == 2


def test_three_read_failures_have_three_next_steps() -> None:
    answers = {
        "missing": v2_error(404, "This resource does not exist", http_status=200),
        "forbidden": v2_error(403, "No permission to operate", http_status=200),
        "failed": v2_error(26030, "auth down", http_status=503),
    }
    seen = {}
    for name, response in answers.items():
        with pytest.raises(CliError) as caught:
            _client(PlatformMock().get(FLOW_PATH, response)).current_definition(FLOW_ID)
        seen[name] = (caught.value.exit_code, caught.value.next_step)
    assert len({code for code, _ in seen.values()}) == 3
    assert len({step for _, step in seen.values()}) == 3


@pytest.mark.parametrize("code", [404, 403, 26003, 26030])
def test_translated_read_failures_keep_the_payload(code: int) -> None:
    """Translating the sentence must not drop what the platform sent — it is the agent's repair input."""
    data = {"exception": "x", "required": "workflow:read", "hints": ["平台给的提示"]}
    response = httpx.Response(200, json={"status_code": code, "status_message": "platform says", "data": data})
    with pytest.raises(CliError) as caught:
        _client(PlatformMock().get(FLOW_PATH, response)).current_definition(FLOW_ID)
    failure = caught.value.as_failure("diff")
    assert failure["code"] == code
    assert failure["details"] == data
    assert failure["hints"] == ["平台给的提示"]
    assert failure["message"] == "platform says"


def test_missing_scope_names_the_scope() -> None:
    mock = PlatformMock().get(FLOW_PATH, v2_error(26003, "scope missing", required="workflow:read"))
    with pytest.raises(CliError) as caught:
        _client(mock).current_definition(FLOW_ID)
    assert "workflow:read" in caught.value.next_step
    assert caught.value.code == 26003


def test_missing_version_is_not_found() -> None:
    mock = PlatformMock().get(f"{VERSIONS_PATH}/9", v2_error(10500, "version not found", http_status=200))
    with pytest.raises(CliError) as caught:
        _client(mock).version_definition(FLOW_ID, 9)
    assert caught.value.exit_code == EXIT_LOCAL_INVALID
    assert "版本" in caught.value.message and "9" in caught.value.message
    assert caught.value.code == 10500


@pytest.mark.parametrize("bad", ["../../etc", "1194f854/../x", "", "not-a-uuid"])
def test_flow_id_must_be_a_workflow_id(bad: str) -> None:
    """A path segment built from user input is checked before it becomes a URL."""
    with pytest.raises(CliError) as caught:
        _client(PlatformMock()).current_definition(bad)
    assert caught.value.exit_code == EXIT_USAGE


def test_dashed_uuid_is_normalised() -> None:
    dashed = f"{FLOW_ID[:8]}-{FLOW_ID[8:12]}-{FLOW_ID[12:16]}-{FLOW_ID[16:20]}-{FLOW_ID[20:]}"
    mock = PlatformMock().get(FLOW_PATH, v2_ok({"id": FLOW_ID, "data": _definition()}))
    _client(mock).current_definition(dashed)
    assert mock.paths_called() == [FLOW_PATH]


# ---- writes ---------------------------------------------------------------------


def test_create_version_posts_definition_and_returns_row() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return v2_ok({"id": 43, "name": "cli-1", "flow_id": FLOW_ID, "is_current": 0})

    mock = PlatformMock().post(VERSIONS_PATH, answer)
    row = _client(mock).create_version(FLOW_ID, "cli-1", _definition("a"), description="from cli")
    assert (row["id"], row["name"]) == (43, "cli-1")
    body = json.loads(seen[0].content)
    assert body == {"name": "cli-1", "description": "from cli", "data": _definition("a")}


def test_activate_version_posts_to_activate() -> None:
    path = f"{VERSIONS_PATH}/43/activate"
    mock = PlatformMock().post(path, v2_ok(None))
    _client(mock).activate_version(FLOW_ID, 43)
    assert mock.paths_called() == [path]


def test_create_flow_posts_to_flows() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return v2_ok({"id": FLOW_ID, "name": "研究", "version_id": 7})

    mock = PlatformMock().post("/api/v2/flows", answer)
    row = _client(mock).create_flow("研究", _definition(), description="d")
    assert row["id"] == FLOW_ID
    assert json.loads(seen[0].content) == {"name": "研究", "description": "d", "data": _definition()}


def test_tool_register_round_trip() -> None:
    draft = {"name": "invest", "children": [{"name": "submit_candidate"}]}
    mock = PlatformMock().post("/api/v2/tools/schema", v2_ok(draft)).post("/api/v2/tools", v2_ok(dict(draft, id=9)))
    client = _client(mock)
    parsed = client.parse_tool_schema("openapi: 3.0.0\n")
    assert parsed == draft
    assert json.loads(mock.calls[0].content) == {"file_content": "openapi: 3.0.0\n"}
    assert client.register_tool(parsed)["id"] == 9


def test_invoke_continuation_carries_both_ids() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return v2_ok({"session_id": "s1", "events": []})

    mock = PlatformMock().post("/api/v2/workflow/invoke", answer)
    client = _client(mock)
    client.invoke(FLOW_ID)
    client.invoke(FLOW_ID, session_id="s1", message_id=38, inputs={"input_1": {"user_input": "hi"}})
    first, second = (json.loads(r.content) for r in seen)
    # Streamed: the session id arrives with the first event, so a long round stays continuable.
    assert first == {"workflow_id": FLOW_ID, "stream": True}
    assert second == {
        "workflow_id": FLOW_ID,
        "stream": True,
        "session_id": "s1",
        "message_id": 38,
        "input": {"input_1": {"user_input": "hi"}},
    }

    # Half a continuation is refused locally: the platform would answer 10543
    # only after the round trip, and only for one of the two halves.
    for kwargs in ({"session_id": "s1"}, {"message_id": 38}):
        with pytest.raises(CliError) as caught:
            client.invoke(FLOW_ID, **kwargs)
        assert caught.value.exit_code == EXIT_USAGE
    assert len(seen) == 2


def test_platform_error_becomes_clierror() -> None:
    details = {"exception": "Version name already exists", "field": "name"}
    response = httpx.Response(
        200,
        json={
            "status_code": 10502,
            "status_message": "Version name already exists",
            "data": dict(details, hints=["换一个版本名"]),
        },
    )
    mock = PlatformMock().post(VERSIONS_PATH, response)
    with pytest.raises(CliError) as caught:
        _client(mock).create_version(FLOW_ID, "dup", _definition())
    failure = caught.value.as_failure("push")
    assert failure["code"] == 10502
    assert failure["details"] == dict(details, hints=["换一个版本名"])
    assert failure["hints"] == ["换一个版本名"]


def test_write_without_scope_names_the_scope() -> None:
    mock = PlatformMock().post(VERSIONS_PATH, v2_error(26003, "scope missing", required="workflow:write"))
    with pytest.raises(CliError) as caught:
        _client(mock).create_version(FLOW_ID, "v", _definition())
    assert caught.value.exit_code == EXIT_FORBIDDEN
    assert "workflow:write" in caught.value.next_step


def test_write_forbidden_by_owner_is_forbidden() -> None:
    mock = PlatformMock().post(VERSIONS_PATH, v2_error(403, "No permission to operate", http_status=200))
    with pytest.raises(CliError) as caught:
        _client(mock).create_version(FLOW_ID, "v", _definition())
    assert caught.value.exit_code == EXIT_FORBIDDEN
    assert FLOW_ID in caught.value.message


# ---- credentials ----------------------------------------------------------------


def test_no_credential_in_output(tmp_path: Path) -> None:
    cache = tmp_path / "node_templates.json"
    mock = PlatformMock().get(TEMPLATES_PATH, v2_ok(templates()))
    client = _client(mock, cache)
    client.node_templates()

    # The key is sent — that is the point of having it...
    assert mock.calls[0].headers["Authorization"] == f"Bearer {FAKE_KEY}"
    # ...and it is written nowhere else.
    assert FAKE_KEY not in cache.read_text("utf-8")
    assert FAKE_KEY not in repr(client)

    # A connection failure names the request, not the header.
    unreachable = _client(PlatformMock().get(FLOW_PATH, httpx.ConnectError(f"refused {FAKE_KEY}")))
    with pytest.raises(CliError) as caught:
        unreachable.current_definition(FLOW_ID)
    assert FAKE_KEY not in caught.value.message + caught.value.next_step

    # A platform that echoes the key back is masked on the way out.
    echo = _client(PlatformMock().get(FLOW_PATH, v2_error(403, f"denied for {FAKE_KEY}", http_status=200)))
    with pytest.raises(CliError) as caught:
        echo.current_definition(FLOW_ID)
    out, err = io.StringIO(), io.StringIO()
    emitter = Emitter(json_mode=True, stdout=out, stderr=err)
    emitter.error(caught.value.message + caught.value.platform_message)
    emitter.result("flow diff", False, caught.value.exit_code, failure=caught.value.as_failure("diff"))
    assert FAKE_KEY not in out.getvalue() + err.getvalue()


def test_cache_entry_is_a_verbatim_copy(tmp_path: Path) -> None:
    """What is stored is what the platform sent, with a checksum over it — nothing derived."""
    cache = tmp_path / "node_templates.json"
    _client(PlatformMock().get(TEMPLATES_PATH, v2_ok(templates())), cache).node_templates()
    (entry,) = json.loads(cache.read_text("utf-8"))["entries"].values()
    assert entry["templates"] == templates()
    canonical = json.dumps(templates(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    assert entry["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---- AC-43: a key with no usable resource owner -------------------------------------

#: What ``open_endpoints/domain/utils.py`` raises, rendered by the v2 HTTP-exception
#: handler: an envelope with the HTTP status as ``status_code`` and no ``data``.
_OWNER_UNAVAILABLE = httpx.Response(
    404, json={"status_code": 404, "status_message": "Open API resource owner is unavailable"}
)
_OWNER_INCOMPATIBLE = httpx.Response(
    500, json={"status_code": 500, "status_message": "Open API execution has no compatible resource owner"}
)


@pytest.mark.parametrize("reply", [_OWNER_UNAVAILABLE, _OWNER_INCOMPATIBLE], ids=["unavailable", "incompatible"])
@pytest.mark.parametrize("call", ["read", "write", "run"])
def test_missing_resource_owner_is_not_a_missing_workflow(reply: httpx.Response, call: str) -> None:
    mock = PlatformMock().get(FLOW_PATH, reply).post(VERSIONS_PATH, reply).post("/api/v2/workflow/invoke", reply)
    client = _client(mock)
    action = {
        "read": lambda: client.current_definition(FLOW_ID),
        "write": lambda: client.create_version(FLOW_ID, "v", _definition()),
        "run": lambda: client.invoke(FLOW_ID),
    }[call]
    with pytest.raises(CliError) as caught:
        action()
    err = caught.value
    assert err.exit_code == EXIT_FORBIDDEN
    assert "归属人" in err.message
    assert "不存在" not in err.message and "push --create" not in err.next_step


def test_plain_missing_workflow_is_still_missing() -> None:
    reply = httpx.Response(404, json={"status_code": 404, "status_message": "This resource does not exist"})
    with pytest.raises(CliError) as caught:
        _client(PlatformMock().get(FLOW_PATH, reply)).current_definition(FLOW_ID)
    assert caught.value.exit_code == EXIT_LOCAL_INVALID and "不存在" in caught.value.message


@pytest.mark.parametrize("data", [{"session_id": "s"}, {"session_id": "s", "events": None}, {"events": {}}])
def test_invoke_envelope_must_carry_an_event_list(data) -> None:
    """A JSON reply to a run is a defect unless it lists the round's events — "no events" is not "nothing happened"."""
    mock = PlatformMock().post("/api/v2/workflow/invoke", v2_ok(data))
    with pytest.raises(CliError) as caught:
        _client(mock).invoke(FLOW_ID)
    assert caught.value.exit_code == EXIT_DEFECT
