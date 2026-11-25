"""F002 T003: the Redis-backed store of paused workflow runs."""

import pytest

from bisheng.common.errcode.flow import (
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
    owner = store.acquire_resume("u1")
    assert owner
    assert store.acquire_resume("u1") is None
    store.release_resume("u1", owner)
    assert store.acquire_resume("u1")


def test_resume_token_expires_by_itself(env):
    store, _, clock = env
    assert store.acquire_resume("u1")
    clock.now += pause_state.RESUME_LOCK_SECONDS + 1
    assert store.acquire_resume("u1")


def test_a_late_release_does_not_free_the_next_holders_token(env):
    store, _, clock = env
    first = store.acquire_resume("u1")
    clock.now += pause_state.RESUME_LOCK_SECONDS + 1  # the first holder's token expired
    second = store.acquire_resume("u1")
    assert second
    store.release_resume("u1", first)  # the first holder finishes late
    assert store.acquire_resume("u1") is None  # the second holder still owns it
    store.release_resume("u1", second)
    assert store.acquire_resume("u1")


def test_the_token_lifetime_follows_the_configured_limit(env):
    store, redis, clock = env
    store.lock_seconds = 100
    assert store.acquire_resume("u1")
    clock.now += 101
    assert store.acquire_resume("u1")


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


def test_ensure_not_expired_passes_inside_the_retention_period_and_without_a_marker(env):
    store, _, clock = env
    store.ensure_not_expired("never-saved")  # old sessions have no marker
    _save(store)
    clock.now += RETENTION - 1
    store.ensure_not_expired("u1")


def test_ensure_not_expired_raises_after_the_retention_period(env):
    store, _, clock = env
    _save(store)
    clock.now += RETENTION + 1
    with pytest.raises(WorkFlowPauseStateExpiredError):
        store.ensure_not_expired("u1")


def test_save_returns_the_stamp_that_paused_at_reports(env):
    store, _, _ = env
    stamp = store.save("u1", tenant_id=7, workflow_id="wf1", canvas=CANVAS, run_state=RUN_STATE)
    assert store.paused_at("u1") == stamp
    assert store.paused_at("nothing") is None


def test_a_newer_save_changes_the_stamp(env):
    store, _, clock = env
    first = store.save("u1", tenant_id=7, workflow_id="wf1", canvas=CANVAS, run_state=RUN_STATE)
    clock.now += 5
    second = store.save("u1", tenant_id=7, workflow_id="wf1", canvas=CANVAS, run_state=RUN_STATE)
    assert second != first and store.paused_at("u1") == second


def test_an_oversized_snapshot_is_refused_with_a_readable_reason(env, monkeypatch):
    store, redis, _ = env
    monkeypatch.setattr(pause_state, "MAX_SNAPSHOT_BYTES", 100)
    with pytest.raises(RunStateError) as exc:
        store.save("u1", tenant_id=7, workflow_id="wf1", canvas={"nodes": ["x" * 500]}, run_state=RUN_STATE)
    assert "too large" in str(exc.value)
    assert redis.get("workflow:u1:pause") is None


def test_a_snapshot_with_non_text_keys_fails_readably_instead_of_crashing(env):
    store, _, _ = env
    with pytest.raises(RunStateError):
        store.save("u1", tenant_id=7, workflow_id="wf1", canvas={(1, 2): "x"}, run_state=RUN_STATE)


def test_the_size_cap_counts_bytes_not_characters(env, monkeypatch):
    store, _, _ = env
    monkeypatch.setattr(pause_state, "MAX_SNAPSHOT_BYTES", 300)
    chinese = {"nodes": ["节" * 90]}  # about 90 characters but about 270 bytes
    with pytest.raises(RunStateError):
        store.save("u1", tenant_id=7, workflow_id="wf1", canvas=chinese, run_state=RUN_STATE)


def test_the_marker_is_written_before_the_snapshot(env):
    store, redis, _ = env
    order = []
    original = redis.set

    def spy(key, *a, **k):
        order.append(key)
        return original(key, *a, **k)

    redis.set = spy
    _save(store)
    assert order == ["workflow:u1:pause_meta", "workflow:u1:pause"]
