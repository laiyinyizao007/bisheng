"""Shared scaffolding for the ``bisheng flow`` command tests (T030–T039).

Every test drives the real entry point (``main.run``) against a ``PlatformMock``:
argument parsing, dispatch, the emitter and the exit-code funnel are all the
production ones. Only the transport is replaced (``use_mock_transport``), so the
``no_network`` sentinel still catches any request a test forgot to route.
"""

from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from bisheng_cli import credentials
from bisheng_cli.commands import flow as flow_cmd
from bisheng_cli.flow.dsl import compile_spec
from bisheng_cli.main import run as main_run
from tests.helpers.flow_fixtures import templates, tools
from tests.helpers.platform_mock import FAKE_KEY, FAKE_KEY_MASK, PlatformMock, use_mock_transport, v2_ok

BASE = "http://platform.test"
FLOW_ID = "1194f8540b9944fea4353b07f0493cbf"
TEMPLATES_PATH = "/api/v2/flow/node_templates"
TOOLS_PATH = "/api/v2/tools"
TOOL_SCHEMA_PATH = "/api/v2/tools/schema"
FLOWS_PATH = "/api/v2/flows"
FLOW_PATH = f"{FLOWS_PATH}/{FLOW_ID}"
VERSIONS_PATH = f"{FLOW_PATH}/versions"
INVOKE_PATH = "/api/v2/workflow/invoke"

SPEC_YAML = """\
name: 研究
description: 人工驱动的研究循环
nodes:
  - id: start_1
    type: start
  - id: input_1
    type: input
    tab: dialog_input
  - id: llm_1
    type: llm
    name: 写代码
    params:
      model_id: 7
      system_prompt: !include prompts/system.md
      user_prompt: "{{#input_1.user_input#}}"
  - id: tool_1
    type: tool
    tool: submit_candidate
    params:
      name: x
      source: y
  - id: cond_1
    type: condition
    cases:
      - id: stop
        conditions:
          - left: input_1.user_input
            op: equals
            value: 结束
  - id: end_1
    type: end
edges:
  - {from: start_1, to: input_1}
  - {from: input_1, to: llm_1}
  - {from: llm_1, to: tool_1}
  - {from: tool_1, to: cond_1}
  - {from: cond_1, to: end_1, handle: stop}
  - {from: cond_1, to: input_1}
"""
SYSTEM_PROMPT = "你是研究员"


def write_project(root: Path, spec: str = SPEC_YAML) -> Path:
    """A project dir with ``flow.yaml`` and the prompt it includes; returns the spec path."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "prompts").mkdir(exist_ok=True)
    (root / "prompts" / "system.md").write_text(SYSTEM_PROMPT, encoding="utf-8")
    path = root / "flow.yaml"
    path.write_text(spec, encoding="utf-8")
    return path


def login() -> None:
    credentials.save_profile(
        BASE,
        {"api_key": FAKE_KEY, "key_mask": FAKE_KEY_MASK, "tenant_id": 1, "service_account": {"id": 1, "name": "dev"}},
    )


def local_definition() -> dict[str, Any]:
    """What ``SPEC_YAML`` compiles to against the fixture catalogue."""
    import yaml

    spec = yaml.safe_load(SPEC_YAML.replace("!include prompts/system.md", json.dumps(SYSTEM_PROMPT)))
    return compile_spec(spec, templates(), tools())


def serve_catalogue(mock: PlatformMock) -> PlatformMock:
    return mock.get(TEMPLATES_PATH, v2_ok(templates())).get(TOOLS_PATH, v2_ok(tools()))


def flow_row(definition: dict[str, Any] | None, *, flow_id: str = FLOW_ID) -> httpx.Response:
    return v2_ok({"id": flow_id, "name": "研究", "status": 1, "data": copy.deepcopy(definition)})


def run_cli(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    mock: PlatformMock,
    *,
    interactive: bool = False,
    answers: list[str] | None = None,
) -> tuple[int, str, str]:
    """``bisheng <argv>`` → ``(exit_code, stdout, stderr)``.

    ``interactive`` decides whether a person is at the keyboard; ``answers`` is
    what they type, one entry per prompt. A prompt with no answer left fails the
    test — an unexpected question is a bug, not something to default.
    """
    use_mock_transport(monkeypatch, flow_cmd, mock)
    queue = list(answers or [])
    monkeypatch.setattr(flow_cmd, "_interactive", lambda _emitter: interactive)

    def ask(prompt: str) -> str:
        if not queue:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return queue.pop(0)

    monkeypatch.setattr(flow_cmd, "_ask", ask)
    out, err = io.StringIO(), io.StringIO()
    code = main_run(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def events(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def result(stdout: str) -> dict[str, Any]:
    parsed = events(stdout)
    assert parsed and parsed[-1]["event"] == "result", "the last stdout line must be the result event"
    assert [e["event"] for e in parsed].count("result") == 1
    return parsed[-1]


def bodies(mock: PlatformMock, method: str, path: str) -> list[Any]:
    return [json.loads(r.content) for r in mock.calls if r.method == method and r.url.path == path]


def sse(*events_: dict[str, Any], session_id: str = "chat1_async_task_id") -> httpx.Response:
    """A streamed invoke reply, one ``data:`` line per event, as the v2 endpoint writes it."""
    body = "".join(
        f"data: {json.dumps({'session_id': session_id, 'data': e}, ensure_ascii=False)}\n\n" for e in events_
    )
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode("utf-8"))
