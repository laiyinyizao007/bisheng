"""T038 — ``bisheng flow tool register`` / ``bisheng flow tool list``.

Registration is two platform calls: parse the OpenAPI document into a draft,
then register the draft. The draft comes back as the platform's full tool
model (``id`` / ``user_id`` / ``is_delete`` / nulls …) while the register body
forbids extra keys, so the CLI sends only what the register face accepts and
leaves unset what the draft left empty — a ``null`` would override the
platform's own default.

What a developer needs afterwards is the operation ids: a workflow declaration
references tools by them (AC-35).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bisheng_cli.errors import EXIT_DEFECT, EXIT_FORBIDDEN, EXIT_LOCAL_INVALID, EXIT_OK
from tests.helpers.flow_command import TOOL_SCHEMA_PATH, TOOLS_PATH, bodies, login, result, run_cli
from tests.helpers.flow_fixtures import tools
from tests.helpers.platform_mock import PlatformMock, v2_error, v2_ok

OPENAPI = "openapi: 3.0.0\ninfo: {title: invest}\nservers: [{url: 'http://127.0.0.1:8765'}]\npaths: {}\n"


def _draft() -> dict:
    """``GptsToolsTypeRead`` as the parse endpoint dumps it — nulls and server fields included."""
    child = {
        "id": None,
        "name": "submit_candidate",
        "desc": "提交候选策略",
        "logo": None,
        "tool_key": "a1b2",
        "type": None,
        "is_preset": 0,
        "is_delete": 0,
        "user_id": None,
        "api_params": [{"name": "source", "in": "query"}],
        "extra": '{"operationId": "submit_candidate"}',
        "create_time": None,
    }
    return {
        "id": None,
        "name": "invest",
        "logo": None,
        "description": "研究工具",
        "server_host": "http://127.0.0.1:8765",
        "auth_method": 0,
        "api_key": None,
        "auth_type": "basic",
        "is_preset": 0,
        "user_id": None,
        "is_delete": 0,
        "openapi_schema": OPENAPI,
        "parameter_name": None,
        "api_location": None,
        "children": [child, dict(child, name="get_feedback", tool_key="c3d4", desc="取反馈")],
    }


def _registered() -> dict:
    row = _draft()
    row["id"] = 11
    for index, child in enumerate(row["children"]):
        child.update(id=100 + index, type=11)
    return row


@pytest.fixture
def openapi(home_dir, tmp_path: Path) -> Path:
    login()
    path = tmp_path / "openapi.yaml"
    path.write_text(OPENAPI, encoding="utf-8")
    return path


def _register(openapi: Path, monkeypatch, mock, *extra: str) -> tuple[int, str, str]:
    return run_cli(["flow", "tool", "register", str(openapi), "--json", *extra], monkeypatch, mock)


def test_register_reports_tool_and_operation_ids(openapi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = PlatformMock().post(TOOL_SCHEMA_PATH, v2_ok(_draft())).post(TOOLS_PATH, v2_ok(_registered()))
    code, out, err = _register(openapi, monkeypatch, mock)

    assert code == EXIT_OK
    assert bodies(mock, "POST", TOOL_SCHEMA_PATH) == [{"file_content": OPENAPI}]
    data = result(out)["data"]
    assert data["tool_type"] == {"id": 11, "name": "invest"}
    assert [(o["name"], o["tool_key"]) for o in data["operations"]] == [
        ("submit_candidate", "a1b2"),
        ("get_feedback", "c3d4"),
    ]
    assert "submit_candidate" in err and "get_feedback" in err and "11" in err


def test_register_sends_only_what_the_register_face_accepts(openapi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = PlatformMock().post(TOOL_SCHEMA_PATH, v2_ok(_draft())).post(TOOLS_PATH, v2_ok(_registered()))
    _register(openapi, monkeypatch, mock)

    (body,) = bodies(mock, "POST", TOOLS_PATH)
    assert body == {
        "name": "invest",
        "description": "研究工具",
        "server_host": "http://127.0.0.1:8765",
        "auth_method": 0,
        "auth_type": "basic",
        "is_preset": 0,
        "openapi_schema": OPENAPI,
        "children": [
            {
                "name": "submit_candidate",
                "desc": "提交候选策略",
                "tool_key": "a1b2",
                "api_params": [{"name": "source", "in": "query"}],
                "extra": '{"operationId": "submit_candidate"}',
            },
            {
                "name": "get_feedback",
                "desc": "取反馈",
                "tool_key": "c3d4",
                "api_params": [{"name": "source", "in": "query"}],
                "extra": '{"operationId": "submit_candidate"}',
            },
        ],
    }


def test_register_name_override(openapi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = PlatformMock().post(TOOL_SCHEMA_PATH, v2_ok(_draft())).post(TOOLS_PATH, v2_ok(_registered()))
    _register(openapi, monkeypatch, mock, "--name", "invest-dev")

    assert bodies(mock, "POST", TOOLS_PATH)[0]["name"] == "invest-dev"


def test_list_reports_types_and_operations(home_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    login()
    mock = PlatformMock().get(TOOLS_PATH, v2_ok(tools()))
    code, out, err = run_cli(["flow", "tool", "list", "--json"], monkeypatch, mock)

    assert code == EXIT_OK
    (tool_type,) = result(out)["data"]["tools"]
    assert (tool_type["id"], tool_type["name"]) == (3, "invest")
    assert [(o["name"], o["tool_key"]) for o in tool_type["operations"]] == [
        ("submit_candidate", "key_submit"),
        ("get_feedback", "key_feedback"),
    ]
    # node_template is the platform's business, not something to print.
    assert "node_template" not in out
    assert "invest/submit_candidate" in err


@pytest.mark.parametrize("failing", [TOOL_SCHEMA_PATH, TOOLS_PATH])
def test_permission_error_not_reported_as_success(openapi: Path, monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    denied = v2_error(26003, "scope missing", required="workflow:write")
    mock = PlatformMock()
    mock.post(TOOL_SCHEMA_PATH, denied if failing == TOOL_SCHEMA_PATH else v2_ok(_draft()))
    mock.post(TOOLS_PATH, denied if failing == TOOLS_PATH else v2_ok(_registered()))
    code, out, err = _register(openapi, monkeypatch, mock)

    assert code == EXIT_FORBIDDEN
    assert result(out)["ok"] is False
    assert "workflow:write" in err
    assert "注册成功" not in err and "已注册" not in err
    if failing == TOOL_SCHEMA_PATH:
        assert TOOLS_PATH not in mock.paths_called()


def test_missing_openapi_file_sends_nothing(home_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    login()
    mock = PlatformMock()
    code, _, err = run_cli(["flow", "tool", "register", str(tmp_path / "nope.yaml")], monkeypatch, mock)

    assert code == EXIT_LOCAL_INVALID
    assert "nope.yaml" in err
    assert mock.calls == []


def test_registration_without_operations_is_not_success(openapi: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = dict(_registered(), children=[])
    mock = PlatformMock().post(TOOL_SCHEMA_PATH, v2_ok(_draft())).post(TOOLS_PATH, v2_ok(empty))
    code, out, err = _register(openapi, monkeypatch, mock)

    assert code == EXIT_DEFECT
    assert result(out)["ok"] is False
    assert "已注册" not in err
