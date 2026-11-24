import json
import time
from typing import Any, Literal, Union

import jwt
from fastapi import APIRouter, Body, Depends, Query, Request, WebSocket, WebSocketException
from fastapi import status as http_status
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field

from bisheng.api.services import report_template
from bisheng.api.services.flow import FlowService
from bisheng.api.services.office_callback import afetch_office_document
from bisheng.api.services.workflow import WorkFlowService
from bisheng.api.v1.chat import chat_manager
from bisheng.api.v1.schemas import FlowVersionCreate, resp_200
from bisheng.common.chat.types import WorkType
from bisheng.common.constants.enums.telemetry import BaseTelemetryTypeEnum
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.flow import AppWriteAuthError, WorkflowCopilotModelCallError, WorkFlowOnlineEditError
from bisheng.common.errcode.http_error import NotFoundError, ServerError, UnAuthorizedError
from bisheng.common.services import telemetry_service
from bisheng.common.services.config_service import settings as bisheng_settings
from bisheng.core.logger import trace_id_var
from bisheng.core.storage.minio.minio_manager import get_minio_storage
from bisheng.database.models.assistant import AssistantDao
from bisheng.database.models.flow import FlowCreate, FlowDao, FlowStatus, FlowType, FlowUpdate
from bisheng.permission.application.business_authorization import (
    check_business_action,
    require_business_action,
)
from bisheng.role.domain.services.quota_service import QuotaResourceType, require_quota
from bisheng.share_link.api.dependencies import header_share_token_parser
from bisheng.share_link.domain.models.share_link import ShareLink
from bisheng.utils import generate_uuid
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng.workflow.domain.services.copilot.resources import check_default_model, load_catalogue
from bisheng.workflow.domain.services.copilot.service import WorkflowCopilotService
from bisheng_langchain.utils.requests import Requests

router = APIRouter(prefix="/workflow", tags=["Workflow"])

# Command-service rejection codes, per the document server's documented set.
# Anything else collapses to the generic message -- the user can only ever
# retry or reopen the template anyway.
FORCE_SAVE_ERROR_MESSAGES = {
    1: "The template is not open in the editor, please reopen it and retry",
    2: "The document service rejected the callback address",
    3: "The document service failed to save the template",
    5: "The save request was malformed",
    6: "The document service rejected our signature, check office_jwt_secret",
}
# "No changes since the last save" -- nothing to flush, so the template on
# storage already matches what the user sees. Report it as saved, not failed.
FORCE_SAVE_NO_CHANGES = 4


