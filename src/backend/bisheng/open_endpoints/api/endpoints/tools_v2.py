# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 workflow-as-code — 工具清单与注册端点（``bisheng flow tool`` CLI 消费面）。

三个端点全部委托 ``ToolServices`` 既有方法，本文件不复制任何 OpenAPI 解析或
工具落库逻辑；v1 ``/api/v1/tool`` 继续服务浏览器，这里只换凭据通道。规则
RULE-5 的反面也要守：端点**只**导入 ``bisheng.tool.domain.*``，绝不导入
``bisheng.tool.api.*``。

写面（注册）mode S only，与 ``workflow:write`` 同一把钥匙、同样的委派拒绝。
"""

from fastapi import APIRouter, Request

from bisheng.common.schemas.api import UnifiedResponseModel, resp_200
from bisheng.open_api.domain.scopes import open_api_scope
from bisheng.open_endpoints.domain.schemas.flow import OpenToolSchemaParseRequest, OpenToolTypeCreate
from bisheng.open_endpoints.domain.utils import get_open_api_operator_async
from bisheng.role.domain.services.quota_service import QuotaResourceType, QuotaService
from bisheng.tool.domain.models.gpts_tools import GptsToolsTypeRead
from bisheng.tool.domain.services.tool import ToolServices

router = APIRouter(prefix="/tools", tags=["OpenAPI", "FlowV2"])

_SCOPE_WRITE = "workflow:write"
_MODES: tuple[str, ...] = ("S",)


@router.get("", status_code=200, response_model=UnifiedResponseModel, summary="List usable tools")
@open_api_scope("workflow:read")
async def list_tools():
    """可用工具清单（含子工具）——编译器按 operationId 解析 ``tool_key`` 的依据。"""
    login_user = await get_open_api_operator_async()
    res = await ToolServices(login_user=login_user).get_tool_list(None, "use")
    # 不回 api_key：上游凭据明文不给机器钥匙读 (v1 浏览器编辑表单要回显, 那边照旧)。
    # 编译器只需要类型名、子工具 name/tool_key/node_template。
    return resp_200(data=[item.model_dump(mode="json", exclude={"api_key"}) for item in res])


@router.post(
    "/schema",
    status_code=200,
    response_model=UnifiedResponseModel,
    summary="Parse an OpenAPI document into a tool draft",
)
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def parse_tool_schema(request: Request, body: OpenToolSchemaParseRequest):
    """OpenAPI 文档 → 工具草稿（``flow tool register`` 的第一步，先解析后落库）。"""
    login_user = await get_open_api_operator_async()
    tool_type = await ToolServices(request=request, login_user=login_user).parse_openapi_schema(None, body.file_content)
    return resp_200(data=tool_type)


@router.post("", status_code=200, response_model=UnifiedResponseModel, summary="Register a tool type")
@open_api_scope(_SCOPE_WRITE, modes=_MODES)
async def create_tool(request: Request, body: OpenToolTypeCreate):
    """注册工具类型与子工具。配额与 v1 ``POST /api/v1/tool`` 同一资源类型。"""
    login_user = await get_open_api_operator_async()
    await QuotaService.check_quota(
        user_id=login_user.user_id,
        resource_type=QuotaResourceType.TOOL,
        tenant_id=login_user.tenant_id,
        login_user=login_user,
    )
    # 窄请求体 → 服务层的 v1 模型: children 由此成为映射行 GptsTools (DAO 直接落库它们),
    # 未给出的字段走 v1 模型自己的默认值。
    req = GptsToolsTypeRead.model_validate(body.model_dump(exclude_unset=True))
    tool_type = await ToolServices(request=request, login_user=login_user).add_tools(req)
    return resp_200(data=tool_type)
