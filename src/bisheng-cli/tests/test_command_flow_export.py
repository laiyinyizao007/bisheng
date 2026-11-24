"""F007 T006 — ``bisheng flow export``: an online workflow → a declaration file (AC-24).

The command is a thin wire: read the online definition, ``decompile`` it (tested on
its own in test_dsl_decompile.py), write the YAML. Pinned here: the file it writes
diffs clean against the workflow it came from, what the declaration cannot carry is
listed (stderr and ``data.omitted``), an existing file is never overwritten without
``--force``, and every exit path emits exactly one ``result`` event.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from bisheng_cli.errors import EXIT_LOCAL_INVALID, EXIT_OK
from tests.helpers.flow_command import (
    FLOW_ID,
    FLOW_PATH,
    VERSIONS_PATH,
    local_definition,
    login,
    result,
    run_cli,
    serve_catalogue,
)
from tests.helpers.platform_mock import PlatformMock, v2_ok


@pytest.fixture
def target(home_dir, tmp_path: Path) -> Path:
    login()
    return tmp_path / "proj" / "flow.yaml"


def _online_row(definition: dict) -> object:
    return v2_ok(
        {
            "id": FLOW_ID,
            "name": "研究",
            "description": "人工驱动的研究循环",
            "status": 1,
            "data": copy.deepcopy(definition),
        }
    )


def _export(target: Path, monkeypatch, mock, *extra: str) -> tuple[int, dict, str]:
    code, out, err = run_cli(["flow", "export", str(target), "--flow-id", FLOW_ID, "--json", *extra], monkeypatch, mock)
    return code, result(out), err


def test_writes_a_declaration_that_diffs_clean_against_its_source(target: Path, monkeypatch) -> None:
    online = local_definition()
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, _online_row(online))
    code, final, _ = _export(target, monkeypatch, mock)

    assert code == EXIT_OK
    declaration = yaml.safe_load(target.read_bytes().decode("utf-8"))
    assert (declaration["name"], declaration["description"]) == ("研究", "人工驱动的研究循环")
    assert final["data"]["out"] == str(target)
    assert final["data"]["omitted"] == []
    assert (final["data"]["nodes"], final["data"]["edges"]) == (len(online["nodes"]), len(online["edges"]))

    # The exported file is a real declaration: diff against the same workflow is clean.
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, _online_row(online))
    code, out, _ = run_cli(["flow", "diff", str(target), "--json"], monkeypatch, mock)
    assert code == EXIT_OK
    assert result(out)["data"]["identical"] is True


def test_explicit_flow_id_is_bound_for_later_commands(target: Path, monkeypatch) -> None:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, _online_row(local_definition()))
    _export(target, monkeypatch, mock)
    assert (target.parent / ".bisheng" / "flow.json").is_file()


def test_lists_what_the_declaration_cannot_carry(target: Path, monkeypatch) -> None:
    online = local_definition()
    online["nodes"].append(
        {
            "id": "note_ab12c",
            "type": "noteNode",
            "position": {"x": 0, "y": 0},
            "data": {"id": "note_ab12c", "type": "note", "group_params": [], "value": "勿删"},
        }
    )
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, _online_row(online))
    code, final, err = _export(target, monkeypatch, mock)

    assert code == EXIT_OK
    assert [(o["kind"], o.get("node")) for o in final["data"]["omitted"]] == [("note", "note_ab12c")]
    assert "note_ab12c" in err and "便签" in err


def test_existing_file_is_not_overwritten_and_nothing_is_fetched(target: Path, monkeypatch) -> None:
    target.parent.mkdir(parents=True)
    target.write_bytes("name: 手写的\n".encode())
    mock = PlatformMock()
    code, final, err = _export(target, monkeypatch, mock)

    assert code == EXIT_LOCAL_INVALID
    assert final["ok"] is False
    assert target.read_bytes().decode("utf-8") == "name: 手写的\n"
    assert mock.paths_called() == []
    assert "--force" in err


def test_force_overwrites(target: Path, monkeypatch) -> None:
    target.parent.mkdir(parents=True)
    target.write_bytes(b"name: old\n")
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, _online_row(local_definition()))
    code, _, _ = _export(target, monkeypatch, mock, "--force")

    assert code == EXIT_OK
    assert yaml.safe_load(target.read_bytes().decode("utf-8"))["name"] == "研究"


def test_version_id_exports_that_version(target: Path, monkeypatch) -> None:
    historical = local_definition()
    historical["nodes"] = [n for n in historical["nodes"] if n["id"] != "tool_1"]
    historical["edges"] = [e for e in historical["edges"] if "tool_1" not in (e["source"], e["target"])]
    row = {"id": 5, "name": "v5", "flow_id": FLOW_ID, "is_current": 0, "data": historical}
    mock = (
        serve_catalogue(PlatformMock())
        .get(FLOW_PATH, _online_row(local_definition()))
        .get(f"{VERSIONS_PATH}/5", v2_ok(row))
    )
    code, final, _ = _export(target, monkeypatch, mock, "--version-id", "5")

    assert code == EXIT_OK
    assert final["data"]["baseline"] == {"flow_id": FLOW_ID, "version_id": 5}
    assert "tool_1" not in {n["id"] for n in yaml.safe_load(target.read_bytes().decode("utf-8"))["nodes"]}
