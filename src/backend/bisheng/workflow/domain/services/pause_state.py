"""Redis store for workflow runs that are paused waiting for user input (F002).

A paused run used to live only in the memory of the Worker that paused it. Here it is
saved as a snapshot that any Worker can load to rebuild the run:

* ``workflow:{unique_id}:pause``      the snapshot (canvas copy + run state), kept for the retention period;
* ``workflow:{unique_id}:pause_meta`` a small marker that outlives the snapshot by a grace period,
  so a late request gets "expired" instead of a vague "not found";
* ``workflow:{unique_id}:pause_lock`` the token that lets exactly one input continue the run.

Values go through ``RedisClient``, which already authenticates and encrypts what it stores.
Nothing here logs a payload: logs carry the session id, sizes and outcomes only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from bisheng.common.errcode.flow import (
    WorkFlowPauseResumeConflictError,
    WorkFlowPauseStateExpiredError,
    WorkFlowPauseStateInvalidError,
)

SNAPSHOT_FORMAT = 1
#: How long after the retention period a request is still told "expired" rather than "unknown".
EXPIRED_NOTICE_GRACE_SECONDS = 24 * 3600
#: Safety expiry of the resume token; the token is normally released as soon as the run yields.
RESUME_LOCK_SECONDS = 3600


class PauseStateNotFound(Exception):
    """No pause state was ever saved for the session (for example it paused before the upgrade)."""


@dataclass(frozen=True)
class PauseSnapshot:
    canvas: dict
    run_state: dict
    paused_at: float
    expires_at: float


def _snapshot_key(unique_id: str) -> str:
    return f"workflow:{unique_id}:pause"


def _meta_key(unique_id: str) -> str:
    return f"workflow:{unique_id}:pause_meta"


def _lock_key(unique_id: str) -> str:
    return f"workflow:{unique_id}:pause_lock"


class PauseStateStore:
    def __init__(self, redis_client, retention_seconds: int, clock: Callable[[], float] = time.time):
        self.redis = redis_client
        self.retention_seconds = int(retention_seconds)
        self.clock = clock

    @classmethod
    def default(cls) -> "PauseStateStore":
        from bisheng.core.cache.redis_manager import get_redis_client_sync
        from bisheng.common.services.config_service import settings

        return cls(get_redis_client_sync(), settings.get_workflow_conf().pause_retention_seconds)

    def save(self, unique_id: str, *, tenant_id: int, workflow_id: str, canvas: dict, run_state: dict) -> None:
        now = self.clock()
        expires_at = now + self.retention_seconds
        snapshot: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "canvas": canvas,
            "run_state": run_state,
            "paused_at": now,
            "expires_at": expires_at,
        }
        self.redis.set(_snapshot_key(unique_id), snapshot, expiration=self.retention_seconds)
        self.redis.set(
            _meta_key(unique_id),
            {"paused_at": now, "expires_at": expires_at},
            expiration=self.retention_seconds + EXPIRED_NOTICE_GRACE_SECONDS,
        )
        logger.info("workflow pause state saved unique_id={} retention_s={}", unique_id, self.retention_seconds)

    def load(self, unique_id: str, *, tenant_id: int, workflow_id: str) -> PauseSnapshot:
        meta = self.redis.get(_meta_key(unique_id))
        snapshot = self.redis.get(_snapshot_key(unique_id))
        if meta is None and snapshot is None:
            raise PauseStateNotFound(unique_id)

        expires_at = None
        if isinstance(meta, dict):
            expires_at = meta.get("expires_at")
        if expires_at is None and isinstance(snapshot, dict):
            expires_at = snapshot.get("expires_at")
        if isinstance(expires_at, (int, float)) and self.clock() > expires_at:
            self.redis.delete(_snapshot_key(unique_id))
            logger.info("workflow pause state expired unique_id={}", unique_id)
            raise WorkFlowPauseStateExpiredError()

        if snapshot is None:
            logger.warning("workflow pause state missing before expiry unique_id={}", unique_id)
            raise WorkFlowPauseStateInvalidError()
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("format") != SNAPSHOT_FORMAT
            or not isinstance(snapshot.get("canvas"), dict)
            or not isinstance(snapshot.get("run_state"), dict)
        ):
            logger.warning("workflow pause state unreadable unique_id={}", unique_id)
            raise WorkFlowPauseStateInvalidError()
        if snapshot.get("tenant_id") != tenant_id or snapshot.get("workflow_id") != workflow_id:
            logger.warning("workflow pause state target mismatch unique_id={}", unique_id)
            raise WorkFlowPauseStateInvalidError()
        return PauseSnapshot(
            canvas=snapshot["canvas"],
            run_state=snapshot["run_state"],
            paused_at=float(snapshot.get("paused_at") or 0),
            expires_at=float(snapshot.get("expires_at") or 0),
        )

    def delete(self, unique_id: str) -> None:
        for key in (_snapshot_key(unique_id), _meta_key(unique_id)):
            self.redis.delete(key)

    def acquire_resume(self, unique_id: str) -> bool:
        return bool(self.redis.setNx(_lock_key(unique_id), 1, expiration=RESUME_LOCK_SECONDS))

    def require_resume(self, unique_id: str) -> None:
        if not self.acquire_resume(unique_id):
            raise WorkFlowPauseResumeConflictError()

    def release_resume(self, unique_id: str) -> None:
        self.redis.delete(_lock_key(unique_id))
