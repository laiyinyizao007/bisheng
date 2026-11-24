# ruff: noqa: RUF002 - Chinese prose, full-width punctuation is intended
"""F005 workflow-as-code — v2 节点模板端点（CLI 消费面）。

节点模板的唯一真相源是 ``bisheng.workflow.common.node_templates`` 的
``NODE_TEMPLATES``。v1 的 ``GET /api/v1/workflow/node_templates`` 服务浏览器
（JWT cookie），本文件把**同一份**数据经开放 API 暴露给 ``bisheng`` CLI
（Bearer API key）——两处消费、一个数据源，模板变更不再需要两处同步。

这是纯静态读：不解析身份、不碰数据库。鉴权与 scope 由 ``router_rpc`` 上的
``verify_open_api_access`` 依端点标记强制——没有 ``workflow:read`` scope 的
凭据在这里拿不到任何数据，端点自身不需要也不应该再碰身份。
"""

from fastapi import APIRouter

from bisheng.common.schemas.api import resp_200
from bisheng.open_api.domain.scopes import open_api_scope
from bisheng.workflow.common.node_templates import NODE_TEMPLATES

router = APIRouter(prefix="/flow", tags=["OpenAPI", "FlowV2"])


@router.get("/node_templates", status_code=200)
@open_api_scope("workflow:read")
async def get_node_templates():
    """工作流节点模板——与 v1 ``GET /api/v1/workflow/node_templates`` 同一份数据。

    响应走 v2 既有读端点的 ``resp_200`` 信封（design.md 手动验证用 ``.data`` 取数），
    与 ``GET /api/v2/flows/{flow_id}`` 保持同一形状。
    """
    return resp_200(data=NODE_TEMPLATES)
