# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T011 — v2 工具端点（``GET/POST /api/v2/tools``、``POST /api/v2/tools/schema``）。

三件事：

1. **钥匙墙**：读面只要 ``workflow:read``；写面（注册与 schema 解析）要
   ``workflow:write``，缺 scope 一律 403/26003。
2. **委托**：端点把活儿全部交给 ``ToolServices`` 既有方法——打桩服务方法，
   断言参数原样穿透（file_content/download_url、请求体），解析与落库逻辑
   归 ``ToolServices`` 自己的测试；RULE-5 由不 import ``bisheng.tool.api``
   保证（路由矩阵与本文件的标记断言守 import 面）。
3. **配额对齐**：注册走 ``TOOL`` 资源类型配额，与 v1 ``POST /api/v1/tool`` 同
   一道闸。

用 ``validate_bearer`` 接缝注入凭据，身份桥打桩（桥的拒绝语义由
``test_workflow_write_v2`` 与仓库既有测试守）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.main import app
from bisheng.open_api.domain.context import OpenApiPrincipal
from bisheng.role.domain.services.quota_service import QuotaResourceType

TOOLS_ENDPOINTS = "bisheng.open_endpoints.api.endpoints.tools_v2"
TOOL_SERVICE = "bisheng.tool.domain.services.tool.ToolServices"

AUTH = {"Authorization": "Bearer bs-sak-x"}


def principal(*, scopes):
    return OpenApiPrincipal(
        credential_id=7,
        actor_kind="service_account",
        actor_id=31,
        actor_name="cli",
        tenant_id=9,
        resource_owner_user_id=12,
        scopes=scopes,
        authorization_subject_type="service_account",
        authorization_subject_id=31,
        effective_user_id=None,
    )


def patch_principal(monkeypatch, principal_value):
    async def validate(_authorization):
        return principal_value

    monkeypatch.setattr("bisheng.open_api.api.dependencies.validate_bearer", validate)


def login_as_owner(monkeypatch, user_id=12, tenant_id=1):
    user = UserPayload(user_id=user_id, user_name="owner", user_role=[], tenant_id=tenant_id)
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.get_open_api_operator_async", AsyncMock(return_value=user))
    return user


def patch_service(monkeypatch, method, return_value):
    mock = AsyncMock(return_value=return_value)
    monkeypatch.setattr(f"{TOOL_SERVICE}.{method}", mock)
    return mock


async def call(method, path, *, json=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=json, headers=AUTH)


# ---------------------------------------------------------------------------
# 钥匙墙
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v2/tools"),
        ("POST", "/api/v2/tools"),
        ("POST", "/api/v2/tools/schema"),
    ],
)
async def test_no_key_no_tools(monkeypatch, method, path):
    """真实 validate_bearer：没有凭据一律 401/26001——工具面不是匿名面。"""
    response = await call(method, path)
    assert response.status_code == 401
    assert response.json()["status_code"] == 26001


async def test_list_needs_only_read_scope(monkeypatch):
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    login_as_owner(monkeypatch)
    from bisheng.tool.domain.models.gpts_tools import GptsToolsTypeListItem

    listed = patch_service(monkeypatch, "get_tool_list", [GptsToolsTypeListItem(id=1, name="t")])

    response = await call("GET", "/api/v2/tools")

    assert response.status_code == 200
    assert [(one["id"], one["name"]) for one in response.json()["data"]] == [(1, "t")]
    listed.assert_awaited_once_with(None, "use")


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("POST", "/api/v2/tools", {"name": "t"}),
        ("POST", "/api/v2/tools/schema", {"file_content": "openapi: 3.0.0"}),
    ],
)
async def test_missing_write_scope_refused(monkeypatch, method, path, json):
    """只有 ``workflow:read`` 的钥匙注册不了工具——403/26003，指名缺哪个 scope。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    response = await call(method, path, json=json)
    assert response.status_code == 403
    body = response.json()
    assert body["status_code"] == 26003
    assert body["data"]["required"] == "workflow:write"


# ---------------------------------------------------------------------------
# 委托与配额
# ---------------------------------------------------------------------------


async def test_schema_parses_operations(monkeypatch):
    """``POST /tools/schema`` 把请求体原样交给 ``parse_openapi_schema``。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    draft = {"name": "parsed", "children": []}
    parse = patch_service(monkeypatch, "parse_openapi_schema", draft)

    response = await call("POST", "/api/v2/tools/schema", json={"file_content": "openapi: 3.0.0"})

    assert response.status_code == 200
    parse.assert_awaited_once_with(None, "openapi: 3.0.0")  # 位置参数：download_url, file_content


