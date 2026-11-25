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
