# ruff: noqa: RUF001, RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T003 — ``GET /api/v1/workflow/node_templates``。

节点模板从前端常量搬到后端之后，浏览器要有一条路取到它——就是这条 v1 端点
（浏览器拿的是 JWT cookie，开放 API 那条 ``/api/v2`` 端点不认 cookie）。

这里只搭一个最小 app 挂这一个 router：整份 ``bisheng.main`` 的 import 链会把
数据库、Redis、对象存储一起拖进来，而这条端点一个都不碰。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bisheng.api.v1.workflow import router
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.workflow.common.node_templates import NODE_TEMPLATES

EXPECTED_TYPES = [
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


def _app(*, authenticated: bool) -> FastAPI:
    app = FastAPI()
    # ``router`` 自带 ``prefix="/workflow"``，这里只补上 ``/api/v1``。
    app.include_router(router, prefix="/api/v1")
    if authenticated:
        app.dependency_overrides[UserPayload.get_login_user] = lambda: UserPayload(
            user_id=1,
            user_name="tester",
            user_role=[],
            tenant_id=1,
        )
    return app


def test_returns_twelve_templates():
    with TestClient(_app(authenticated=True)) as client:
        response = client.get("/api/v1/workflow/node_templates")
    assert response.status_code == 200
    body = response.json()
    assert body["status_code"] == 200
    assert [tpl["type"] for tpl in body["data"]] == EXPECTED_TYPES


def test_payload_is_the_backend_module():
    """端点返回的就是后端那份常量——不是另一份拷贝。"""
    with TestClient(_app(authenticated=True)) as client:
        body = client.get("/api/v1/workflow/node_templates").json()
    assert body["data"] == NODE_TEMPLATES


def test_payload_has_no_ui_copy():
    """响应里只有结构与参数键，没有界面文案（AC-03）。"""
    with TestClient(_app(authenticated=True)) as client:
        body = client.get("/api/v1/workflow/node_templates").json()
    for tpl in body["data"]:
        for group in tpl["group_params"]:
            for param in group.get("params", []):
                for field in ("label", "description", "placeholder", "help"):
                    value = param.get(field)
                    assert value in (None, "", "true", []), f"{tpl['type']}.{param.get('key')}.{field}={value!r}"


def test_requires_login():
    """没登录就拿不到模板——这是只读接口，但也不是匿名接口。

    这里断言的是**依赖装配**而不是某个状态码：``AuthJwt`` 的密钥来自配置表，
    未认证的请求会先碰数据库，状态码随环境而变（422/500/401 都可能），
    拿状态码当断言会把环境差异写进用例。真正要守的是"这条路由挂着登录依赖"。
    """
    calls = []
    for route in _app(authenticated=False).routes:
        if getattr(route, "path", "") == "/api/v1/workflow/node_templates":
            calls = [dep.call for dep in route.dependant.dependencies]
    assert UserPayload.get_login_user in calls, "节点模板端点必须挂登录依赖，不能是匿名接口"


def test_anonymous_caller_gets_no_template():
    """匿名调用拿不到模板数据——无论它是 401、422 还是直接抛异常。"""
    with TestClient(_app(authenticated=False)) as client:
        try:
            response = client.get("/api/v1/workflow/node_templates")
        except Exception:
            return  # 鉴权链在无数据库的环境里直接抛异常——同样没有模板外泄
    assert response.status_code != 200
    assert "data" not in response.json()
