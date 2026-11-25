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

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from bisheng.workflow.graph.run_state import RunStateError
from bisheng.common.errcode.flow import (
    WorkFlowPauseStateExpiredError,
    WorkFlowPauseStateInvalidError,
)

SNAPSHOT_FORMAT = 1
#: How long after the retention period a request is still told "expired" rather than "unknown".
EXPIRED_NOTICE_GRACE_SECONDS = 24 * 3600
#: Default safety expiry of the resume token; callers pass the workflow's own run-time limit.
#: The token is normally released as soon as the run yields.
RESUME_LOCK_SECONDS = 3600
#: Upper bound of one saved snapshot (serialized size). Larger runs fail at the pause with a readable reason.
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


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
    def __init__(
        self,
        redis_client,
        retention_seconds: int,
        clock: Callable[[], float] = time.time,
        lock_seconds: int = RESUME_LOCK_SECONDS,
    ):
        self.redis = redis_client
        self.retention_seconds = int(retention_seconds)
        self.clock = clock
        self.lock_seconds = int(lock_seconds)

    def save(self, unique_id: str, *, tenant_id: int, workflow_id: str, canvas: dict, run_state: dict) -> float:
        """Save the snapshot and return its ``paused_at`` stamp (identifies this particular pause)."""
        try:
            size = len(json.dumps(run_state, default=str).encode("utf-8")) + len(
                json.dumps(canvas, default=str).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            raise RunStateError("the workflow state contains a value that cannot be saved") from exc
        if size > MAX_SNAPSHOT_BYTES:
            raise RunStateError(
                "the workflow state is too large to save (over %d MiB)" % (MAX_SNAPSHOT_BYTES // 1024 // 1024)
            )
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
        # The marker goes first: it outlives the snapshot, and a missing marker would let a warm copy
        # ignore the retention period. A crash between the two writes leaves a marker without a
        # snapshot, which the loader reports as unusable instead of resuming it.
        self.redis.set(
            _meta_key(unique_id),
            {"paused_at": now, "expires_at": expires_at},
            expiration=self.retention_seconds + EXPIRED_NOTICE_GRACE_SECONDS,
        )
        self.redis.set(_snapshot_key(unique_id), snapshot, expiration=self.retention_seconds)
        logger.info("workflow pause state saved unique_id={} bytes={} retention_s={}", unique_id, size, self.retention_seconds)
        return now

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

    def paused_at(self, unique_id: str) -> float | None:
        """The stamp of the newest saved pause of the session, or None if nothing is saved."""
        meta = self.redis.get(_meta_key(unique_id))
        value = meta.get("paused_at") if isinstance(meta, dict) else None
        return float(value) if isinstance(value, (int, float)) else None

    def ensure_not_expired(self, unique_id: str) -> None:
        """Raise the expiry error if the session's retention period is over (a missing marker is fine)."""
        meta = self.redis.get(_meta_key(unique_id))
        expires_at = meta.get("expires_at") if isinstance(meta, dict) else None
        if isinstance(expires_at, (int, float)) and self.clock() > expires_at:
            logger.info("workflow pause state expired unique_id={}", unique_id)
            raise WorkFlowPauseStateExpiredError()

    def delete(self, unique_id: str) -> None:
        for key in (_snapshot_key(unique_id), _meta_key(unique_id)):
            self.redis.delete(key)

    def acquire_resume(self, unique_id: str) -> str | None:
        """Try to become the one task that continues this session; returns the owner token or None."""
        owner = uuid.uuid4().hex
        if self.redis.setNx(_lock_key(unique_id), owner, expiration=self.lock_seconds):
            return owner
        return None

    def release_resume(self, unique_id: str, owner: str) -> None:
        """Release the token, but only if it is still ours (it may have expired and been taken over)."""
        if self.redis.get(_lock_key(unique_id)) == owner:
            self.redis.delete(_lock_key(unique_id))
