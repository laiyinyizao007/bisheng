# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T009 — v2 工作流版本读写端点（``/api/v2/flows`` 的写与版本读）。

三类断言，各守一件事：

1. **钥匙墙与身份桥**（不打数据库）：缺 scope 拒（26003）、无归属人拒
   （500，AC-43——拒绝发生在 _principal_user_id，任何"退化成 owner 0"的
   兜底都会让这里变绿）、hosted_app 拒（26052）。走真实 app 与真实身份
   桥，凭据经 ``validate_bearer`` 接缝注入。
2. **业务行为**（真实 FlowService + 真实 DAO，内存 SQLite）：新版本是新增
   一行且不碰既有版本（AC-25）；activate 切换 is_current 并同步 ``Flow.data``
   镜像；线上工作流 activate 得 10525 信封报错而不是假成功（AC-27）。
   身份桥在此打桩（它属于第 1 类的职责），业务判定链保持真实——FGA 调用
   参数被记录，非归属人用例让它抛 403。
3. **契约面**：请求体没有身份字段（AC-42，extra=forbid 打桩成 422）；
   路由标记 mode S only。
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.flow import WorkFlowOnlineEditError
from bisheng.common.errcode.open_api import OpenApiHostedAppEndpointRefusedError
from bisheng.common.schemas.api import resp_200
from bisheng.database.models.flow import Flow
from bisheng.database.models.flow_version import FlowVersion
from bisheng.main import app
from bisheng.open_api.domain.context import OpenApiPrincipal
from bisheng.open_api.domain.scopes import get_open_api_scope_marker

FLOW_ID = "f" * 32
WORKFLOW_ENDPOINTS = "bisheng.open_endpoints.api.endpoints.workflow_write"
FLOW_SERVICE = "bisheng.api.services.flow"

DATA_A = {"nodes": [{"id": "a"}], "edges": []}
DATA_B = {"nodes": [{"id": "b"}], "edges": []}


def principal(*, scopes, actor_kind="service_account", resource_owner_user_id=12, effective_user_id=None):
    return OpenApiPrincipal(
        credential_id=7,
        actor_kind=actor_kind,
        actor_id=31,
        actor_name="cli",
        tenant_id=9,
        resource_owner_user_id=resource_owner_user_id,
        scopes=scopes,
        authorization_subject_type="service_account" if actor_kind != "hosted_app" else "user",
        authorization_subject_id=31,
        effective_user_id=effective_user_id,
    )


def patch_principal(monkeypatch, principal_value):
    async def validate(_authorization):
        return principal_value

    monkeypatch.setattr("bisheng.open_api.api.dependencies.validate_bearer", validate)


def login_as_owner(monkeypatch, user_id=12, tenant_id=1):
    """身份桥打桩——桥自身的拒绝语义由本文件第 1 类用例与仓库既有测试守。"""
    user = UserPayload(user_id=user_id, user_name="owner", user_role=[], tenant_id=tenant_id)
    monkeypatch.setattr(f"{WORKFLOW_ENDPOINTS}.get_open_api_operator_async", AsyncMock(return_value=user))
    return user


