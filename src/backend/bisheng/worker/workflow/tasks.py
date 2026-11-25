import time

from loguru import logger

from bisheng.api.services.workflow import WorkFlowService
from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum, BaseTelemetryTypeEnum
from bisheng.common.dependencies.user_deps import UserPayload
from bisheng.common.errcode.base import BaseErrorCode, encode_workflow_error
from bisheng.common.schemas.telemetry.event_data_schema import ApplicationProcessEventData
from bisheng.common.services import telemetry_service
from bisheng.common.services.config_service import settings
from bisheng.core.context.tenant import get_current_tenant_id
from bisheng.core.logger import trace_id_var
from bisheng.database.models.flow import FlowDao
from bisheng.dsh.domain.services import chat_credits
from bisheng.memory.domain.ports import SCENE_WORKFLOW
from bisheng.memory.domain.services.activity import MemoryActivity
from bisheng.memory.domain.services.app_switch import workflow_memory_enabled_sync
from bisheng.memory.domain.services.gate import MemoryGate
from bisheng.open_api.domain.services.execution_context import restore_execution_context
from bisheng.permission.application.business_authorization import require_business_action
from bisheng.permission.application.identity import get_current_permission_actor
from bisheng.public_endpoints.domain.services.guest_policy import should_skip_public_workflow_use
from bisheng.utils.exceptions import IgnoreException
from bisheng.worker._asyncio_utils import run_async_task
from bisheng.worker.main import bisheng_celery
from bisheng.worker.tenant_context import PermissionTaskTenantContextError, durable_workload_scope
from bisheng.worker.utils.stateful_worker import StatefulWorker
from bisheng.worker.workflow.redis_callback import RedisCallback
from redis.exceptions import RedisError

from bisheng.core.config.settings import DEFAULT_PAUSE_RETENTION_SECONDS
from bisheng.worker.workflow import redis_callback as _callback_module
from bisheng.workflow.common.workflow import WorkflowStatus
from bisheng.workflow.graph.workflow import Workflow
from bisheng.workflow.domain.services.pause_state import (
    EXPIRED_NOTICE_GRACE_SECONDS,
    RESUME_LOCK_SECONDS,
    PauseStateNotFound,
    PauseStateStore,
)
from bisheng.workflow.graph.run_state import RunStateError
from bisheng.common.errcode.flow import WorkFlowPauseStateInvalidError

# workflow execute stateful worker for assigning tasks to bound workers
workflow_stateful_worker = StatefulWorker(queue_prefix="workflow_celery", bound_nodes_prefix="workflow_bound_nodes:")
# Stores global workflow objects
_global_workflow: dict[str, Workflow] = {}
# Which saved pause each in-memory object belongs to (see _warm_copy_is_current)
_global_workflow_pause_stamp: dict[str, float] = {}
# When this Worker put each warm copy in memory (local clock); used to release copies nobody came back for
_global_workflow_parked_at: dict[str, float] = {}


def _redis_client():
    # looked up at call time so tests (and the callback module) can swap the client
    return _callback_module.get_redis_client_sync()


def _pause_store() -> PauseStateStore:
    """The store for saving a pause; needs the retention period from the workflow settings."""
    conf = settings.get_workflow_conf()
    return PauseStateStore(_redis_client(), getattr(conf, "pause_retention_seconds", DEFAULT_PAUSE_RETENTION_SECONDS))


def _light_pause_store() -> PauseStateStore:
    """A store for operations that never read the settings (delete, stamps, expiry checks)."""
    return PauseStateStore(_redis_client(), DEFAULT_PAUSE_RETENTION_SECONDS)


def _continue_store(workflow_conf) -> PauseStateStore:
    """The store a continue task uses; the resume token lives as long as one run may last."""
    run_limit = int(getattr(workflow_conf, "timeout", 0) or 0) * 60 + 60
    return PauseStateStore(
        _redis_client(),
        getattr(workflow_conf, "pause_retention_seconds", DEFAULT_PAUSE_RETENTION_SECONDS),
        lock_seconds=max(RESUME_LOCK_SECONDS, run_limit),
    )


