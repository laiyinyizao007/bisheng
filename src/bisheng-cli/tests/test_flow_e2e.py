"""T046 — `bisheng flow` end to end against a real platform (network marker).

Skipped unless ``BISHENG_CLI_RUN_NETWORK_TESTS=1``. Runs as whoever is logged in
(``bisheng login``) — or the platform named by ``BISHENG_E2E_PLATFORM`` — so the
key must carry ``workflow:read``, ``workflow:write`` and ``workflow:invoke``.
The platform must be running this branch's backend: a container built before
F005 has none of the v2 write endpoints (rebuild the image first).

What it proves, in the order the commands are used:

1. ``compile`` against the platform's own templates passes validation;
2. ``push --create`` makes a workflow, and ``diff`` right after says identical;
3. editing the YAML shows up in ``diff`` as exactly that parameter;
4. ``push`` adds a **new** version and makes it current, and the version it
   replaced keeps its data byte for byte (AC-25);
5. ``run`` stops at the input node and names it and the field it waits for; the
   resume token carries the session id **and** the waiting message id; a resume
   with a one-shot input finishes the run with the output the YAML declares
   (AC-30, AC-31, AC-33);
6. ``tool register`` makes an OpenAPI operation referable by its operationId
   (AC-34/35), and a run whose tool node fails names that node and the reason —
   the platform's own failed-node event, not a guess (AC-32).

The tool points at ``http://127.0.0.1:9`` (nothing listens there), so the call
fails fast and locally. It leaves two workflows (``F005-e2e-…``) and one tool
type on the platform; there is no CLI delete command.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from bisheng_cli import credentials
from bisheng_cli.errors import EXIT_PRECHECK_FAILED, CliError
from bisheng_cli.flow.client import FlowClient
from bisheng_cli.http import PlatformClient
from bisheng_cli.main import run as main_run

pytestmark = pytest.mark.network


@pytest.fixture(autouse=True)
def logged_in() -> None:
    """Not logged in is a setup gap, not a product failure: skip with the instruction."""
    platform = os.environ.get("BISHENG_E2E_PLATFORM")
    try:
        credentials.load_profile(platform) if platform else credentials.load_current()
    except CliError:
        pytest.skip("not logged in: run `bisheng login <platform> --api-key-stdin` first")


SPEC = """\
name: NAME
description: F005 端到端验收
nodes:
  - id: start_1
    type: start
  - id: input_1
    type: input
    tab: dialog_input
  - id: output_1
    type: output
    params:
      message: {msg: "GREETING{{#input_1.user_input#}}", files: []}
  - id: end_1
    type: end
edges:
  - {from: start_1, to: input_1}
  - {from: input_1, to: output_1}
  - {from: output_1, to: end_1}