@pytest.fixture()
def flow_db(monkeypatch, tmp_path):
    """函数级 SQLite（临时文件）：flow + flow_version 两张表，DAO 的会话工厂全部改道。

    事务回滚式夹具（conftest 的 db_session）在这里不可用——DAO 内部显式
    commit，会把外层事务一起提交。fresh DB per test 则不需要回滚。
    同步与异步 DAO 各自持连接，所以用文件库（同一文件）而不是内存库——
    StaticPool 的内存库在两个池之间是两份独立数据库，数据互不可见。
    """
    db_file = tmp_path / "flow_test.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Flow.metadata.create_all(engine, tables=[Flow.__table__])
    FlowVersion.metadata.create_all(engine, tables=[FlowVersion.__table__])
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    @contextlib.contextmanager
    def fake_sync():
        session = Session(engine, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()

    @contextlib.asynccontextmanager
    async def fake_async():
        yield AsyncSession(async_engine, expire_on_commit=False)

    for module in ("bisheng.database.models.flow", "bisheng.database.models.flow_version"):
        monkeypatch.setattr(f"{module}.get_sync_db_session", fake_sync)
        monkeypatch.setattr(f"{module}.get_async_db_session", fake_async)
    # FlowService.create_workflow 自己也直接拿会话（重名检查）
    monkeypatch.setattr(f"{FLOW_SERVICE}.get_sync_db_session", fake_sync)

    yield engine
    engine.dispose()
    # aiosqlite 的连接挂在后台线程上，测试用例的循环已关——同步 dispose 即可，
    # 临时文件本身随 tmp_path 清理。
    async_engine.sync_engine.dispose()


@pytest.fixture()
def service_seams(monkeypatch):
    """业务判定与遥测的接缝：FGA 调用被记录（归属人断言靠它），遥测静音。"""
    fga = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{FLOW_SERVICE}.require_business_action", fga)
    monkeypatch.setattr(f"{FLOW_SERVICE}.telemetry_service.log_event", AsyncMock())
    return fga


def seed_flow(engine, *, status=1, data=DATA_A):
    with Session(engine, expire_on_commit=False) as session:
        flow = Flow(id=FLOW_ID, name="wf", user_id=12, tenant_id=1, data=data, status=status, flow_type=10)
        session.add(flow)
        session.commit()
    return flow


def seed_version(engine, *, name, data, is_current=0):
    with Session(engine, expire_on_commit=False) as session:
        version = FlowVersion(flow_id=FLOW_ID, name=name, data=data, user_id=12, flow_type=10, is_current=is_current)
        session.add(version)
        session.commit()
        session.refresh(version)
        return version


def read_row(engine, model, row_id):
    with Session(engine, expire_on_commit=False) as session:
        return session.exec(select(model).where(model.id == row_id)).first()


async def call(method, path, *, json=None, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=json, headers=headers)


AUTH = {"Authorization": "Bearer bs-sak-x"}


# ---------------------------------------------------------------------------
# 1. 钥匙墙与身份桥（不打数据库）
# ---------------------------------------------------------------------------


async def test_missing_scope_refused(monkeypatch):
    """只有 ``workflow:read`` 的钥匙写不进去——403/26003，指名缺哪个 scope。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions", json={"name": "v2"}, headers=AUTH)
    assert response.status_code == 403
    body = response.json()
    assert body["status_code"] == 26003
    assert body["data"]["required"] == "workflow:write"


async def test_no_resource_owner_refused(monkeypatch):
    """AC-43：无归属人的钥匙连"它能对谁行使写权"都没有——500 拒绝。

    显式拒绝而非退化成 owner 0（那会让所有权比对放进任何无主行）。
    """
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"}), resource_owner_user_id=None))
    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions", json={"name": "v2"}, headers=AUTH)
    assert response.status_code == 500
    assert "no compatible resource owner" in response.text


async def test_hosted_app_refused(monkeypatch):
    """AC-52 形状：hosted_app 不经身份桥执行任何自然人的写路径——26052。"""
    patch_principal(
        monkeypatch,
        principal(
            scopes=frozenset({"workflow:write"}),
            actor_kind="hosted_app",
            resource_owner_user_id=12,
        ),
    )
    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions", json={"name": "v2"}, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["status_code"] == OpenApiHostedAppEndpointRefusedError.Code


async def test_non_owner_edit_refused(monkeypatch, flow_db, service_seams):
    """AC-40 fail-closed：FGA edit 判定不过（归属人不是这个工作流的编辑者）→ 403。"""
    from bisheng.common.errcode.http_error import UnAuthorizedError

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db)
    service_seams.side_effect = UnAuthorizedError.http_exception()

    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions", json={"name": "v2"}, headers=AUTH)

    assert response.status_code == 403
    assert "No permission to operate" in response.text
    # 拒绝发生在业务判定——没有任何版本行被创建
    with Session(flow_db, expire_on_commit=False) as session:
        assert session.exec(select(FlowVersion)).all() == []


# ---------------------------------------------------------------------------
# 2. 业务行为（真实 FlowService + 真实 DAO）
# ---------------------------------------------------------------------------


async def test_create_version_does_not_touch_existing_versions(monkeypatch, flow_db, service_seams):
    """AC-25：push 出的新版本是新增一行；既有版本的 data/is_current 原样。

    AC-28：FGA edit 判定收到的是凭据归属人（user_id=12），不是服务账号。
    """
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db)
    existing = seed_version(flow_db, name="v1", data=DATA_A, is_current=1)

    response = await call(
        "POST", f"/api/v2/flows/{FLOW_ID}/versions", json={"name": "v2", "data": DATA_B}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["status_code"] == 200
    created = response.json()["data"]
    assert created["name"] == "v2"
    assert created["data"] == DATA_B
    assert created["flow_type"] == 10  # 类型钉死为工作流，不收自请求
    assert created["user_id"] == 12

    untouched = read_row(flow_db, FlowVersion, existing.id)
    assert untouched.data == DATA_A
    assert untouched.is_current == 1  # 是否当前版本只由 activate 改，创建不改

    # FGA 判定收到归属人身份
    assert service_seams.await_count == 1
    args, call_kwargs = service_seams.await_args
    assert call_kwargs["resource_type"] == "workflow"
    assert call_kwargs["action"] == "edit"
    assert args[0].user_id == 12


async def test_activate_updates_flow_data(monkeypatch, flow_db, service_seams):
    """AC-26/AC-40：activate 把目标版本切为当前，并把 ``Flow.data`` 镜像同步。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db, status=1, data=DATA_A)
    v1 = seed_version(flow_db, name="v1", data=DATA_A, is_current=1)
    v2 = seed_version(flow_db, name="v2", data=DATA_B)
    monkeypatch.setattr(f"{FLOW_SERVICE}.FlowService.update_flow_hook", AsyncMock(return_value=True))

    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions/{v2.id}/activate", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status_code"] == 200

    assert read_row(flow_db, FlowVersion, v2.id).is_current == 1
    assert read_row(flow_db, FlowVersion, v1.id).is_current == 0
    assert read_row(flow_db, Flow, FLOW_ID).data == DATA_B