def _drop_pause_state(unique_id: str):
    """The run ended or was stopped: its saved pause state is no longer needed (best effort)."""
    try:
        _light_pause_store().delete(unique_id)
    except Exception:
        logger.warning("workflow pause state could not be removed unique_id={}", unique_id)


def _clear_workflow_obj(unique_id: str):
    """Clear Global Workflow Objects"""
    _drop_pause_state(unique_id)
    _global_workflow_pause_stamp.pop(unique_id, None)
    _global_workflow_parked_at.pop(unique_id, None)
    if unique_id in _global_workflow:
        del _global_workflow[unique_id]
        logger.debug(f"clear workflow object for unique_id: {unique_id}")
    else:
        logger.warning(f"workflow object not found for unique_id: {unique_id}")


def _extend_session_record(redis_callback: RedisCallback):
    """Keep the session's status record a little longer than the saved state, so a late
    request hears "expired" instead of silently starting a new run."""
    conf = settings.get_workflow_conf()
    ttl = getattr(conf, "pause_retention_seconds", DEFAULT_PAUSE_RETENTION_SECONDS) + EXPIRED_NOTICE_GRACE_SECONDS
    if ttl > 3600 * 24 * 7:  # set_workflow_status already keeps the record 7 days
        redis_callback.redis_client.expire_key(redis_callback.workflow_status_key, ttl)


def _release_abandoned_warm_copies(retention_seconds: int, now: float | None = None) -> int:
    """Drop in-memory copies of paused sessions nobody came back for within the retention period.

    The saved state in Redis expires on its own; this only frees the Worker's memory. It runs whenever
    a run pauses, so no extra timer is needed. Returns how many copies were released.
    """
    now = time.time() if now is None else now
    stale = [uid for uid, parked in _global_workflow_parked_at.items() if now - parked > retention_seconds]
    for uid in stale:
        _global_workflow.pop(uid, None)
        _global_workflow_pause_stamp.pop(uid, None)
        _global_workflow_parked_at.pop(uid, None)
    if stale:
        logger.info("released {} abandoned paused workflow copies from memory", len(stale))
    return len(stale)


def _save_pause_state(redis_callback: RedisCallback, workflow: Workflow):
    engine = workflow.graph_engine
    run = engine.export_run_state()
    run["memory_enabled"] = bool(getattr(engine.graph_state, "memory_enabled", False))
    stamp = _pause_store().save(
        redis_callback.unique_id,
        tenant_id=workflow.tenant_id,
        workflow_id=workflow.workflow_id,
        canvas=engine.workflow_data,
        run_state=run,
    )
    _global_workflow_pause_stamp[redis_callback.unique_id] = stamp


def _judge_workflow_status(redis_callback: RedisCallback, workflow: Workflow):
    status = workflow.status()
    reason = workflow.reason()
    if workflow.status() in [WorkflowStatus.SUCCESS.value, WorkflowStatus.FAILED.value]:
        redis_callback.set_workflow_status(status, reason)
        _clear_workflow_obj(redis_callback.unique_id)
        return
    if workflow.status() == WorkflowStatus.INPUT.value:
        # Keep the object in memory as a warm cache for the Worker that paused it. This comes first,
        # so a Redis hiccup below never costs the run (the behaviour before F002).
        _global_workflow[redis_callback.unique_id] = workflow
        _global_workflow_parked_at[redis_callback.unique_id] = time.time()
        try:
            conf = settings.get_workflow_conf()
            _release_abandoned_warm_copies(getattr(conf, "pause_retention_seconds", DEFAULT_PAUSE_RETENTION_SECONDS))
        except Exception:
            logger.warning("abandoned paused workflow copies could not be released")
        # F002: also save the resumable state, so any Worker can continue this session
        try:
            _save_pause_state(redis_callback, workflow)
        except RunStateError as e:
            logger.warning(f"workflow pause state cannot be saved: {e}")
            redis_callback.set_workflow_status(
                WorkflowStatus.FAILED.value, encode_workflow_error(WorkFlowPauseStateInvalidError(exception=str(e)))
            )
            _clear_workflow_obj(redis_callback.unique_id)
            return
        except Exception as e:
            # The store is unavailable: the session still works while this Worker lives.
            logger.warning(f"workflow pause state could not be stored, keeping the in-memory copy only: {e}")
        redis_callback.set_workflow_status(status, reason)
        try:
            _extend_session_record(redis_callback)
        except Exception:
            logger.warning("workflow session record lifetime could not be extended")
        return
    logger.error(f"unexpected workflow status error: {status}")
    redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, f"workflow run failed, unexpected status: {status}")
    _clear_workflow_obj(redis_callback.unique_id)