@router.get("/node_templates")
async def get_node_templates(
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """工作流节点模板——12 类节点的模板定义。

    模板由后端持有——``bisheng.workflow.common.node_templates``——前端与命令行工具
    从这里取同一份数据。响应里只有结构与参数键——界面文案由前端本地化资源提供。
    """
    return resp_200(data=NODE_TEMPLATES)


class WorkflowCopilotCanvas(BaseModel):
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    viewport: dict[str, Any] | None = None


class WorkflowCopilotTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class WorkflowCopilotRequest(BaseModel):
    flow_id: str
    canvas: WorkflowCopilotCanvas
    history: list[WorkflowCopilotTurn] = Field(default_factory=list)
    message: str = Field(min_length=1, max_length=4_000)


@router.get("/copilot/availability")
async def workflow_copilot_availability(
    flow_id: str = Query(..., description="Workflow id"),
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """F007 AC-03: can the copilot run here? Checked when the drawer opens.

    Same gates as a turn, without listing resources or calling a model: edit
    permission, then the workflow default model (10560 not configured / 10561
    deleted or offline), each as an ordinary error envelope.
    """
    await FlowService.judge_flow_write_permission(login_user, flow_id)
    await check_default_model()
    return resp_200(data={"available": True})


@router.post("/copilot")
async def workflow_copilot(
    request: Request,
    payload: WorkflowCopilotRequest,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """F007: one copilot turn on the editor's canvas, streamed as NDJSON.

    Errors known before the stream starts — no edit permission (AC-04), the
    workflow default model not configured / unavailable (10560 / 10561), input too
    large (10562) — are ordinary JSON envelopes and call no model. Events are
    documented in ``copilot/service.py``; the stream always ends with ``done``.
    Nothing is saved: the proposal only lands when the user applies it.
    """
    flow = await FlowService.judge_flow_write_permission(login_user, payload.flow_id)
    catalogue = await load_catalogue(request=request, login_user=login_user)
    turn = WorkflowCopilotService.prepare(
        flow_id=payload.flow_id,
        flow_name=getattr(flow, "name", "") or payload.flow_id,
        canvas=payload.canvas.model_dump(),
        history=[t.model_dump() for t in payload.history],
        message=payload.message,
        catalogue=catalogue,
        code_node_enabled=_code_node_enabled(),
        user_id=login_user.user_id,
    )

    async def events():
        try:
            async for event in WorkflowCopilotService.stream(turn):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as exc:  # the stream has started: report in-band, never the exception text
            logger.warning(f"workflow copilot stream failed flow={payload.flow_id} error={type(exc).__name__}")
            failure = {
                "type": "error",
                "code": WorkflowCopilotModelCallError.Code,
                "message": WorkflowCopilotModelCallError.Msg,
            }
            yield json.dumps(failure, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


def _code_node_enabled() -> bool:
    try:
        return bool(bisheng_settings.get_workflow_conf().code_node_enabled)
    except Exception:  # unreadable config: the platform treats the switch as off
        return False


@router.get("/write/auth")
async def check_app_write_auth(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    flow_id: str = Query(..., description="ApplicationsID"),
    flow_type: int = Query(..., description="Apply type"),
):
    """Check if the user has administrative rights to the app"""
    if flow_type == FlowType.ASSISTANT.value:
        flow_info = await AssistantDao.aget_one_assistant(flow_id)
    elif flow_type == FlowType.WORKFLOW.value:
        flow_info = await FlowDao.aget_flow_by_id(flow_id)
        if flow_info and flow_info.flow_type != FlowType.WORKFLOW.value:
            flow_info = None
    else:
        raise NotFoundError.http_exception()
    if not flow_info:
        raise NotFoundError.http_exception()
    if await check_business_action(
        login_user,
        resource_type=("assistant" if flow_type == FlowType.ASSISTANT.value else "workflow"),
        resource_id=flow_id,
        action="edit",
    ):
        return resp_200()
    return AppWriteAuthError.return_resp()


@router.get("/report/file")
async def get_report_file(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    version_key: str = Query("", description="minioright of privacyobject_name"),
    workflow_id: str = Query(..., description="The WorkflowID"),
):
    """DapatkanreportTemplate file for the node"""

    # Check if the user has read access to the app
    flow_info = await FlowDao.aget_flow_by_id(workflow_id)
    if not flow_info:
        raise NotFoundError.http_exception()
    if not await check_business_action(
        login_user,
        resource_type="workflow",
        resource_id=workflow_id,
        action="visible",
    ):
        return UnAuthorizedError.return_resp()

    # Saving is decided here, not in the callback: the document server posts the
    # callback itself and carries no user identity. A viewer may open the
    # template, but the save is refused.
    can_edit = await check_business_action(
        login_user,
        resource_type="workflow",
        resource_id=workflow_id,
        action="edit",
    )

    minio_client = await get_minio_storage()
    version_key = report_template.storage_key(version_key)
    owner_id = report_template.owner_workflow_id(version_key)
    if not version_key:
        version_key = report_template.mint_version_key(workflow_id)
    elif owner_id and owner_id != workflow_id:
        # The key names another workflow's template. Reading it here would let
        # anyone who once saw that workflow keep its template forever.
        logger.warning(
            "report template refused: key={!r} belongs to workflow {!r}, not {!r}",
            version_key,
            owner_id,
            workflow_id,
        )
        return UnAuthorizedError.return_resp()
    elif owner_id is None and can_edit:
        # Minted before templates had an owner. Adopt it into this workflow so
        # it stops being readable from any workflow; the frontend stores the
        # returned key back onto the node.
        version_key = await _adopt_unowned_template(minio_client, version_key, workflow_id)

    if can_edit:
        # Only an editor leaves a ticket. A viewer writing one would overwrite
        # the ticket of whoever is editing the same template right now, and
        # their save would then be refused; with no ticket at all the viewer's
        # own save is refused anyway, which is the whole point.
        await report_template.aremember_edit_session(
            version_key=version_key,
            workflow_id=workflow_id,
            can_edit=True,
        )

    file_url = ""
    object_name = f"workflow/report/{version_key}.docx"
    if await minio_client.object_exists(minio_client.bucket, object_name):
        file_url = await minio_client.get_share_link(object_name, clear_host=False)

    return resp_200(
        data={
            "url": file_url,
            "version_key": f"{version_key}_{int(time.time() * 1000)}",
        }
    )


async def _adopt_unowned_template(minio_client, version_key: str, workflow_id: str) -> str:
    """Re-home a pre-ownership template under a key naming its workflow.

    The adopted name is derived from the pair, not drawn at random, and the
    document is copied only when the adopted object is missing. The node keeps
    pointing at the old key until the workflow itself is saved, so the same
    template gets adopted again on every open until then -- a random name would
    strand each round of edits under a key nothing references, and re-copying
    over an existing one would roll the template back to its pre-adoption
    content.
    """
    adopted_key = report_template.adopted_version_key(workflow_id, version_key)
    adopted_object = f"workflow/report/{adopted_key}.docx"
    object_name = f"workflow/report/{version_key}.docx"
    if not await minio_client.object_exists(minio_client.bucket, adopted_object) and await minio_client.object_exists(
        minio_client.bucket, object_name
    ):
        await minio_client.copy_object(
            source_object=object_name,
            dest_object=adopted_object,
            source_bucket=minio_client.bucket,
            dest_bucket=minio_client.bucket,
        )
    logger.info("report template adopted: {!r} -> {!r} for workflow {!r}", version_key, adopted_key, workflow_id)
    return adopted_key


async def _may_read_template_source(login_user: UserPayload, version_key: str) -> bool:
    """Whether this user may take a copy of the template behind ``version_key``."""
    owner_id = report_template.owner_workflow_id(version_key)
    if owner_id is None:
        # Minted before templates had an owner; there is nothing to check
        # against. Adoption converts these as they are opened.
        return True
    if await check_business_action(
        login_user,
        resource_type="workflow",
        resource_id=owner_id,
        action="visible",
    ):
        return True
    # Starting an app from a published template copies a node authored in a
    # workflow the user was never meant to see.
    return await report_template.ais_app_template_asset(version_key)


@router.post("/report/copy", status_code=200)
async def copy_report_file(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    version_key: str = Body(..., embed=True, description="minioright of privacyobject_name"),
):
    """SalinreportTemplate file for the node"""
    version_key = report_template.storage_key(version_key)
    if not await _may_read_template_source(login_user, version_key):
        logger.warning("report template copy refused: key={!r}", version_key)
        return UnAuthorizedError.return_resp()
    # The copy is not owned yet: the caller is duplicating a node, importing a
    # flow or starting from a template, and the workflow it lands in may not
    # exist. It is adopted the first time it is opened there.
    new_version_key = generate_uuid()
    object_name = f"workflow/report/{version_key}.docx"
    new_object_name = f"workflow/report/{new_version_key}.docx"
    minio_client = await get_minio_storage()
    if await minio_client.object_exists(minio_client.bucket, object_name):
        await minio_client.copy_object(
            source_object=object_name,
            dest_object=new_object_name,
            source_bucket=minio_client.bucket,
            dest_bucket=minio_client.bucket,
        )
    return resp_200(
        data={
            "version_key": f"{new_version_key}",
        }
    )


@router.post("/report/save", status_code=200)
async def force_save_report_file(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    workflow_id: str = Body(..., embed=True, description="The WorkflowID"),
    version_key: str = Body(..., embed=True, description="minioright of privacyobject_name"),
):
    """Ask the document server to flush the open template to storage right now.

    Manual save exists because auto-save can silently fail and lose template
    edits. We only *trigger* the save here: the document server answers by
    calling /report/callback, which stays the single path that writes MinIO.
    See features/v3.0.0-beta1/043-report-node-optimization/design.md §3.
    """
    flow_info = await FlowDao.aget_flow_by_id(workflow_id)
    if not flow_info:
        raise NotFoundError.http_exception()
    if not await check_business_action(
        login_user,
        resource_type="workflow",
        resource_id=workflow_id,
        action="edit",
    ):
        return AppWriteAuthError.return_resp()

    owner_id = report_template.owner_workflow_id(version_key)
    if owner_id != workflow_id:
        # Otherwise a manual save would mint an edit session for someone else's
        # template under a workflow the caller happens to own. An unowned key is
        # refused too: opening the editor adopts it first, so a legitimate save
        # always arrives under this workflow's own key.
        logger.warning(
            "report force save refused: key={!r} belongs to workflow {!r}, not {!r}",
            report_template.storage_key(version_key),
            owner_id,
            workflow_id,
        )
        return AppWriteAuthError.return_resp()
    # Refreshes the session so an editor left open past the TTL can still save.
    await report_template.aremember_edit_session(
        version_key=version_key,
        workflow_id=workflow_id,
        can_edit=True,
    )

    # The editor is opened with a per-session key (`<version_key>_<ts>`); the
    # command service needs that exact key, so keep whatever the client sent.
    # `office_url` is served to the browser out of the `env` config block (see
    # GET /env), so read it there; the top-level key is only a legacy override.
    office_url = await bisheng_settings.aget_from_db("office_url")
    if not office_url:
        office_url = (await bisheng_settings.aget_from_db("env") or {}).get("office_url")
    if not office_url:
        return ServerError.return_resp(msg="Document service is not configured")

    payload = {"c": "forcesave", "key": version_key}
    secret = await bisheng_settings.aget_from_db("office_jwt_secret") or ""
    if secret:
        # Signed payloads must also carry the token in-body for the command
        # service (header-only auth is rejected when JWT is enabled).
        payload["token"] = jwt.encode(payload, secret, algorithm="HS256")

    command_url = f"{office_url.rstrip('/')}/coauthoring/CommandService.ashx"
    try:
        response = Requests().post(url=command_url, json=payload)
        result = response.json()
    except Exception as e:
        logger.error(f"report force save request failed key={version_key} err={e}")
        return ServerError.return_resp(msg="Cannot reach the document service")

    # error==0 means the save command was accepted; the callback does the rest.
    error_code = result.get("error")
    if error_code not in (0, FORCE_SAVE_NO_CHANGES):
        logger.warning(f"report force save rejected key={version_key} result={result}")
        return ServerError.return_resp(
            msg=FORCE_SAVE_ERROR_MESSAGES.get(error_code, "The document service refused the save request")
        )

    logger.info(f"report force save accepted key={version_key}")
    return resp_200(data={"saved": True})


@router.post("/report/callback", status_code=200)
async def upload_report_file(request: Request, data: dict = Body(...)):
    """office Callback interface save reportTemplate file for the node"""
    status = data.get("status")
    file_url = data.get("url")
    key = data.get("key")
    logger.debug(f"callback={data}")
    if status not in {2, 6}:
        # Non-saved callbacks are not processed
        return {"error": 0}
    logger.info(f"office_callback url={file_url}")
    # The callback carries no user identity, so it cannot be authorized on its
    # own. It is accepted only for a template the backend handed to an editor,
    # and only when that editor was opened by someone with edit rights.
    session = await report_template.aget_edit_session(key)
    if not session:
        logger.warning("office callback refused: no editor session for key={!r}", report_template.storage_key(key))
        return {"error": 1}
    if not session.get("can_edit"):
        logger.warning(
            "office callback refused: key={!r} was opened without edit rights on workflow {!r}",
            report_template.storage_key(key),
            session.get("workflow_id"),
        )
        return {"error": 1}
    content = await afetch_office_document(file_url)
    if content is None:
        # Non-zero tells the document server the save failed; nothing is stored.
        return {"error": 1}
    version_key = report_template.storage_key(key)

    minio_client = await get_minio_storage()
    object_name = f"workflow/report/{version_key}.docx"
    await minio_client.put_object(
        object_name=object_name,
        file=content,
        bucket_name=minio_client.bucket,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    return {"error": 0}


@router.post("/run_once", status_code=200)
async def run_once(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    node_input: dict | None = None,  # Input parameters of the node
    node_data: dict | None = None,
    workflow_id: str = Body(..., description="The WorkflowID"),
):
    """Single node operation"""
    result = await WorkFlowService.run_once(
        login_user,
        node_input,
        node_data,
        workflow_id,
    )

    return resp_200(data=result)


@router.websocket("/chat/{workflow_id}")
async def workflow_ws(
    *,
    workflow_id: str,
    websocket: WebSocket,
    chat_id: str | None = None,
    flow_lease_id: str | None = None,
    login_user: UserPayload = Depends(UserPayload.get_login_user_from_ws),
):
    try:
        await require_business_action(
            login_user,
            resource_type="workflow",
            resource_id=workflow_id,
            action="use",
        )
        # ``flow_lease_id`` is the page lease opened by the session page; without
        # it the connection takes its own execution lease.
        await chat_manager.dispatch_client(
            websocket,
            workflow_id,
            chat_id,
            login_user,
            WorkType.WORKFLOW,
            websocket,
            flow_lease_id=flow_lease_id,
        )
    except UnAuthorizedError:
        await websocket.close(
            code=http_status.WS_1008_POLICY_VIOLATION,
            reason="No permission to use this app",
        )
    except WebSocketException as exc:
        logger.error(f"Websocket exception: {exc!s}")
        await websocket.close(code=http_status.WS_1011_INTERNAL_ERROR, reason=str(exc))


@router.post("/create", status_code=201)
@require_quota(QuotaResourceType.WORKFLOW)
async def create_flow(
    *,
    request: Request,
    flow: FlowCreate,
    login_user: UserPayload = Depends(UserPayload.get_app_creator_user),
):
    """Create a new flow.

    The body lives in ``FlowService.create_workflow`` — v2 ``POST /api/v2/flows``
    (F005) delegates to the same method, so the duplicate-name check and the
    owner/tenant stamping cannot drift between the two write faces. What this
    endpoint still owns is **who** may call it: the create-app gate and quota.
    """
    return await FlowService.create_workflow(request, login_user, flow)


@router.get("/versions", status_code=200)
async def get_versions(
    *,
    flow_id: str,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """
    Get a list of versions for your skill
    """
    return await FlowService.get_version_list_by_flow(login_user, flow_id)


@router.post("/versions", status_code=200)
async def create_versions(
    *, flow_id: str, flow_version: FlowVersionCreate, login_user: UserPayload = Depends(UserPayload.get_login_user)
):
    """
    Create New Skill Version
    """
    flow_version.flow_type = FlowType.WORKFLOW.value
    return await FlowService.create_new_version(login_user, flow_id, flow_version)


@router.put("/versions/{version_id}", status_code=200)
async def update_versions(
    *,
    request: Request,
    version_id: int,
    flow_version: FlowVersionCreate,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """
    Update to version
    """
    return await FlowService.update_version_info(request, login_user, version_id, flow_version)


@router.delete("/versions/{version_id}", status_code=200)
async def delete_versions(
    *,
    version_id: int,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """
    Remove Version
    """
    return await FlowService.delete_version(login_user, version_id)


@router.get("/versions/{version_id}", status_code=200)
async def get_version_info(
    *,
    version_id: int,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """
    Get Version Info
    """
    return await FlowService.get_version_info(login_user, version_id)


@router.post("/change_version", status_code=200)
async def change_version(
    *,
    request: Request,
    flow_id: str = Query(default=None, description="Skill UniqueID"),
    version_id: int = Query(default=None, description="Current version that needs to be setID"),
    login_user: UserPayload = Depends(UserPayload.get_login_user),
):
    """
    Modify Current Version
    """
    return await FlowService.change_current_version(request, login_user, flow_id, version_id)


@router.get("/get_one_flow/{flow_id}")
async def read_flow(
    *,
    flow_id: str,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    share_link: Union["ShareLink", None] = Depends(header_share_token_parser),
):
    """Read a flow."""
    return await FlowService.get_one_flow(login_user, flow_id, share_link)


@router.patch("/update/{flow_id}")
async def update_flow(
    *, request: Request, flow_id: str, flow: FlowUpdate, login_user: UserPayload = Depends(UserPayload.get_login_user)
):
    """online offline"""
    db_flow = await FlowDao.aget_flow_by_id(flow_id)
    if not db_flow:
        raise NotFoundError()

    if not await check_business_action(
        login_user,
        resource_type="workflow",
        resource_id=flow_id,
        action="edit",
    ):
        return UnAuthorizedError.return_resp()

    flow_data = flow.model_dump(exclude_unset=True)

    if db_flow.status == FlowStatus.ONLINE.value and (
        "status" not in flow_data or flow_data["status"] != FlowStatus.OFFLINE.value
    ):
        raise WorkFlowOnlineEditError.http_exception()

    for key, value in flow_data.items():
        if key in ["data", "create_time", "update_time"]:
            continue
        if key == "logo" and not value:
            continue
        setattr(db_flow, key, value)
    db_flow = await FlowDao.aupdate_flow(db_flow)
    await telemetry_service.log_event(
        user_id=login_user.user_id, event_type=BaseTelemetryTypeEnum.EDIT_APPLICATION, trace_id=trace_id_var.get()
    )
    await FlowService.update_flow_hook(request, login_user, db_flow)
    return resp_200(db_flow)


@router.patch("/status")
async def update_flow_status(
    request: Request,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    flow_id: str = Body(..., description="SkillID"),
    version_id: int = Body(..., description="VersionID"),
    status: int = Body(..., description="Status"),
):
    await WorkFlowService.update_flow_status(login_user, flow_id, version_id, status)
    return resp_200()


@router.get("/list", status_code=200)
async def read_flows(
    *,
    login_user: UserPayload = Depends(UserPayload.get_login_user),
    name: str = Query(default=None, description="accordingnameFind databases with fuzzy searches for descriptions"),
    tag_id: int = Query(default=None, description="labelID"),
    flow_type: int = Query(default=None, description="Type 5 assistant 10 workflow 35 hosted app"),
    page_size: int = Query(default=10, description="Items per page"),
    status: int | None = None,
    app_state: str | None = Query(
        default=None,
        description="F054 hosted-application state (draft / online / pending_capacity / stopped / deleted). "
        "Separate from `status`, which stays the 1/2 on-off switch shared by all three types. "
        "An unknown value simply matches nothing.",
    ),
    managed: bool = Query(
        default=False, description="Whether to query the list of apps with administrative permissions"
    ),
    action: str = Query(
        default="use",
        pattern="^(visible|use)$",
        description="Concrete action required for non-managed app lists",
    ),
    cursor: str | None = Query(
        default=None,
        description="F027 cursor-based pagination token from the previous response's "
        "`next_cursor`. Omit (or pass empty) to fetch the first page.",
    ),
):
    """Read all flows (F027 cursor-based pagination).

    Response shape (PageInfiniteCursorData): ``{data, page_size, has_more, next_cursor}``.
    The legacy ``total`` / ``page_num`` fields have been removed (AC-02).
    """
    result = await WorkFlowService.get_all_flows_envelope(
        login_user,
        name,
        status,
        tag_id,
        flow_type,
        cursor=cursor,
        page_size=page_size,
        managed=managed,
        action=action,
        app_state=app_state,
    )
    return resp_200(data=result)
