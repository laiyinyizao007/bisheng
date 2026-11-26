"""Which tenant a new workflow (and its v0 version) is written to.

Bug (2026-11-25, isolated e2e stack): a global super admin whose management view
(F019 admin scope) was another tenant created a workflow from the browser. The row was
stamped with the admin's own tenant (``login_user.tenant_id``) while the request's tenant
context was the managed one, so the read-back after the commit was filtered out and the
request ended in 500 ("Could not refresh instance '<Flow>'"); the v0 version, filled from
the context, landed in the other tenant.

Writing it into the managed tenant instead was tried and rejected (user decision,
2026-11-25): the editor's business routes (guest link, test run, copilot, report) are not
management routes and run in the user's own tenant (F019 AC-07), so such a workflow opens
half broken. Creating while managing another tenant is refused up front with 10570, like
knowledge-base creation refuses a changed tenant; otherwise the workflow and its v0 version
are the user's own tenant.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from bisheng.api.services.flow import FlowService
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.flow import WorkflowCreateOutsideOwnTenantError
from bisheng.core.context import tenant as tenant_context
from bisheng.database.models.flow import Flow, FlowCreate
from bisheng.database.models.flow_version import FlowVersion, FlowVersionDao
from test.open_api.test_workflow_write_v2 import FLOW_SERVICE, flow_db, service_seams  # noqa: F401  (fixtures)

ADMIN = UserPayload(user_id=1, user_name="admin", user_role=[1], tenant_id=1)


@contextlib.contextmanager
def request_context(*, tenant: int | None, scope: int | None = None, management: bool = False):
    """The context the HTTP middlewares set: JWT tenant, admin-scope override, management route."""
    tokens = [
        (tenant_context.current_tenant_id, tenant_context.current_tenant_id.set(tenant)),
        (tenant_context._admin_scope_tenant_id, tenant_context._admin_scope_tenant_id.set(scope)),
        (tenant_context._is_management_api, tenant_context._is_management_api.set(management)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


@pytest.fixture()
def no_hook(monkeypatch):
    monkeypatch.setattr(f"{FLOW_SERVICE}.FlowService.create_flow_hook", AsyncMock(return_value=True))


def stored(engine, name: str) -> tuple[Flow, FlowVersion]:
    # Read across tenants: should the tenant filter be registered, a plain read would be
    # pinned to one tenant and miss the row whose tenant is the very thing under test.
    with tenant_context.bypass_tenant_filter(), Session(engine) as session:
        flow = session.exec(select(Flow).where(Flow.name == name)).one()
        version = session.exec(select(FlowVersion).where(FlowVersion.flow_id == flow.id)).one()
        return flow, version


async def create(name: str) -> None:
    await FlowService.create_workflow(None, ADMIN, FlowCreate(name=name, data={"nodes": [], "edges": []}))


async def test_refused_while_managing_another_tenant(flow_db, service_seams, no_hook) -> None:  # noqa: F811
    # The editor's business routes (guest link, test run, copilot, report) run in the
    # user's own tenant (F019 AC-07), so a workflow created in the managed tenant would
    # open half broken. Refuse up front, before anything is written.
    with request_context(tenant=1, scope=3, management=True), pytest.raises(HTTPException) as refused:
        await create("wf")
    assert refused.value.status_code == WorkflowCreateOutsideOwnTenantError.Code
    with tenant_context.bypass_tenant_filter(), Session(flow_db) as session:
        assert session.exec(select(Flow)).all() == []
        assert session.exec(select(FlowVersion)).all() == []


async def test_managing_your_own_tenant_is_not_refused(flow_db, service_seams, no_hook) -> None:  # noqa: F811
    with request_context(tenant=1, scope=1, management=True):
        await create("wf")
    flow, version = stored(flow_db, "wf")
    assert (flow.tenant_id, version.tenant_id) == (1, 1)


async def test_without_a_management_view_it_is_the_users_tenant(flow_db, service_seams, no_hook) -> None:  # noqa: F811
    with request_context(tenant=1):
        await create("wf")
    flow, version = stored(flow_db, "wf")
    assert (flow.tenant_id, version.tenant_id) == (1, 1)


async def test_a_scope_outside_a_management_route_is_ignored(flow_db, service_seams, no_hook) -> None:  # noqa: F811
    # F019 AC-07: the override applies to management routes only.
    with request_context(tenant=1, scope=3, management=False):
        await create("wf")
    assert stored(flow_db, "wf")[0].tenant_id == 1


def test_a_new_version_follows_its_workflows_tenant(flow_db) -> None:  # noqa: F811
    # Not left to the context fill-in: a version always lives with its workflow.
    with Session(flow_db, expire_on_commit=False) as session:
        session.add(Flow(id="f1", name="wf", user_id=1, tenant_id=7, data={}, status=1, flow_type=10))
        session.commit()
    FlowVersionDao.create_version(FlowVersion(name="v1", flow_id="f1", user_id=1, data={}, flow_type=10, is_current=0))
    with tenant_context.bypass_tenant_filter(), Session(flow_db) as session:
        assert session.exec(select(FlowVersion.tenant_id).where(FlowVersion.name == "v1")).one() == 7


async def test_without_any_context_it_falls_back_to_the_users_tenant(flow_db, service_seams, no_hook) -> None:  # noqa: F811
    # CLI / scripts: no HTTP middleware has set a tenant.
    with request_context(tenant=None):
        await create("wf")
    flow, version = stored(flow_db, "wf")
    assert (flow.tenant_id, version.tenant_id) == (1, 1)