def _memory_chat_id(chat_id: str, source: str) -> str:
    """F132 AC-10: only platform (logged-in) runs may use memory; open-API runs never do,
    even when they reuse a chat_id that a logged-in connection registered."""
    return chat_id if source == "platform" else ""


def _online_memory_switch(workflow_id: str, chat_id: str) -> bool:
    """F132 R5: the workflow memory switch of the ONLINE version in the database.

    Read once per run at execution start, never from the graph the client sent.
    Runs without a session (evaluation, canvas debug, open API) skip the lookup.
    A lookup failure counts as off and never fails the run.
    """
    if not chat_id:
        return False
    try:
        return workflow_memory_enabled_sync(workflow_id)
    except Exception:
        logger.exception("memory switch lookup failed workflow_id={}", workflow_id)
        return False


def _end_memory_turn(chat_id: str, memory_enabled: bool | None, user_id: int | None = None) -> None:
    """F132: the task yields (waiting for input, success, failure, error) -> one turn ends.

    Enabled turns mark the session for consolidation; turns with memory off move
    the watermark past them. Unregistered sessions are a no-op. Best effort.
    """
    if not chat_id or memory_enabled is None:
        # memory_enabled None: the run object was lost, the switch is unknown; leave
        # the session as it is so earlier enabled turns are still consolidated.
        return
    try:
        ctx = MemoryGate.resolve_sync(chat_id, SCENE_WORKFLOW, app_enabled=memory_enabled, actor_user_id=user_id)
        MemoryActivity.end_turn_sync(chat_id, None, ctx is not None)
    except Exception:
        logger.exception("memory end_turn failed chat_id={}", chat_id)


def _execute_workflow(unique_id: str, workflow_id: str, chat_id: str, user_id: int, source: str = "platform"):
    # F022 INV-T18: resolve Flow.tenant_id once; reuse for both Workflow
    # node tenant threading and RedisCallback's sync workbench reads.
    workflow_info = FlowDao.get_flow_by_id(workflow_id)
    flow_tenant_id = getattr(workflow_info, "tenant_id", None)
    from bisheng.core.context.tenant import get_current_tenant_id
    from bisheng.worker.tenant_context import PermissionTaskTenantContextError

    if flow_tenant_id is None or flow_tenant_id != get_current_tenant_id():
        raise PermissionTaskTenantContextError("workflow tenant disagrees with persistent target")
    # F041: Flow creator (config author) — nodes filter knowledge-space retrieval
    # by this identity when the permission toggle is OFF (distinct from `user_id`,
    # the runtime user who triggered the run).
    flow_user_id = getattr(workflow_info, "user_id", None)
    redis_callback = RedisCallback(unique_id, workflow_id, chat_id, user_id, source=source, tenant_id=flow_tenant_id)
    memory_chat_id = _memory_chat_id(chat_id, source)
    memory_enabled = _online_memory_switch(workflow_id, memory_chat_id)
    try:
        # update workflow status
        redis_callback.set_workflow_status(WorkflowStatus.RUNNING.value)
        # get workflow data
        workflow_data = redis_callback.get_workflow_data()
        if not workflow_data:
            raise Exception("workflow data not found maybe data is expired")

        # init workflow
        workflow_conf = settings.get_workflow_conf()
        workflow_name = workflow_info.name if workflow_info else workflow_id
        with chat_credits.billing_context(source_type="workflow", source_id=str(workflow_id)):
            workflow = Workflow(
                workflow_id,
                workflow_name,
                user_id,
                workflow_data,
                False,
                workflow_conf.max_steps,
                workflow_conf.timeout,
                redis_callback,
                tenant_id=flow_tenant_id,
                flow_user_id=flow_user_id,
                chat_id=chat_id or "",
                memory_enabled=memory_enabled,
            )
            redis_callback.workflow = workflow
            _status, _reason = workflow.run()
        _judge_workflow_status(redis_callback, workflow)
    except IgnoreException as e:
        logger.warning(f"execute_workflow ignore error: {e}")
        redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, str(e))
        _clear_workflow_obj(redis_callback.unique_id)
    except BaseErrorCode as e:
        logger.warning(f"execute_workflow error code {e.code}: {e.message}")
        redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, encode_workflow_error(e))
        _clear_workflow_obj(redis_callback.unique_id)
    except Exception as e:
        logger.exception("execute_workflow error")
        redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, str(e)[:100])
        _clear_workflow_obj(redis_callback.unique_id)
    finally:
        _end_memory_turn(memory_chat_id, memory_enabled, user_id)