async def test_activate_online_workflow_returns_10525(monkeypatch, flow_db, service_seams):
    """AC-27：线上工作流不悄悄改版本——10525 信封报错（v1 同一错误码）。

    HTTP 200 + status_code=10525 是 v1 的既有形状，CLI 按 status_code 分支。
    """
    from unittest.mock import AsyncMock

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db, status=2, data=DATA_A)  # FlowStatus.ONLINE
    v1 = seed_version(flow_db, name="v1", data=DATA_A, is_current=1)
    v2 = seed_version(flow_db, name="v2", data=DATA_B)
    hook = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{FLOW_SERVICE}.FlowService.update_flow_hook", hook)

    response = await call("POST", f"/api/v2/flows/{FLOW_ID}/versions/{v2.id}/activate", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["status_code"] == WorkFlowOnlineEditError.Code
    # 拒绝是完整的：当前版本没换，镜像没动，hook 没跑
    assert read_row(flow_db, FlowVersion, v1.id).is_current == 1
    assert read_row(flow_db, FlowVersion, v2.id).is_current == 0
    assert read_row(flow_db, Flow, FLOW_ID).data == DATA_A
    hook.assert_not_called()


async def test_read_endpoints_need_only_read_scope(monkeypatch, service_seams):
    """diff 基准读面只要求 ``workflow:read``——写 scope 不是读的前提。"""
    login_as_owner(monkeypatch)
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:read"})))
    monkeypatch.setattr(
        f"{FLOW_SERVICE}.FlowService.get_version_list_by_flow", AsyncMock(return_value=resp_200(data={"data": []}))
    )

    response = await call("GET", f"/api/v2/flows/{FLOW_ID}/versions", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["data"] == {"data": []}


# ---------------------------------------------------------------------------
# 3. 契约面
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "payload", "field"),
    [
        ("/api/v2/flows", {"name": "wf", "user_id": 999}, "user_id"),
        ("/api/v2/flows", {"name": "wf", "flow_type": 5}, "flow_type"),
        (f"/api/v2/flows/{FLOW_ID}/versions", {"name": "v2", "user_id": 999}, "user_id"),
    ],
)
async def test_no_impersonation_entry_point(monkeypatch, path, payload, field):
    """AC-42：请求体里没有"指定他人身份/类型"的字段——多给即被校验拒绝。

    extra=forbid 把"被静默忽略的多余字段"变成显式拒绝：CLI 编译器对着
    schema 写请求，多出的字段只可能是错位，不能装作没看见。仓库把请求
    校验错误统一翻成 400（部分代理层是 422），错误载荷里指名该字段。
    """
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    response = await call("POST", path, json=payload, headers=AUTH)
    assert response.status_code in (400, 422), f"{field} 被接受了——schema 出现了身份字段"
    # 两条防线各挡一层：http_middleware 对"身份输入"直接 26019（user_id 这类），
    # 请求 schema 的 extra=forbid 再挡其余多出字段（flow_type 等）。
    assert field in response.text or "26019" in response.text


