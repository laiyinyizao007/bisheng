"""F002 T003: the Redis-backed store of paused workflow runs."""

import pytest

from bisheng.common.errcode.flow import (
    WorkFlowPauseResumeConflictError,
    WorkFlowPauseStateExpiredError,
    WorkFlowPauseStateInvalidError,
)
from bisheng.workflow.domain.services import pause_state
from bisheng.workflow.domain.services.pause_state import PauseStateNotFound, PauseStateStore
from bisheng.workflow.graph.run_state import RunStateError


class FakeRedis:
    """Just enough of RedisClient: values with an expiry measured on an injectable clock."""

    def __init__(self, clock):
        self.clock = clock
        self.data: dict[str, tuple[object, float]] = {}
        self.unavailable = False

    def _check(self):
        if self.unavailable:
            raise ConnectionError("redis down")

    def set(self, key, value, expiration=3600, enx=None):
        self._check()
        self.data[key] = (value, self.clock.now + expiration)

    def setNx(self, key, value, expiration=3600):
        self._check()
        if self.get(key) is not None:
            return False
        self.set(key, value, expiration)
        return True

    def get(self, key):
        self._check()
        item = self.data.get(key)
        if item is None or item[1] <= self.clock.now:
            return None
        return item[0]

    def delete(self, key):
        self._check()
        self.data.pop(key, None)

    def expire_key(self, key, expiration):
        self._check()
        if key in self.data:
            self.data[key] = (self.data[key][0], self.clock.now + expiration)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


RUN_STATE = {"format": 1, "status": "INPUT", "checkpoint": {"t": "x", "b": "y"}, "graph_state": {}, "steps": {"a": 1}}
CANVAS = {"nodes": [{"id": "a"}], "edges": []}
RETENTION = 3600


@pytest.fixture
def env():
    clock = Clock()
    redis = FakeRedis(clock)
    store = PauseStateStore(redis, retention_seconds=RETENTION, clock=clock)
    return store, redis, clock


def _save(store, uid="u1", tenant=7, wf="wf1"):
    store.save(uid, tenant_id=tenant, workflow_id=wf, canvas=CANVAS, run_state=RUN_STATE)


def test_save_then_load_returns_what_was_saved(env):
    store, _, _ = env
    _save(store)
    snap = store.load("u1", tenant_id=7, workflow_id="wf1")
    assert snap.canvas == CANVAS
    assert snap.run_state == RUN_STATE


def test_the_snapshot_expires_after_the_retention_period_with_a_clear_error(env):
    store, _, clock = env
    _save(store)
    clock.now += RETENTION + 1
    with pytest.raises(WorkFlowPauseStateExpiredError):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_expired_state_is_removed_when_it_is_reported(env):
    store, redis, clock = env
    _save(store)
    clock.now += RETENTION + 1
    with pytest.raises(WorkFlowPauseStateExpiredError):
        store.load("u1", tenant_id=7, workflow_id="wf1")
    assert redis.data.get("workflow:u1:pause") is None or redis.get("workflow:u1:pause") is None


def test_a_session_without_any_saved_state_is_not_found_not_expired(env):
    store, _, _ = env
    with pytest.raises(PauseStateNotFound):
        store.load("never-saved", tenant_id=7, workflow_id="wf1")


def test_after_the_snapshot_is_gone_the_marker_still_says_expired(env):
    store, redis, clock = env
    _save(store)
    redis.delete("workflow:u1:pause")  # the big value is dropped first by its own expiry
    clock.now += RETENTION + 1
    with pytest.raises(WorkFlowPauseStateExpiredError):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_the_marker_outlives_the_snapshot_by_a_grace_period(env):
    store, redis, clock = env
    _save(store)
    clock.now += RETENTION + pause_state.EXPIRED_NOTICE_GRACE_SECONDS + 1
    with pytest.raises(PauseStateNotFound):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_another_tenant_or_workflow_cannot_load_the_snapshot(env):
    store, _, _ = env
    _save(store)
    with pytest.raises(WorkFlowPauseStateInvalidError):
        store.load("u1", tenant_id=8, workflow_id="wf1")
    with pytest.raises(WorkFlowPauseStateInvalidError):
        store.load("u1", tenant_id=7, workflow_id="other")


def test_a_damaged_snapshot_is_reported_as_invalid(env):
    store, redis, _ = env
    _save(store)
    redis.set("workflow:u1:pause", {"unexpected": True}, expiration=RETENTION)
    with pytest.raises(WorkFlowPauseStateInvalidError):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_an_unknown_format_is_reported_as_invalid(env):
    store, redis, _ = env
    _save(store)
    value, _exp = redis.data["workflow:u1:pause"]
    redis.set("workflow:u1:pause", {**value, "format": 99}, expiration=RETENTION)
    with pytest.raises(WorkFlowPauseStateInvalidError):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_delete_removes_state_and_marker(env):
    store, redis, _ = env
    _save(store)
    store.delete("u1")
    with pytest.raises(PauseStateNotFound):
        store.load("u1", tenant_id=7, workflow_id="wf1")
    assert redis.get("workflow:u1:pause_meta") is None


def test_only_one_caller_gets_the_resume_token(env):
    store, _, _ = env
    assert store.acquire_resume("u1") is True
    assert store.acquire_resume("u1") is False
    store.release_resume("u1")
    assert store.acquire_resume("u1") is True


def test_resume_token_expires_by_itself(env):
    store, _, clock = env
    assert store.acquire_resume("u1") is True
    clock.now += pause_state.RESUME_LOCK_SECONDS + 1
    assert store.acquire_resume("u1") is True


def test_require_resume_raises_the_conflict_error(env):
    store, _, _ = env
    store.require_resume("u1")
    with pytest.raises(WorkFlowPauseResumeConflictError):
        store.require_resume("u1")


def test_redis_failure_is_not_reported_as_expiry(env):
    store, redis, _ = env
    _save(store)
    redis.unavailable = True
    with pytest.raises(ConnectionError):
        store.load("u1", tenant_id=7, workflow_id="wf1")


def test_save_surfaces_a_run_state_error_unchanged(env):
    store, _, _ = env

    class Boom:
        def set(self, *a, **k):
            raise RunStateError("variable x.y cannot be saved")

    s = PauseStateStore(Boom(), retention_seconds=RETENTION, clock=Clock())
    with pytest.raises(RunStateError):
        s.save("u1", tenant_id=7, workflow_id="wf1", canvas=CANVAS, run_state=RUN_STATE)