def _authorize_workflow_use(snapshot, user_id: int, workflow_id: str, chat_id: str) -> None:
    if snapshot is None:
        return
    if should_skip_public_workflow_use(
        channel=getattr(snapshot, "channel", None),
        chat_id=chat_id,
        workflow_id=workflow_id,
    ):
        return
    run_async_task(
        lambda: require_business_action(
            UserPayload(user_id=user_id, user_role=[], tenant_id=snapshot.tenant_id),
            resource_type="workflow",
            resource_id=workflow_id,
            action="visible",
            actor=get_current_permission_actor(),
        )
    )


def _log_workflow_telemetry(workflow_id: str, chat_id: str, user_id: int, start_time: float):
    """Record best-effort workflow telemetry without changing task outcome."""
    try:
        end_time = time.time()
        workflow_info = WorkFlowService.get_one_workflow_simple_info_sync(workflow_id)
        telemetry_service.log_event_sync(
            user_id=user_id,
            event_type=BaseTelemetryTypeEnum.APPLICATION_PROCESS,
            trace_id=trace_id_var.get(),
            event_data=ApplicationProcessEventData(
                app_id=workflow_id,
                app_name=workflow_info.name if workflow_info else workflow_id,
                app_type=ApplicationTypeEnum.WORKFLOW,
                chat_id=chat_id,
                start_time=int(start_time),
                end_time=int(end_time),
                process_time=int((end_time - start_time) * 1000),
            ),
        )
    except Exception:
        # Telemetry is opportunistic and must not reverse the workflow task outcome.
        logger.exception("failed to record workflow telemetry for workflow_id={}", workflow_id)


@bisheng_celery.task
def execute_workflow(
    unique_id: str,
    workflow_id: str,
    chat_id: str,
    user_id: int,
    source: str = "platform",
    execution_snapshot: dict | None = None,
):
    """Implementationworkflow"""
    trace_id_var.set(unique_id)
    start_time = time.time()
    try:
        with durable_workload_scope("workflow", workflow_id) as expected_tenant:
            with restore_execution_context(execution_snapshot) as snapshot:
                if get_current_tenant_id() != expected_tenant:
                    raise PermissionTaskTenantContextError("execution snapshot disagrees with workflow target")
                # Guest runs skip `use` only when session.flow_id matches this workflow.
                _authorize_workflow_use(snapshot, user_id, workflow_id, chat_id)
                _execute_workflow(unique_id, workflow_id, chat_id, user_id, source)
    finally:
        _log_workflow_telemetry(workflow_id, chat_id, user_id, start_time)


def _warm_copy_is_current(store: PauseStateStore, unique_id: str) -> bool:
    """A Worker's in-memory copy is only valid while it belongs to the newest saved pause.

    If the session was continued on another Worker in between (the routing can change when
    Workers come and go), the newest saved pause carries a different stamp.
    """
    mine = _global_workflow_pause_stamp.get(unique_id)
    newest = store.paused_at(unique_id)
    return mine is None or newest is None or mine == newest