"""


def _cli(*argv: str) -> dict[str, Any]:
    platform = os.environ.get("BISHENG_E2E_PLATFORM")
    extra = ["--platform", platform] if platform else []
    out, err = io.StringIO(), io.StringIO()
    code = main_run(["--json", *extra, "flow", *argv], stdout=out, stderr=err)
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert lines and lines[-1]["event"] == "result", err.getvalue()
    result = lines[-1]
    result["exit"] = code
    result["stderr"] = err.getvalue()
    result["events"] = lines[:-1]
    return result


def _ok(result: dict[str, Any]) -> dict[str, Any]:
    assert result["ok"] and result["exit"] == 0, result["stderr"]
    return result["data"]


def _flow_client() -> FlowClient:
    platform = os.environ.get("BISHENG_E2E_PLATFORM")
    profile = credentials.load_profile(platform) if platform else credentials.load_current()
    return FlowClient(PlatformClient(profile.base_url, api_key=profile.api_key, trust_env=False))


def test_compile_diff_push_run_end_to_end(tmp_path: Path) -> None:
    name = time.strftime("F005-e2e-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    spec = tmp_path / "flow.yaml"
    spec.write_text(SPEC.replace("NAME", name).replace("GREETING", "收到："), encoding="utf-8")

    # 1. compile against the platform's templates
    compiled = _ok(_cli("compile", str(spec)))
    assert compiled["nodes"] == 4 and compiled["edges"] == 3

    # 2. create, then diff is identical
    created = _ok(_cli("push", str(spec), "--create"))
    flow_id, first_version = created["flow_id"], created["version_id"]
    assert _ok(_cli("diff", str(spec)))["identical"] is True

    # 3. a YAML edit is exactly one parameter difference
    spec.write_text(SPEC.replace("NAME", name).replace("GREETING", "已收到："), encoding="utf-8")
    changed = _ok(_cli("diff", str(spec)))
    assert changed["identical"] is False
    (node,) = changed["diff"]["nodes_changed"]
    assert node["id"] == "output_1" and [p["key"] for p in node["params"]] == ["message"]

    # 4. push adds a version, switches to it, and leaves the old one untouched
    client = _flow_client()
    before = client.version_definition(flow_id, first_version)
    pushed = _ok(_cli("push", str(spec), "--name", "e2e-second"))
    assert pushed["version_id"] != first_version and pushed["activated"] is True
    assert client.version_definition(flow_id, first_version) == before
    versions = {v["id"]: v for v in _ok(_cli("diff", str(spec), "--list-versions"))["versions"]}
    assert versions[pushed["version_id"]]["is_current"] is True
    assert versions[first_version]["is_current"] is False
    assert _ok(_cli("diff", str(spec)))["identical"] is True

    # 5. run: pause at the input node, resume with input, finish with the declared output
    paused = _ok(_cli("run", str(spec)))
    assert paused["status"] == "waiting_input" and paused["node_id"] == "input_1"
    assert [f["key"] for f in paused["fields"]] == ["user_input"]
    body = paused["resume"].removeprefix("r1.")
    token = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert (token["s"], token["m"], token["n"]) == (paused["session_id"], paused["message_id"], "input_1")
    assert token["s"] and str(token["m"]).isdigit()
    resumed = _cli("run", str(spec), "--resume", paused["resume"], "--input", "e2e")
    finished = _ok(resumed)
    assert finished["status"] == "finished"
    texts = [
        json.dumps(e.get("data", {}).get("output_schema"), ensure_ascii=False)
        for e in resumed["events"]
        if e.get("event") == "flow_event"
    ]
    assert any("已收到：e2e" in text for text in texts), texts


OPENAPI = """\
openapi: 3.0.0
info: {title: TITLE, version: "1"}
servers: [{url: "http://127.0.0.1:9"}]
paths:
  /ping:
    get:
      operationId: OPERATION
      summary: unreachable on purpose
      responses: {"200": {description: ok}}
"""

FAILING_SPEC = """\
name: NAME
nodes:
  - id: start_1
    type: start
  - id: tool_1
    type: tool
    tool: OPERATION
  - id: end_1
    type: end
edges:
  - {from: start_1, to: tool_1}
  - {from: tool_1, to: end_1}
"""


def test_registered_tool_failure_names_the_node(tmp_path: Path) -> None:
    stamp = time.strftime("%Y%m%d%H%M%S_") + uuid.uuid4().hex[:6]
    operation = f"f005_e2e_ping_{stamp}"
    openapi = tmp_path / "openapi.yaml"
    openapi.write_text(OPENAPI.replace("TITLE", f"f005-e2e-{stamp}").replace("OPERATION", operation), encoding="utf-8")

    registered = _ok(_cli("tool", "register", str(openapi)))
    assert [op["name"] for op in registered["operations"]] == [operation]
    listed = _ok(_cli("tool", "list"))
    assert any(op["name"] == operation for t in listed["tools"] for op in t["operations"])

    project = tmp_path / "failing"
    project.mkdir()
    spec = project / "flow.yaml"
    spec.write_text(
        FAILING_SPEC.replace("NAME", f"F005-e2e-fail-{stamp}").replace("OPERATION", operation), encoding="utf-8"
    )
    _ok(_cli("push", str(spec), "--create"))

    failed = _cli("run", str(spec))
    assert failed["ok"] is False and failed["exit"] == EXIT_PRECHECK_FAILED, failed["stderr"]
    details = failed["failure"]["details"]
    assert details["node_id"] == "tool_1" and details["reason"], details
    assert "tool_1" in failed["stderr"]
