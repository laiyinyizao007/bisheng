"""F002 T004/T005: a paused run survives the loss of the Worker that paused it.

Runs the real engine, nodes and task functions in-process (the ``dialogue`` harness of
``test_public_workflow_continuation``). "The Worker restarted" is simulated by emptying the
process-local dictionary of paused runs, which is exactly what a restart does.
"""

import pytest

from test.workflow.memory_fixtures import import_worker_tasks
from test.workflow.test_public_workflow_continuation import (  # noqa: F401  (dialogue is a fixture)
    CHAT,
    FID,
    Socket,
    admission,
    answer,
    dialogue,
    initialize,
    store,
    tenant,
)


def _keys(h, suffix):
    return [k for k in h.cache.values if k.endswith(suffix)]


def _forget_the_paused_object():
    tasks = import_worker_tasks()
    tasks._global_workflow.clear()


async def test_pause_state_is_saved_when_the_run_waits_for_input(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        await initialize(socket)
    assert len(_keys(h, ":pause")) == 1
    assert len(_keys(h, ":pause_meta")) == 1


async def test_input_after_the_worker_lost_the_run_still_continues_it(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        frames = await socket.until("processing", "close")
    assert not [f for f in frames if f["category"] == "error"]
    assert any(f["category"] == "output_msg" and f["message"]["msg"] == "Synthetic model answer" for f in frames)
    assert h.model.invoke.call_count == 1


async def test_pause_state_is_removed_when_the_run_finishes(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        await socket.until("processing", "close")
    assert _keys(h, ":pause") == []
    assert _keys(h, ":pause_meta") == []
    assert _keys(h, ":pause_lock") == []


async def test_a_session_without_saved_state_keeps_the_old_failure(dialogue):
    """AC-15: a run paused before the upgrade has no saved state; it fails as it always did."""
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        for key in _keys(h, ":pause") + _keys(h, ":pause_meta"):
            h.cache.delete(key)
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        frames = await socket.until("processing", "close")
    assert [f for f in frames if f["category"] == "error"]
    assert h.model.invoke.call_count == 0


async def test_input_after_the_retention_period_reports_expiry(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        for key in _keys(h, ":pause") + _keys(h, ":pause_meta"):
            value = h.cache.get(key)
            value["expires_at"] = 1.0  # long ago
            h.cache.set(key, value)
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        frames = await socket.until("processing", "close")
    errors = [f for f in frames if f["category"] == "error"]
    assert errors
    assert errors[-1]["message"]["status_code"] == 10544
    assert h.model.invoke.call_count == 0
    assert _keys(h, ":pause") == []


async def test_a_damaged_snapshot_fails_the_session_with_a_readable_error(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        for key in _keys(h, ":pause"):
            h.cache.set(key, {"unexpected": True})
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        frames = await socket.until("processing", "close")
    errors = [f for f in frames if f["category"] == "error"]
    assert errors
    assert errors[-1]["message"]["status_code"] == 10545
    assert h.model.invoke.call_count == 0


@pytest.mark.parametrize("dialogue", [True], indirect=True)
async def test_a_looping_workflow_can_be_resumed_twice_across_restarts(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        _forget_the_paused_object()
        await socket.send(answer(prompt))
        frames = await socket.until("input")
        second = frames[-1]
        assert second["category"] == "input"
        _forget_the_paused_object()
        await socket.send(answer(second))
        frames = await socket.until("input")
    assert not [f for f in frames if f["category"] == "error"]
    assert h.model.invoke.call_count == 2


async def test_stopping_a_paused_session_does_not_need_the_original_worker(dialogue, monkeypatch):
    """AC-10: the session is ended and its saved state removed even though no Worker holds the run."""
    h = dialogue
    async with Socket(h.app) as socket:
        await initialize(socket)
    pause_key = _keys(h, ":pause")[0]
    unique_id = pause_key.split(":")[1]
    _forget_the_paused_object()

    tasks = import_worker_tasks()
    import asyncio
    from contextlib import contextmanager

    @contextmanager
    def scope(*_args, **_kwargs):
        yield 101  # tenant resolution from the database is covered elsewhere

    monkeypatch.setattr(tasks, "durable_workload_scope", scope)
    await asyncio.to_thread(tasks.stop_workflow.run, unique_id, FID, CHAT, 11)

    assert _keys(h, ":pause") == []
    assert _keys(h, ":pause_meta") == []
    status = h.cache.get(f"workflow:{unique_id}:status")
    assert status["status"] == "FAILED"


async def test_the_warm_in_memory_copy_is_not_resumable_after_the_retention_period(dialogue):
    """The Worker that still holds the run must not keep a session alive past its retention period."""
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        for key in _keys(h, ":pause_meta"):
            value = h.cache.get(key)
            value["expires_at"] = 1.0
            h.cache.set(key, value)
        await socket.send(answer(prompt))  # the object is still in this process
        frames = await socket.until("processing", "close")
    errors = [f for f in frames if f["category"] == "error"]
    assert errors and errors[-1]["message"]["status_code"] == 10544
    assert h.model.invoke.call_count == 0


async def test_a_stale_warm_copy_is_not_used_after_another_worker_continued_the_session(dialogue):
    """If the session was continued elsewhere since this Worker paused it, its in-memory copy is stale."""
    h = dialogue
    tasks = import_worker_tasks()

    class Stale:
        """Stands in for an out-of-date object; using it would break the run."""

        tenant_id = 101
        workflow_id = FID

        def status(self):
            raise AssertionError("the stale in-memory copy must not be used")

    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        unique_id = _keys(h, ":pause")[0].split(":")[1]
        tasks._global_workflow[unique_id] = Stale()
        for key in _keys(h, ":pause_meta"):
            value = h.cache.get(key)
            value["paused_at"] = value["paused_at"] + 5  # a newer pause was saved by another Worker
            h.cache.set(key, value)
        await socket.send(answer(prompt))
        frames = await socket.until("processing", "close")
    assert not [f for f in frames if f["category"] == "error"]
    assert h.model.invoke.call_count == 1


def _unique_id(h):
    return _keys(h, ":pause")[0].split(":")[1]


async def _settle():
    import asyncio

    await asyncio.sleep(0.5)


async def test_a_second_continue_does_not_run_while_another_task_owns_the_session(dialogue):
    """The token guards the warm path too: with the token taken, the continue task exits and runs nothing."""
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        unique_id = _unique_id(h)
        h.cache.set(f"workflow:{unique_id}:pause_lock", "someone-else")
        await socket.send(answer(prompt))
        await _settle()
    assert h.model.invoke.call_count == 0
    assert h.cache.get(f"workflow:{unique_id}:pause_lock") == "someone-else"  # the loser left it alone
    assert len(_keys(h, ":pause")) == 1  # the saved state is untouched


async def test_a_session_stopped_before_the_continue_is_not_resurrected(dialogue):
    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        unique_id = _unique_id(h)
        status = h.cache.get(f"workflow:{unique_id}:status")
        h.cache.set(f"workflow:{unique_id}:status", {**status, "status": "FAILED", "reason": "workflow stop by user"})
        await socket.send(answer(prompt))
        await _settle()
    assert h.model.invoke.call_count == 0
    assert h.cache.get(f"workflow:{unique_id}:status")["status"] == "FAILED"


async def test_a_redis_outage_while_continuing_keeps_the_saved_state(dialogue):
    """Nothing ran yet, so the saved state must survive and the session goes back to waiting."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    h = dialogue
    async with Socket(h.app) as socket:
        prompt = await initialize(socket)
        unique_id = _unique_id(h)
        original = h.cache.setNx

        def down(*args, **kwargs):
            raise RedisConnectionError("redis down")

        h.cache.setNx = down
        try:
            await socket.send(answer(prompt))
            await _settle()
        finally:
            h.cache.setNx = original
    assert h.model.invoke.call_count == 0
    assert len(_keys(h, ":pause")) == 1 and len(_keys(h, ":pause_meta")) == 1
    assert h.cache.get(f"workflow:{unique_id}:status")["status"] == "INPUT"


async def test_a_redis_outage_at_the_pause_does_not_lose_the_run(dialogue):
    """The in-memory copy comes first: without Redis the session still works on this Worker."""
    from redis.exceptions import ConnectionError as RedisConnectionError

    h = dialogue
    original = h.cache.set

    def flaky(key, value, **kwargs):
        if key.endswith((":pause", ":pause_meta")):
            raise RedisConnectionError("redis down")
        return original(key, value, **kwargs)

    h.cache.set = flaky
    try:
        async with Socket(h.app) as socket:
            prompt = await initialize(socket)
            assert _keys(h, ":pause") == []  # nothing could be saved
            await socket.send(answer(prompt))
            frames = await socket.until("processing", "close")
    finally:
        h.cache.set = original
    assert not [f for f in frames if f["category"] == "error"]
    assert h.model.invoke.call_count == 1