def _restore_paused_workflow(redis_callback: RedisCallback, workflow_id: str, chat_id: str, user_id: int, store, conf):
    """F002: rebuild a paused run from its saved state when this Worker does not hold it in memory.

    The caller already holds the resume token, so the snapshot read here is the newest one.
    Sessions that never saved state keep the old failure; expired or unusable state raises
    the pause-state error codes.
    """
    unique_id = redis_callback.unique_id
    try:
        snapshot = store.load(unique_id, tenant_id=get_current_tenant_id(), workflow_id=workflow_id)
    except PauseStateNotFound:
        raise Exception("workflow object not found maybe data is expired") from None
    workflow_info = FlowDao.get_flow_by_id(workflow_id)
    flow_tenant_id = getattr(workflow_info, "tenant_id", None)
    if flow_tenant_id is None or flow_tenant_id != get_current_tenant_id():
        raise PermissionTaskTenantContextError("workflow tenant disagrees with persistent target")
    try:
        with chat_credits.billing_context(source_type="workflow", source_id=str(workflow_id)):
            workflow = Workflow(
                workflow_id,
                workflow_info.name if workflow_info else workflow_id,
                user_id,
                snapshot.canvas,
                False,
                conf.max_steps,
                conf.timeout,
                redis_callback,
                tenant_id=flow_tenant_id,
                flow_user_id=getattr(workflow_info, "user_id", None),
                chat_id=chat_id or "",
                memory_enabled=bool(snapshot.run_state.get("memory_enabled")),
            )
        redis_callback.workflow = workflow
        workflow.graph_engine.import_run_state(snapshot.run_state)
    except RunStateError as e:
        raise WorkFlowPauseStateInvalidError(exception=str(e)) from e
    logger.info(f"workflow restored from pause state unique_id={unique_id}")
    return workflow


def _store_unavailable(error: BaseException) -> bool:
    return isinstance(error, (RedisError, ConnectionError, TimeoutError))


def _session_was_stopped(redis_callback: RedisCallback) -> bool:
    info = redis_callback.get_workflow_status()
    return bool(info) and info.get("status") == WorkflowStatus.FAILED.value


def _continue_workflow(unique_id: str, workflow_id: str, chat_id: str, user_id: int, source: str = "platform"):
    """Resumeworkflow"""
    redis_callback = RedisCallback(unique_id, workflow_id, chat_id, user_id, source=source)
    memory_chat_id = _memory_chat_id(chat_id, source)
    memory_enabled = None
    store = None
    resume_owner = None
    run_started = False
    try:
        conf = settings.get_workflow_conf()
        store = _continue_store(conf)
        # Exactly one task continues a session, on every path (warm copy or rebuild).
        resume_owner = store.acquire_resume(unique_id)
        if resume_owner is None:
            logger.warning(f"workflow continue skipped, another task owns session unique_id={unique_id}")
            return
        if _session_was_stopped(redis_callback):
            logger.info(f"workflow continue skipped, session was stopped unique_id={unique_id}")
            return
        workflow = _global_workflow.get(redis_callback.unique_id, None)
        if workflow:
            # the warm copy must not outlive the retention period either
            store.ensure_not_expired(unique_id)
            if not _warm_copy_is_current(store, unique_id):
                # another Worker continued this session since; this copy is stale, rebuild from Redis
                _global_workflow.pop(unique_id, None)
                _global_workflow_pause_stamp.pop(unique_id, None)
                _global_workflow_parked_at.pop(unique_id, None)
                workflow = None
        if not workflow:
            workflow = _restore_paused_workflow(redis_callback, workflow_id, chat_id, user_id, store, conf)
        if workflow.tenant_id != get_current_tenant_id() or workflow.workflow_id != workflow_id:
            raise PermissionTaskTenantContextError("cached workflow target mismatch")
        # a resumed run keeps the switch read at its start (R5)
        memory_enabled = bool(getattr(workflow.graph_engine.graph_state, "memory_enabled", False))
        if workflow.status() not in [WorkflowStatus.INPUT.value, WorkflowStatus.INPUT_OVER.value]:
            raise Exception(f"workflow status is {workflow.status()} not INPUT")
        user_input = redis_callback.get_user_input()
        if not user_input:
            raise IgnoreException("workflow continue not found user input")
        redis_callback.set_workflow_status(WorkflowStatus.RUNNING.value)
        run_started = True
        with chat_credits.billing_context(source_type="workflow", source_id=str(workflow_id)):
            _status, _reason = workflow.run(user_input)
        _judge_workflow_status(redis_callback, workflow)
    except IgnoreException as e:
        logger.warning(f"continue_workflow ignore error: {e}")
        redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, str(e))
        _clear_workflow_obj(redis_callback.unique_id)
    except BaseErrorCode as e:
        logger.warning(f"continue_workflow error code {e.code}: {e.message}")
        redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, encode_workflow_error(e))
        _clear_workflow_obj(redis_callback.unique_id)
    except Exception as e:
        if not run_started and _store_unavailable(e):
            # Nothing ran yet and the saved state is untouched: do not destroy it. Put the session back
            # to waiting (best effort) so the user can send the input again once Redis is back.
            logger.warning(f"continue_workflow could not reach the state store, session kept: {type(e).__name__}")
            try:
                redis_callback.set_workflow_status(WorkflowStatus.INPUT.value, "state store unavailable, retry later")
            except Exception:
                logger.warning("continue_workflow could not put the session back to waiting")
        else:
            logger.exception("continue_workflow error")
            redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, str(e)[:100])
            _clear_workflow_obj(redis_callback.unique_id)
    finally:
        if resume_owner:
            try:
                store.release_resume(unique_id, resume_owner)
            except Exception:
                logger.warning(f"workflow resume token could not be released unique_id={unique_id}")
        _end_memory_turn(memory_chat_id, memory_enabled, user_id)