def test_write_markers_are_mode_s_only():
    """路由标记即钥匙墙的判定依据：六个写路径 mode S only、拒绝 hosted_app。

    读面保持默认（S+D 可读）。这个测试钉的是**标记**而非行为——行为由
    上面的用例守，标记由路由矩阵测试与这里共同守。
    """
    expected = {
        ("POST", "/api/v2/flows"): ("workflow:write", {"S"}),
        ("POST", "/api/v2/flows/{flow_id}/versions"): ("workflow:write", {"S"}),
        ("PUT", "/api/v2/flows/{flow_id}/versions/{version_id}"): ("workflow:write", {"S"}),
        ("POST", "/api/v2/flows/{flow_id}/versions/{version_id}/activate"): ("workflow:write", {"S"}),
        ("POST", "/api/v2/tools"): ("workflow:write", {"S"}),
        ("POST", "/api/v2/tools/schema"): ("workflow:write", {"S"}),
        ("GET", "/api/v2/flows/{flow_id}/versions"): ("workflow:read", {"S", "D"}),
        ("GET", "/api/v2/flows/{flow_id}/versions/{version_id}"): ("workflow:read", {"S", "D"}),
        ("GET", "/api/v2/tools"): ("workflow:read", {"S", "D"}),
    }
    seen = {}
    from fastapi.routing import APIRoute

    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v2"):
            continue
        marker = get_open_api_scope_marker(route.endpoint)
        if marker is None:
            continue
        for method in route.methods:
            if (method, route.path) in expected:
                seen[(method, route.path)] = (marker.scope, set(marker.modes))
                assert marker.hosted_app is False, f"{method} {route.path} 不应对 hosted_app 放行"
    assert seen == expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v2/flows"),
        ("POST", f"/api/v2/flows/{FLOW_ID}/versions"),
        ("PUT", f"/api/v2/flows/{FLOW_ID}/versions/1"),
    ],
)
@pytest.mark.parametrize("data", [{"nodes": []}, {"edges": []}, "not-a-graph"])
async def test_malformed_graph_is_a_request_error(monkeypatch, method, path, data):
    """G8 R1 M2：``data`` 缺 nodes/edges 是**请求**错误（400/422），不是 500。

    v1 在 FastAPI 边界校验 ``FlowCreate``；v2 若在端点体内才构造它，
    pydantic 的 ValidationError 不走请求校验处理器，变成 500。拒绝必须发生在
    任何业务调用之前——身份桥都不该被走到。
    """
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write"})))
    bridge = AsyncMock(side_effect=AssertionError("request validation must reject before the identity bridge"))
    monkeypatch.setattr(f"{WORKFLOW_ENDPOINTS}.get_open_api_operator_async", bridge)

    response = await call(method, path, json={"name": "wf", "data": data}, headers=AUTH)

    assert response.status_code in (400, 422), response.text
    bridge.assert_not_called()


OTHER_FLOW_ID = "e" * 32


async def test_version_of_another_flow_is_not_found(monkeypatch, flow_db, service_seams):
    """G8 R1 M3：路径里的 flow_id 不是摆设——拿 A 的路径改 B 的版本必须 404，B 原样。

    授权本来就按版本的真实归属判（不会越权），但"URL 说改 A、实际改了 B
    并回 200"会让 CLI 写错 id 时悄悄改坏另一条工作流。与 activate 同一语义。
    """
    from bisheng.common.errcode.flow import NotFoundVersionError

    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write", "workflow:read"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db)
    version_of_b = seed_version(flow_db, name="v1", data=DATA_A)

    put = await call(
        "PUT", f"/api/v2/flows/{OTHER_FLOW_ID}/versions/{version_of_b.id}", json={"data": DATA_B}, headers=AUTH
    )
    get = await call("GET", f"/api/v2/flows/{OTHER_FLOW_ID}/versions/{version_of_b.id}", headers=AUTH)

    for response in (put, get):
        assert response.status_code == 200
        assert response.json()["status_code"] == NotFoundVersionError.Code
    assert read_row(flow_db, FlowVersion, version_of_b.id).data == DATA_A


async def test_version_of_own_flow_updates(monkeypatch, flow_db, service_seams):
    """对照用例：路径与版本归属一致时照常更新（防"一律 404"的假修复）。"""
    patch_principal(monkeypatch, principal(scopes=frozenset({"workflow:write", "workflow:read"})))
    login_as_owner(monkeypatch)
    seed_flow(flow_db)
    version = seed_version(flow_db, name="v1", data=DATA_A)
    monkeypatch.setattr(f"{FLOW_SERVICE}.FlowService.update_flow_hook", AsyncMock(return_value=True))

    put = await call("PUT", f"/api/v2/flows/{FLOW_ID}/versions/{version.id}", json={"data": DATA_B}, headers=AUTH)
    get = await call("GET", f"/api/v2/flows/{FLOW_ID}/versions/{version.id}", headers=AUTH)

    assert put.json()["status_code"] == 200
    assert get.json()["status_code"] == 200
    assert get.json()["data"]["data"] == DATA_B
    assert read_row(flow_db, FlowVersion, version.id).data == DATA_B