async def test_register_creates_tool_type(monkeypatch):
    """``POST /tools`` 走 ``add_tools``，配额按 ``TOOL`` 资源类型记账。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    created = patch_service(monkeypatch, "add_tools", {"id": 5, "name": "t"})
    quota = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", quota)

    response = await call("POST", "/api/v2/tools", json={"name": "t"})

    assert response.status_code == 200
    created.assert_awaited_once()
    quota.assert_awaited_once()
    assert quota.await_args.kwargs["resource_type"] == QuotaResourceType.TOOL
    assert quota.await_args.kwargs["user_id"] == 12


async def test_list_returns_registered_tools(monkeypatch):
    """读面拿到的就是 ToolServices 的产出——端点只摘掉 api_key, 其余不加工。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    login_as_owner(monkeypatch)
    from bisheng.tool.domain.models.gpts_tools import GptsToolsTypeListItem, GptsToolsWithNodeTemplate

    child = GptsToolsWithNodeTemplate(id=9, name="op", tool_key="k", node_template={"type": "tool"})
    item = GptsToolsTypeListItem(id=3, name="query_api", children=[child])
    patch_service(monkeypatch, "get_tool_list", [item])

    response = await call("GET", "/api/v2/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 200
    # 除 api_key 外与服务产出逐字段相同
    assert body["data"] == [item.model_dump(mode="json", exclude={"api_key"})]
    assert body["data"][0]["children"][0]["node_template"]["type"] == "tool"


async def test_service_error_codes_propagate(monkeypatch):
    """服务层的 105xx 业务错误原样透出——CLI 要的是可诊断的错误码，不是 500。

    ``GptsToolsTypeRead`` 的字段全有默认值，"缺名字"这类业务校验在
    ``add_tools`` 里做（那里有自己的测试）；端点层要守的是**错误传播**。
    """
    from bisheng.common.errcode.tool import ToolTypeRepeatError

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    patch_service(monkeypatch, "add_tools", None).side_effect = ToolTypeRepeatError.http_exception()
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", AsyncMock(return_value=True))

    response = await call("POST", "/api/v2/tools", json={"name": "t"})

    assert response.status_code in (400, 422)
    assert response.json()["status_code"] == ToolTypeRepeatError.Code


# ---------------------------------------------------------------------------
# 写请求体是窄契约（G8 R1 M4）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"name": "t", "is_shared": True},  # 租户级共享根——不归请求决定
        {"name": "t", "tenant_id": 5},
        {"name": "t", "user_id": 99},
        {"name": "t", "id": 3},
        {"name": "t", "children": [{"name": "op", "tenant_id": 5}]},
        {"name": "t", "children": [{"name": "op", "user_id": 99}]},
        {"name": "t", "children": [{"name": "op", "type": 8}]},
    ],
)
async def test_register_body_has_no_scope_fields(monkeypatch, body):
    """v2 注册不接受任何身份/作用域字段——多给即被请求校验拒绝，服务方法不被调用。

    v1 ``POST /api/v1/tool`` 用宽松的读模型当请求体，``add_tools`` 覆盖了
    user/tenant/id，但没有覆盖 ``is_shared`` 与子项 ``tenant_id``——新开的 v2
    面不继承这个口子。
    """
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    created = patch_service(monkeypatch, "add_tools", {"id": 5})
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", AsyncMock(return_value=True))

    response = await call("POST", "/api/v2/tools", json=body)

    assert response.status_code in (400, 422), response.text
    created.assert_not_awaited()


