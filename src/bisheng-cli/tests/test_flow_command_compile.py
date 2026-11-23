"""T030 — ``bisheng flow compile``.

Compile is the one flow command that never writes: it reads the declaration,
fetches the platform's node templates and tool list, and prints a definition.
The tests pin the stream split (definition / NDJSON on stdout, people on
stderr), that a failed check reports *every* problem as data rather than a
traceback, and that nothing reaches the platform before the local quick-check
has passed (AC-11).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bisheng_cli.errors import EXIT_DEFECT, EXIT_LOCAL_INVALID, EXIT_OK, EXIT_UNREACHABLE
from tests.helpers.flow_command import (
    SPEC_YAML,
    SYSTEM_PROMPT,
    TEMPLATES_PATH,
    local_definition,
    login,
    result,
    run_cli,
    serve_catalogue,
    write_project,
)
from tests.helpers.flow_fixtures import find_param
from tests.helpers.platform_mock import PlatformMock, v2_error


@pytest.fixture
def spec(home_dir, tmp_path: Path) -> Path:
    login()
    return write_project(tmp_path / "proj")


def _node(definition: dict, node_id: str) -> dict:
    return next(n for n in definition["nodes"] if n["id"] == node_id)


def test_compile_emits_definition(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = run_cli(["flow", "compile", str(spec)], monkeypatch, serve_catalogue(PlatformMock()))

    assert code == EXIT_OK
    definition = json.loads(out)
    assert set(definition) == {"nodes", "edges", "viewport"}
    assert definition == local_definition()
    # The included prompt was expanded relative to the spec, not left as a tag.
    assert find_param(_node(definition, "llm_1"), "system_prompt")["value"] == SYSTEM_PROMPT
    assert "校验通过" in err


def test_json_mode_splits_streams(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err = run_cli(["flow", "compile", str(spec), "--json"], monkeypatch, serve_catalogue(PlatformMock()))

    assert code == EXIT_OK
    for line in out.splitlines():
        json.loads(line)  # every stdout line is NDJSON
    final = result(out)
    assert final["ok"] is True and final["command"] == "flow"
    assert final["data"]["definition"] == local_definition()
    assert final["data"]["nodes"] == 6 and final["data"]["edges"] == 6
    assert "校验通过" in err and "{" not in err.splitlines()[0]


@pytest.mark.parametrize("json_flag", [["--json"], []])
def test_result_event_is_last_line(spec: Path, monkeypatch: pytest.MonkeyPatch, json_flag) -> None:
    broken = write_project(spec.parent / "broken", SPEC_YAML.replace("type: start", "type: input"))
    code, out, _ = run_cli(["flow", "compile", str(broken), *json_flag], monkeypatch, serve_catalogue(PlatformMock()))

    assert code == EXIT_LOCAL_INVALID
    if json_flag:
        assert result(out)["ok"] is False
    else:
        assert out == ""  # no half-written definition on a failed check


def test_validation_failure_reports_all_issues(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = SPEC_YAML.replace("type: start", "type: input").replace(
        "{from: llm_1, to: tool_1}", "{from: llm_1, to: ghost}"
    )
    broken = write_project(spec.parent / "broken", text)
    code, out, err = run_cli(["flow", "compile", str(broken), "--json"], monkeypatch, serve_catalogue(PlatformMock()))

    assert code == EXIT_LOCAL_INVALID
    failure = result(out)["failure"]
    codes = {issue["code"] for issue in failure["details"]["issues"]}
    assert {"missing_start", "edge_target_missing"} <= codes
    # Every issue is on screen, one per line, and there is no stack trace.
    for issue in failure["details"]["issues"]:
        assert issue["message"] in err
    assert "Traceback" not in err


def test_overlay_applied(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = spec.parent / "overlay.py"
    overlay.write_text(
        "def apply(spec):\n"
        "    for node in spec['nodes']:\n"
        "        if node['id'] == 'llm_1':\n"
        "            node['params']['system_prompt'] = 'overlaid'\n",
        encoding="utf-8",
    )
    code, out, _ = run_cli(
        ["flow", "compile", str(spec), "--overlay", str(overlay)], monkeypatch, serve_catalogue(PlatformMock())
    )

    assert code == EXIT_OK
    assert find_param(_node(json.loads(out), "llm_1"), "system_prompt")["value"] == "overlaid"


def test_overlay_that_rewrites_structure_is_refused(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    overlay = spec.parent / "overlay.py"
    overlay.write_text("def apply(spec):\n    spec['edges'].pop()\n", encoding="utf-8")
    code, out, err = run_cli(
        ["flow", "compile", str(spec), "--overlay", str(overlay), "--json"], monkeypatch, PlatformMock()
    )

    assert code == EXIT_LOCAL_INVALID
    assert result(out)["failure"]["details"]["refused"]
    assert "覆盖层" in err


def test_out_file_written(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = spec.parent / "build" / "flow.json"
    code, out, err = run_cli(
        ["flow", "compile", str(spec), "--out", str(target)], monkeypatch, serve_catalogue(PlatformMock())
    )

    assert code == EXIT_OK
    assert out == ""
    assert json.loads(target.read_text("utf-8")) == local_definition()
    assert str(target) in err


def test_missing_top_level_field_sends_no_request(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = write_project(spec.parent / "broken", "name: x\nedges: []\n")
    mock = PlatformMock()
    code, _, err = run_cli(["flow", "compile", str(broken)], monkeypatch, mock)

    assert code == EXIT_LOCAL_INVALID
    assert "nodes" in err
    assert mock.calls == []


@pytest.mark.parametrize(
    ("templates_reply", "exit_code"),
    [(httpx.ConnectError("refused"), EXIT_UNREACHABLE), (v2_error(500, "boom", http_status=500), None)],
)
def test_compile_fails_without_platform_templates(
    spec: Path, monkeypatch: pytest.MonkeyPatch, templates_reply, exit_code
) -> None:
    code, out, err = run_cli(
        ["flow", "compile", str(spec)], monkeypatch, PlatformMock().get(TEMPLATES_PATH, templates_reply)
    )

    assert code not in (EXIT_OK, EXIT_DEFECT)
    if exit_code is not None:
        assert code == exit_code
    assert out == "" and "节点模板" in err


def test_compile_only_reads(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock())
    run_cli(["flow", "compile", str(spec), "--json"], monkeypatch, mock)

    assert {r.method for r in mock.calls} == {"GET"}
    assert {r.url.path for r in mock.calls} == {TEMPLATES_PATH, "/api/v2/tools"}
