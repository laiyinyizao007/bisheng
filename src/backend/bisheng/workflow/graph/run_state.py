"""Export and import the resumable state of a workflow run that waits for user input (F002).

Only the pieces that cannot be rebuilt from the canvas are handled here:

* the LangGraph checkpoint (what ran, what runs next, the channel values);
* the shared variable pool and the chat history;
* nothing else — nodes, callbacks and clients are rebuilt by the caller.

Everything goes through public APIs (the saver's ``get_tuple`` / ``put`` / ``put_writes`` and
LangGraph's own serializer), so the in-memory saver's private storage layout is not relied on.
No payload is ever written to a log from here: errors name a variable, never its value.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from typing import Any

from langchain_classic.memory import ConversationBufferWindowMemory
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from bisheng.workflow.graph.graph_state import GraphState

FORMAT_VERSION = 1

_serde = JsonPlusSerializer()


class RunStateError(Exception):
    """The run state could not be saved or restored; the message is safe to show and to log."""


def _pack(serde, obj: Any) -> dict[str, str]:
    type_, data = serde.dumps_typed(obj)
    return {"t": type_, "b": base64.b64encode(data).decode("ascii")}


def _unpack(serde, packed: Any) -> Any:
    try:
        type_ = packed["t"]
        data = base64.b64decode(packed["b"], validate=True)
        return serde.loads_typed((type_, data))
    except Exception as exc:  # noqa: BLE001 - any decoding problem means an unusable snapshot
        raise RunStateError("the saved run state is damaged") from exc


def dump_checkpoint(saver, config: dict) -> dict[str, str] | None:
    """The newest checkpoint of the thread with its pending writes, or None if there is none."""
    tup = saver.get_tuple(config)
    if tup is None:
        return None
    bundle = {
        "checkpoint": tup.checkpoint,
        "metadata": tup.metadata,
        "writes": [(task_id, channel, value) for task_id, channel, value in (tup.pending_writes or [])],
    }
    try:
        return _pack(saver.serde, bundle)
    except Exception as exc:  # noqa: BLE001
        raise RunStateError("the workflow state contains a value that cannot be saved") from exc


def load_checkpoint(saver, config: dict, packed: dict[str, str]) -> None:
    """Make ``packed`` the newest checkpoint of the thread in ``saver``."""
    bundle = _unpack(saver.serde, packed)
    try:
        checkpoint = bundle["checkpoint"]
        configurable = config["configurable"]
        put_config = {"configurable": {"thread_id": configurable["thread_id"], "checkpoint_ns": ""}}
        stored_config = saver.put(put_config, checkpoint, bundle["metadata"], checkpoint["channel_versions"])
        by_task: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for task_id, channel, value in bundle["writes"]:
            by_task[task_id].append((channel, value))
        for task_id, writes in by_task.items():
            saver.put_writes(stored_config, writes, task_id)
    except RunStateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RunStateError("the saved run state cannot be restored") from exc


def dump_graph_state(graph_state: GraphState) -> dict[str, str]:
    """The variable pool and the chat history of the run."""
    for node_id, values in graph_state.variables_pool.items():
        for key, value in values.items():
            try:
                _serde.dumps_typed(value)
            except Exception as exc:  # noqa: BLE001
                raise RunStateError(f"variable {node_id}.{key} cannot be saved") from exc
    memory = graph_state.history_memory
    messages = list(memory.chat_memory.messages) if memory is not None else []
    try:
        return _pack(_serde, {"variables": graph_state.variables_pool, "history": messages})
    except Exception as exc:  # noqa: BLE001
        raise RunStateError("the chat history cannot be saved") from exc


def load_graph_state(graph_state: GraphState, packed: dict[str, str]) -> None:
    bundle = _unpack(_serde, packed)
    try:
        graph_state.variables_pool = dict(bundle["variables"])
        if graph_state.history_memory is None:
            graph_state.history_memory = ConversationBufferWindowMemory(k=10)
        graph_state.history_memory.chat_memory.messages = list(bundle["history"])
    except Exception as exc:  # noqa: BLE001
        raise RunStateError("the saved run state cannot be restored") from exc