@bisheng_celery.task
def continue_workflow(
    unique_id: str,
    workflow_id: str,
    chat_id: str,
    user_id: int,
    source: str = "platform",
    execution_snapshot: dict | None = None,
):
    """Resumeworkflow"""
    trace_id_var.set(unique_id)
    start_time = time.time()
    try:
        with durable_workload_scope("workflow", workflow_id) as expected_tenant:
            with restore_execution_context(execution_snapshot) as snapshot:
                if get_current_tenant_id() != expected_tenant:
                    raise PermissionTaskTenantContextError("execution snapshot disagrees with workflow target")
                _authorize_workflow_use(snapshot, user_id, workflow_id, chat_id)
                _continue_workflow(unique_id, workflow_id, chat_id, user_id, source)
    finally:
        _log_workflow_telemetry(workflow_id, chat_id, user_id, start_time)


@bisheng_celery.task
def stop_workflow(unique_id: str, workflow_id: str, chat_id: str, user_id: int):
    """Stopworkflow"""
    trace_id_var.set(unique_id)

    with durable_workload_scope("workflow", workflow_id):
        redis_callback = RedisCallback(unique_id, workflow_id, chat_id, user_id)
        if unique_id not in _global_workflow:
            redis_callback.set_workflow_status(WorkflowStatus.FAILED.value, "workflow stop by user")
            # F002: a paused session may be held only in Redis; ending it must not need the old Worker
            _drop_pause_state(unique_id)
            logger.warning("stop_workflow called but workflow not found in global cache")
            return
        workflow = _global_workflow[unique_id]
        if workflow.tenant_id != get_current_tenant_id() or workflow.workflow_id != workflow_id:
            raise PermissionTaskTenantContextError("cached workflow target mismatch")
        workflow.stop()

        while workflow.status() == WorkflowStatus.RUNNING.value:
            time.sleep(0.3)
        status, reason = workflow.status(), workflow.reason()
        if status != WorkflowStatus.FAILED.value:
            status = WorkflowStatus.FAILED.value
            reason = "workflow stop by user"
            redis_callback.set_workflow_status(status, reason)
        _clear_workflow_obj(unique_id)
        logger.info(f"workflow stop by user {user_id}")
