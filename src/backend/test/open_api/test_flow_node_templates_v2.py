# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T005 — v2 节点模板端点（``GET /api/v2/flow/node_templates``）。

三件事：

1. 带 ``workflow:read`` scope 的凭据拿到与 v1 **同一份**模板——v1 的响应体是
   ``resp_200(data=NODE_TEMPLATES)``，T003 已钉住它的 ``data`` 就是后端常量；
   这里钉住 v2 的响应体**就是那个常量本身**，传递地等价于"与 v1 逐字段相同"。
2. 缺 scope 的凭据被拒（403 / 26003），拿不到任何模板数据——这条链路是
   ``verify_open_api_access`` 读端点标记强制的，端点自己不再做身份解析。
3. 端点带 scope 标记（``get_open_api_scope_marker``）——路由矩阵测试靠它判定
   "每个 v2 路由都被钥匙墙管住"。

用 ``validate_bearer`` 的接缝注入凭据（照 ``test_dependencies.py`` 的做法），
不碰真实数据库与 Redis。
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from bisheng.open_api.api.dependencies import verify_open_api_access
from bisheng.open_api.api.exception_handlers import register_open_api_exception_handlers
from bisheng.open_api.domain.context import OpenApiPrincipal
from bisheng.open_api.domain.scopes import get_open_api_scope_marker
from bisheng.open_endpoints.api.endpoints.flow_templates import router as flow_templates_router
from bisheng.workflow.common.node_templates import NODE_TEMPLATES


def build_app() -> FastAPI:
    """与 ``api/router.py`` 挂 ``flow_templates_router_rpc`` 的方式逐字一致。"""
    app = FastAPI()
    register_open_api_exception_handlers(app)
    router = APIRouter(prefix="/api/v2", dependencies=[Depends(verify_open_api_access)])
    router.include_router(flow_templates_router)
    app.include_router(router)
    return app


def principal(*, scopes: frozenset[str]) -> OpenApiPrincipal:
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


async def get_node_templates(authorization: str | None):
    headers = {"Authorization": authorization} if authorization is not None else None
    async with AsyncClient(transport=ASGITransport(app=build_app()), base_url="http://test") as client:
        return await client.get("/api/v2/flow/node_templates", headers=headers)


async def test_same_payload_as_v1(monkeypatch):
    """有 scope 的凭据拿到模板，且与 v1 的 ``data`` 逐字相等。

    v1 的响应体是 ``resp_200(data=NODE_TEMPLATES)``（T003 已钉住 data 就是后端常量）；
    v2 走同一个 ``resp_200`` 信封——两边取 ``data`` 比对，等价于"逐字段相同"。
    """

    async def validate(_authorization):
        return principal(scopes=frozenset({"workflow:read"}))

    monkeypatch.setattr("bisheng.open_api.api.dependencies.validate_bearer", validate)
    response = await get_node_templates("Bearer opaque")
    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 200
    assert body["data"] == NODE_TEMPLATES
    assert [tpl["type"] for tpl in body["data"]] == [
        "start",
        "input",
        "output",
        "llm",
        "agent",
        "qa_retriever",
        "rag",
        "knowledge_retriever",
        "report",
        "code",
        "condition",
        "end",
    ]


async def test_missing_scope_is_refused(monkeypatch):
    """缺 ``workflow:read`` 的凭据拿不到数据——403，错误码 26003。"""

    async def validate(_authorization):
        return principal(scopes=frozenset({"knowledge:read"}))

    monkeypatch.setattr("bisheng.open_api.api.dependencies.validate_bearer", validate)
    response = await get_node_templates("Bearer opaque")
    assert response.status_code == 403
    body = response.json()
    assert body["status_code"] == 26003
    # 拒绝时泄漏的只是错误说明（required scope），不是模板数据本身——
    # 逐键核对不含任何模板内容，单个模板 dict 误返回也逃不过。
    assert body["data"]["required"] == "workflow:read"
    assert "group_params" not in body["data"]
    assert not {tpl["type"] for tpl in NODE_TEMPLATES} & set(body["data"])


def test_endpoint_carries_scope_marker():
    """端点带 ``workflow:read`` 标记——钥匙墙的判定依据，不能漏挂。

    ``hosted_app is False`` 是显式钉住默认拒绝语义：应用型主体不认这条路由。
    """
    route = next(route for route in flow_templates_router.routes if route.path == "/flow/node_templates")
    marker = get_open_api_scope_marker(route.endpoint)
    assert marker is not None
    assert marker.scope == "workflow:read"
    assert marker.hosted_app is False


@pytest.mark.parametrize("authorization", [None, "Bearer jwt.header.payload"])
async def test_no_key_no_templates(authorization):
    """没有可用凭据时 401——模板目录不是匿名面。

    用**真实的** ``validate_bearer``：没有钥匙在解析凭据之前就该被拒，
    不能靠伪造凭据来测这条。
    """
    response = await get_node_templates(authorization)
    assert response.status_code == 401
    assert response.json()["status_code"] == 26001
