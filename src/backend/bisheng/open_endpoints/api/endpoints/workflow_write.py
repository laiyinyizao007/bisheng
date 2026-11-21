# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 workflow-as-code — 工作流版本读写端点（``bisheng flow`` CLI 消费面）。

四写两读，全部挂在 ``router_rpc`` 的单一鉴权管线上：凭据 → scope 标记 →
身份模式在端点体运行前判定，端点体只做三件事——

1. 经 ``get_open_api_operator_async`` 桥接出操作者身份（资源归属人，AC-42：
   不存在任何"指定他人身份"的入口，请求体里也没有身份字段——schema 层
   ``extra="forbid"`` 把这类字段直接拒掉）；
2. 把业务判定**全部**委托给 ``FlowService`` 既有方法——scope 说"这把钥匙
   可以试写工作流"，``judge_flow_write_permission`` 的 FGA ``edit`` 判定说
   "这个归属人可以改这个工作流"，两层缺一不可（AC-40 fail-closed）；
3. 不新增任何 DAO/错误码——105xx 既有段覆盖全部场景（design C5）。

``modes=("S",)``：委派钥匙（mode D）不得写工作流——以他人名义 push 正是
AC-42 要封死的入口，与 ``app:manage`` 的 INV-31 形状一致。
"""

from uuid import UUID

from fastapi import APIRouter, Request

from bisheng.api.services.flow import FlowService
from bisheng.api.v1.schemas import FlowVersionCreate
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.schemas.api import UnifiedResponseModel
from bisheng.database.models.flow import FlowCreate
from bisheng.database.models.role_access import WebMenuResource
from bisheng.open_api.domain.scopes import open_api_scope
from bisheng.open_endpoints.domain.schemas.flow import OpenFlowCreate, OpenFlowVersionCreate
from bisheng.open_endpoints.domain.utils import get_open_api_operator_async
from bisheng.role.domain.services.quota_service import QuotaResourceType, QuotaService

router = APIRouter(prefix="/flows", tags=["OpenAPI", "FlowV2"])

_SCOPE_WRITE = "workflow:write"
#: INV-31 形状——写面 mode S only。委派调用在管线就被 26006 拒掉，
#: 这里再钉一层标记，路由矩阵测试读的是这个标记。
_MODES: tuple[str, ...] = ("S",)


def _normalize_flow_id(flow_id: UUID) -> str:
    """与既有 ``GET /api/v2/flows/{flow_id}`` 同一归一化：UUID → 无连字符 hex。"""
    return flow_id.hex


@router.post("", status_code=201, response_model=UnifiedResponseModel, summary="Create a workflow")
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def create_workflow(request: Request, body: OpenFlowCreate):
    """创建工作流——v1 ``POST /api/v1/workflow/create`` 的开放 API 通道。

    门槛与 v1 对齐（不因换了凭据类型而放松）：创建门槛是"新建应用"菜单
    （超管短路），配额走 ``WORKFLOW`` 资源类型；剩下的编排在
    ``FlowService.create_workflow`` 里，与 v1 同一份实现。
    """
    login_user = await get_open_api_operator_async()
    if not login_user.is_admin():
        await UserPayload.assert_effective_web_menu_contains(login_user.user_id, WebMenuResource.CREATE_APP.value)
    await QuotaService.check_quota(
        user_id=login_user.user_id,
        resource_type=QuotaResourceType.WORKFLOW,
        tenant_id=login_user.tenant_id,
        login_user=login_user,
    )
    # 请求 schema 没有 user_id/flow_type，这里显式只搬业务四字段——
    # 身份与类型归属是服务端的，不是请求的。
    flow = FlowCreate(name=body.name, description=body.description, data=body.data, logo=body.logo)
    return await FlowService.create_workflow(request, login_user, flow)


@router.post(
    "/{flow_id}/versions", status_code=200, response_model=UnifiedResponseModel, summary="Create a workflow version"
)
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def create_version(flow_id: UUID, body: OpenFlowVersionCreate):
    """创建新版本（AC-25：新版本永远是新增一行，不覆盖既有版本）。"""
    login_user = await get_open_api_operator_async()
    # flow_type 不收自请求：这个面只写工作流版本，类型是服务端钉死的。
    flow_version = FlowVersionCreate(**body.model_dump())
    return await FlowService.create_new_version(login_user, _normalize_flow_id(flow_id), flow_version)


@router.put(
    "/{flow_id}/versions/{version_id}",
    status_code=200,
    response_model=UnifiedResponseModel,
    summary="Update a workflow version",
)
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def update_version(request: Request, flow_id: UUID, version_id: int, body: OpenFlowVersionCreate):
    """更新既有版本内容（线上当前版本会被 10525 拒——先下线再改）。"""
    login_user = await get_open_api_operator_async()
    flow_version = FlowVersionCreate(**body.model_dump())
    # 路径上的 flow_id 参与判定：版本不属于它 → 未找到 (与 activate 同语义), 写之前就拒
    return await FlowService.update_version_info(
        request, login_user, version_id, flow_version, expected_flow_id=_normalize_flow_id(flow_id)
    )


@router.post(
    "/{flow_id}/versions/{version_id}/activate",
    status_code=200,
    response_model=UnifiedResponseModel,
    summary="Switch the workflow's current version",
)
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def activate_version(request: Request, flow_id: UUID, version_id: int):
    """把某版本切为当前版本；线上工作流被 10525 拒（AC-27：可操作的报错，不是假成功）。"""
    login_user = await get_open_api_operator_async()
    return await FlowService.change_current_version(request, login_user, _normalize_flow_id(flow_id), version_id)


@router.get(
    "/{flow_id}/versions", status_code=200, response_model=UnifiedResponseModel, summary="List workflow versions"
)
@open_api_scope("workflow:read")
async def get_versions(flow_id: UUID):
    """版本列表——``bisheng flow diff`` 的基准数据（AC-29：报版本 id 与名字）。"""
    login_user = await get_open_api_operator_async()
    return await FlowService.get_version_list_by_flow(login_user, _normalize_flow_id(flow_id))


@router.get(
    "/{flow_id}/versions/{version_id}",
    status_code=200,
    response_model=UnifiedResponseModel,
    summary="Read one workflow version",
)
@open_api_scope("workflow:read")
async def get_version_info(flow_id: UUID, version_id: int):
    """读任意一个版本——push 前核对"线上那个版本到底是什么"。"""
    login_user = await get_open_api_operator_async()
    return await FlowService.get_version_info(login_user, version_id, expected_flow_id=_normalize_flow_id(flow_id))
