# ruff: noqa: RUF002 - Chinese prose, full-width punctuation is intended
import pytest

from bisheng.common.services.config_service import settings
from bisheng.open_api.domain.scopes import (
    ALWAYS_ISSUABLE_OPEN_API_SCOPE_CODES,
    LOCAL_DEV_TOOLKIT_SCOPE_CODES,
    OPEN_API_SCOPE_IMPLIED_BY,
    OPEN_API_SCOPE_MAP,
    OPEN_API_SCOPES,
    get_open_api_scope_marker,
    issuable_scope_codes,
    issuable_scopes,
    open_api_scope,
)


def test_scope_marker_carries_modes_and_session_semantics():
    @open_api_scope("chat:invoke", modes=("S",), session=True)
    async def endpoint():
        return None

    marker = get_open_api_scope_marker(endpoint)
    assert marker is not None
    assert marker.scope == "chat:invoke"
    assert marker.modes == {"S"}
    assert marker.session is True


def test_unknown_scope_and_invalid_modes_fail_at_import_time():
    with pytest.raises(ValueError):
        open_api_scope("not-registered")
    with pytest.raises(ValueError):
        open_api_scope("knowledge:read", modes=())


def test_extension_scopes_are_not_issuable_without_open_platform(monkeypatch):
    monkeypatch.setattr(settings.open_platform, "enabled", False)

    assert LOCAL_DEV_TOOLKIT_SCOPE_CODES == {"model:invoke", "identity:read", "app:manage"}
    assert LOCAL_DEV_TOOLKIT_SCOPE_CODES.isdisjoint(issuable_scope_codes())
    assert LOCAL_DEV_TOOLKIT_SCOPE_CODES.isdisjoint(ALWAYS_ISSUABLE_OPEN_API_SCOPE_CODES)
    assert "delegate" in issuable_scope_codes()
    assert issuable_scope_codes() == ALWAYS_ISSUABLE_OPEN_API_SCOPE_CODES


def test_the_three_extension_scopes_become_issuable_with_open_platform(monkeypatch):
    """All three ship now: F051 gave ``model:invoke`` its protocol face and F052
    gave ``identity:read`` its MCP tools, so an administrator can grant each of
    them and something implements it. The equality is the point — a fourth scope
    appearing here without a surface behind it should fail this test."""

    monkeypatch.setattr(settings.open_platform, "enabled", True)

    codes = issuable_scope_codes()
    assert {"app:manage", "model:invoke", "identity:read"} <= codes
    assert codes == ALWAYS_ISSUABLE_OPEN_API_SCOPE_CODES | {"app:manage", "model:invoke", "identity:read"}


def test_issuable_scopes_have_localized_presentation_metadata(monkeypatch):
    monkeypatch.setattr(settings.open_platform, "enabled", False)
    assert [scope.group for scope in issuable_scopes()] == [
        "app",
        "app",
        "app",
        "app",
        "app",  # workflow:write — F005 workflow-as-code
        "workstation",
        "knowledge",
        "knowledge",
        "delegation",
    ]

    monkeypatch.setattr(settings.open_platform, "enabled", True)
    scopes = issuable_scopes()
    assert [scope.group for scope in scopes] == [
        "app",
        "app",
        "app",
        "app",
        "app",  # workflow:write — F005 workflow-as-code
        "workstation",
        "knowledge",
        "knowledge",
        "local_dev_toolkit",  # model:invoke — F051 protocol face
        "local_dev_toolkit",  # identity:read — F052 MCP tools, no REST route
        "local_dev_toolkit",  # app:manage
        "delegation",
    ]
    assert all(scope.label_key.startswith("openApiManagement.scopes.") for scope in scopes)
    assert all(scope.desc_key.startswith("openApiManagement.scopes.") for scope in scopes)
    # ``delegate`` gates a mode rather than a surface; ``identity:read`` gates
    # MCP tools, which are dispatched from the tool registry and so have no
    # ``/api/v2`` route to register. Everything else answers on a route.
    assert all(scope.endpoints for scope in scopes if scope.code not in {"delegate", "identity:read"})


def test_registry_maps_each_endpoint_to_exactly_one_scope():
    mapped = [endpoint for scope in OPEN_API_SCOPES for endpoint in scope.endpoints]
    assert len(mapped) == len(set(mapped))


def test_workflow_read_scope_extended():
    """F005 T007 — ``workflow:read`` 覆盖 diff 所需的全部读面。

    读当前版本（既有）、读任意版本列表/单版本（diff 基准与多版本核对）、
    节点模板（编译器骨架来源）、工具清单（按 operationId 引用工具）。
    """
    endpoints = set(OPEN_API_SCOPE_MAP["workflow:read"].endpoints)
    assert {("GET", "/api/v2/flows/{flow_id}"), ("GET", "/api/v2/flow/node_templates")} <= endpoints
    assert {
        ("GET", "/api/v2/flows/{flow_id}/versions"),
        ("GET", "/api/v2/flows/{flow_id}/versions/{version_id}"),
        ("GET", "/api/v2/tools"),
    } <= endpoints


def test_workflow_write_scope_registered():
    """F005 T007 — ``workflow:write`` 已注册且覆盖全部六个写路径。"""
    scope = OPEN_API_SCOPE_MAP["workflow:write"]
    assert scope.endpoints == (
        ("POST", "/api/v2/flows"),
        ("POST", "/api/v2/flows/{flow_id}/versions"),
        ("PUT", "/api/v2/flows/{flow_id}/versions/{version_id}"),
        ("POST", "/api/v2/flows/{flow_id}/versions/{version_id}/activate"),
        ("POST", "/api/v2/tools"),
        ("POST", "/api/v2/tools/schema"),
    )
    assert scope.issuable is True
    assert scope.requires_open_platform is False


def test_scope_group_is_not_local_dev_toolkit():
    """``workflow:write`` 是平台级能力，不是本地开发工具箱的扩展位。

    group ``app`` 意味着它在任何部署上都可签发（不开 open platform 也能发），
    且不进 ``LOCAL_DEV_TOOLKIT_SCOPE_CODES`` 的互斥/开关联动。
    """
    scope = OPEN_API_SCOPE_MAP["workflow:write"]
    assert scope.group == "app"
    assert scope.code not in LOCAL_DEV_TOOLKIT_SCOPE_CODES
    assert scope.label_key.startswith("openApiManagement.scopes.")
    assert scope.desc_key.startswith("openApiManagement.scopes.")


def test_workflow_write_never_implied_by_read():
    """读钥匙不会被暗示成写钥匙——``OPEN_API_SCOPE_IMPLIED_BY`` 里没有这对。

    ``knowledge:write`` 暗示 ``knowledge:read`` 是因为写必经读；工作流写不经
    任何读端点，且「diff 钥匙自动能 push」正是 scope 拆开要防的事。
    """
    assert "workflow:read" not in OPEN_API_SCOPE_IMPLIED_BY
    assert "workflow:write" not in OPEN_API_SCOPE_IMPLIED_BY
