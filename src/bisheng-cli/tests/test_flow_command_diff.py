"""T032 — ``bisheng flow diff``.

The command is a thin wire between three pieces that are tested on their own
(compile, the client, the diff algorithm). What is pinned here is the wiring:
which baseline is read (current version by default, a named version on
request), that the human lines are produced from the machine result and from
nothing else (AC-23), that "not there / not yours / could not read" never
collapses into "no differences" (AC-24), and where the workflow id is kept.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from bisheng_cli.errors import EXIT_FORBIDDEN, EXIT_LOCAL_INVALID, EXIT_OK, EXIT_UNREACHABLE, EXIT_USAGE
from bisheng_cli.flow.diff import render
from tests.helpers.flow_command import (
    BASE,
    FLOW_ID,
    FLOW_PATH,
    TEMPLATES_PATH,
    VERSIONS_PATH,
    flow_row,
    local_definition,
    login,
    result,
    run_cli,
    serve_catalogue,
    write_project,
)
from tests.helpers.flow_fixtures import find_param
from tests.helpers.platform_mock import PlatformMock, v2_error, v2_ok


@pytest.fixture
def spec(home_dir, tmp_path: Path) -> Path:
    login()
    return write_project(tmp_path / "proj")


def _edited_online() -> dict:
    """The local definition with two web edits: a changed prompt and an extra node."""
    online = local_definition()
    llm = next(n for n in online["nodes"] if n["id"] == "llm_1")
    find_param(llm, "system_prompt")["value"] = "网页上改过的提示词"
    extra = copy.deepcopy(llm)
    extra["id"] = extra["data"]["id"] = "llm_web"
    online["nodes"].append(extra)
    return online


def _diff(spec: Path, monkeypatch, mock, *extra: str) -> tuple[int, dict, str]:
    code, out, err = run_cli(["flow", "diff", str(spec), "--flow-id", FLOW_ID, "--json", *extra], monkeypatch, mock)
    return code, result(out), err


def test_defaults_to_current_version(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, flow_row(local_definition()))
    code, final, err = _diff(spec, monkeypatch, mock)

    assert code == EXIT_OK
    assert final["data"]["identical"] is True and final["data"]["count"] == 0
    assert final["data"]["baseline"] == {"flow_id": FLOW_ID, "version_id": None}
    assert not [p for p in mock.paths_called() if p.startswith(VERSIONS_PATH)]
    assert "一致" in err


def test_version_id_selects_historical_baseline(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    row = {"id": 5, "name": "v5", "flow_id": FLOW_ID, "is_current": 0, "data": _edited_online()}
    mock = serve_catalogue(PlatformMock()).get(f"{VERSIONS_PATH}/5", v2_ok(row))
    code, final, _ = _diff(spec, monkeypatch, mock, "--version-id", "5")

    assert code == EXIT_OK
    assert final["data"]["baseline"] == {"flow_id": FLOW_ID, "version_id": 5}
    assert final["data"]["identical"] is False
    assert FLOW_PATH not in mock.paths_called()


def test_list_versions(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"id": 3, "name": "v0", "flow_id": FLOW_ID, "is_current": 0, "update_time": "2026-11-20T10:00:00", "data": {}},
        {"id": 5, "name": "cli-1", "flow_id": FLOW_ID, "is_current": 1, "update_time": "2026-11-21T10:00:00"},
    ]
    mock = PlatformMock().get(VERSIONS_PATH, v2_ok({"data": rows, "total": 3}))
    code, final, err = _diff(spec, monkeypatch, mock, "--list-versions")

    assert code == EXIT_OK
    versions = final["data"]["versions"]
    assert [(v["id"], v["name"], v["is_current"]) for v in versions] == [(3, "v0", False), (5, "cli-1", True)]
    assert all("data" not in v for v in versions)
    # Listing compiles nothing, so it fetches no templates.
    assert TEMPLATES_PATH not in mock.paths_called()
    assert "cli-1" in err and "当前" in err


def test_list_versions_and_version_id_are_exclusive(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_cli(["flow", "diff", str(spec), "--list-versions", "--version-id", "5"], monkeypatch, PlatformMock())


def test_machine_readable_details(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, flow_row(_edited_online()))
    code, final, _ = _diff(spec, monkeypatch, mock)

    assert code == EXIT_OK
    found = final["data"]["diff"]
    assert [n["id"] for n in found["nodes_only_online"]] == ["llm_web"]
    (changed,) = found["nodes_changed"]
    assert changed["id"] == "llm_1"
    assert [(p["key"], p["online"], p["local"]) for p in changed["params"]] == [
        ("system_prompt", "网页上改过的提示词", "你是研究员")
    ]
    assert final["data"]["count"] == 2


def test_human_and_machine_agree(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, flow_row(_edited_online()))
    _, final, err = _diff(spec, monkeypatch, mock)

    lines = render(final["data"]["diff"])
    assert len(lines) > 1
    assert err.splitlines()[-len(lines) :] == lines


@pytest.mark.parametrize(
    ("reply", "exit_code"),
    [
        (v2_error(404, "This resource does not exist", http_status=404), EXIT_LOCAL_INVALID),
        (v2_error(403, "No permission to operate", http_status=403), EXIT_FORBIDDEN),
        (httpx.ConnectError("refused"), EXIT_UNREACHABLE),
    ],
    ids=["missing", "forbidden", "failed"],
)
def test_three_failure_modes_distinguished(spec: Path, monkeypatch: pytest.MonkeyPatch, reply, exit_code: int) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, reply)
    code, final, err = _diff(spec, monkeypatch, mock)

    assert code == exit_code
    assert final["ok"] is False
    assert "identical" not in final["data"]
    assert "一致" not in err and "无差异" not in err


def test_failed_read_does_not_bind(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, v2_error(404, "gone", http_status=404))
    _diff(spec, monkeypatch, mock)

    assert not (spec.parent / ".bisheng" / "flow.json").exists()


def test_flow_binding_written_to_bisheng_dir(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, flow_row(local_definition()))
    code, _, _ = _diff(spec, monkeypatch, mock)
    assert code == EXIT_OK

    store = json.loads((spec.parent / ".bisheng" / "flow.json").read_text("utf-8"))
    assert store["version"] == 1
    (entry,) = store["flows"].values()
    assert entry[spec.name]["flow_id"] == FLOW_ID
    assert BASE in store["flows"]  # keyed like .bisheng/app.json, by normalised platform address

    # The next run finds the workflow without --flow-id.
    code, out, _ = run_cli(["flow", "diff", str(spec), "--json"], monkeypatch, mock)
    assert code == EXIT_OK and result(out)["data"]["baseline"]["flow_id"] == FLOW_ID


def test_no_binding_and_no_flow_id_is_a_usage_error(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = PlatformMock()
    code, _, err = run_cli(["flow", "diff", str(spec)], monkeypatch, mock)

    assert code == EXIT_USAGE
    assert "--flow-id" in err and f"flow push {spec.name} --create" in err
    assert mock.calls == []