async def test_register_hands_service_mapped_rows(monkeypatch):
    """合法请求体到服务层时是 ``GptsToolsTypeRead``，子项是映射行 ``GptsTools``（DAO 直接落库它们）。"""
    from bisheng.tool.domain.models.gpts_tools import GptsTools, GptsToolsTypeRead

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    created = patch_service(monkeypatch, "add_tools", {"id": 5})
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", AsyncMock(return_value=True))

    body = {
        "name": "t",
        "is_preset": 0,  # ToolPresetType.API (1 = PRESET is refused)
        "openapi_schema": "openapi: 3.0.0",
        "children": [{"name": "op", "desc": "d", "tool_key": "k", "api_params": [{"name": "q"}]}],
    }
    response = await call("POST", "/api/v2/tools", json=body)

    assert response.status_code == 200, response.text
    (req,), _ = created.await_args
    assert type(req) is GptsToolsTypeRead
    assert req.is_shared is False
    assert [type(child) for child in req.children] == [GptsTools]
    assert req.children[0].api_params == [{"name": "q"}]


# ---------------------------------------------------------------------------
# G8 第二轮：机器钥匙能读到/能触发的面再收一层
# ---------------------------------------------------------------------------


async def test_list_never_returns_api_key(monkeypatch):
    """清单不回 ``api_key``——一把只有 ``workflow:read`` 的机器钥匙不该读到上游凭据明文。

    v1 浏览器面照旧（编辑表单要回显）；v2 只服务编译器，用不到它。
    """
    from bisheng.tool.domain.models.gpts_tools import GptsToolsTypeListItem

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    login_as_owner(monkeypatch)
    secret = "sk-" + "S" * 20
    item = GptsToolsTypeListItem(id=3, name="t", api_key=secret, children=[])
    patch_service(monkeypatch, "get_tool_list", [item])

    response = await call("GET", "/api/v2/tools")

    assert response.status_code == 200
    assert secret not in response.text
    assert "api_key" not in response.json()["data"][0]
    assert response.json()["data"][0]["name"] == "t"  # 其余字段照常


async def test_schema_parse_does_not_fetch_urls(monkeypatch):
    """``download_url`` 不在 v2 契约里——服务端不替机器钥匙去抓任意 URL（SSRF 面）。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    parse = patch_service(monkeypatch, "parse_openapi_schema", {"name": "x"})

    response = await call("POST", "/api/v2/tools/schema", json={"download_url": "http://169.254.169.254/latest"})

    assert response.status_code in (400, 422), response.text
    parse.assert_not_awaited()


async def test_schema_parse_requires_content(monkeypatch):
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    parse = patch_service(monkeypatch, "parse_openapi_schema", {"name": "x"})

    response = await call("POST", "/api/v2/tools/schema", json={})

    assert response.status_code in (400, 422), response.text
    parse.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"name": "t", "is_preset": 1},  # 预置
        {"name": "t", "is_preset": 2},  # MCP：会主动连调用方给的地址
        {"name": "t", "is_preset": 7},  # 不在枚举里——曾让 DSH 目录 KeyError
        {"name": "t", "api_key": "k" * 1001},
        {"name": "t", "children": [{"name": "o" * 126}]},
        {"name": "t", "children": [{"name": "op", "logo": "l" * 513}]},
    ],
)
async def test_register_body_bounds(monkeypatch, body):
    """越界输入是请求错误，不是落库时的 500；服务方法不被调用。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    created = patch_service(monkeypatch, "add_tools", {"id": 5})
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", AsyncMock(return_value=True))

    response = await call("POST", "/api/v2/tools", json=body)

    assert response.status_code in (400, 422), response.text
    created.assert_not_awaited()


async def test_register_bounds_accept_the_limits(monkeypatch):
    """对照：恰好在上限上的输入照常放行（防"一律拒绝"的假修复）。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    created = patch_service(monkeypatch, "add_tools", {"id": 5})
    monkeypatch.setattr(f"{TOOLS_ENDPOINTS}.QuotaService.check_quota", AsyncMock(return_value=True))

    body = {
        "name": "t",
        "is_preset": 0,
        "api_key": "k" * 1000,
        "children": [{"name": "o" * 125, "logo": "l" * 512}],
    }
    response = await call("POST", "/api/v2/tools", json=body)

    assert response.status_code == 200, response.text
    created.assert_awaited_once()
