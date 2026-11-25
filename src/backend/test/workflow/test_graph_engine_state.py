"""F002 T002: exporting and importing the resumable state of a paused workflow run.

The state of a run that waits for user input is: the LangGraph checkpoint, the
variable pool with the chat history, and each node's step counter. It must go to
bytes and come back into a freshly built graph without touching private internals
of the in-memory saver.
"""

import copy
import operator
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from bisheng.workflow.common.workflow import WorkflowStatus
from bisheng.workflow.graph import run_state
from bisheng.workflow.graph.graph_engine import GraphEngine
from bisheng.workflow.graph.graph_state import GraphState

CONFIG = {"configurable": {"thread_id": "1"}}


class _State(TypedDict, total=False):
    flag: bool
    trail: Annotated[list, operator.add]
    answer: str


def _build(saver):
    """a -> b (interrupted before it runs) -> c; node b reads what a produced."""
    seen = {"c": 0}

    def a(state):
        return {"trail": ["a"]}

    def b(state):
        return {"trail": ["b"], "answer": "got:" + ",".join(state.get("trail", []))}

    def c(state):
        seen["c"] += 1
        return {"trail": ["c"]}

    g = StateGraph(_State)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_node("c", c)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g.compile(checkpointer=saver, interrupt_before=["b"]), seen


def _run_to_pause():
    saver = MemorySaver()
    graph, seen = _build(saver)
    for _ in graph.stream({"flag": True}, config=CONFIG):
        pass
    assert graph.get_state(CONFIG).next == ("b",)
    return saver, graph, seen


def test_checkpoint_round_trip_resumes_in_a_fresh_graph():
    old_saver, old_graph, _ = _run_to_pause()
    data = run_state.dump_checkpoint(old_saver, CONFIG)
    assert isinstance(data, dict)

    new_saver = MemorySaver()
    new_graph, seen = _build(new_saver)
    run_state.load_checkpoint(new_saver, CONFIG, data)

    assert new_graph.get_state(CONFIG).next == ("b",)
    for _ in new_graph.stream(None, config=CONFIG):
        pass
    final = new_graph.get_state(CONFIG)
    assert final.next == ()
    assert final.values["answer"] == "got:a"
    assert final.values["trail"] == ["a", "b", "c"]
    assert seen["c"] == 1


def test_exported_checkpoint_is_plain_json_data():
    import json

    saver, _, _ = _run_to_pause()
    data = run_state.dump_checkpoint(saver, CONFIG)
    assert json.loads(json.dumps(data)) == data


def test_dump_without_a_checkpoint_returns_none():
    assert run_state.dump_checkpoint(MemorySaver(), CONFIG) is None


def test_loading_garbage_raises_a_readable_error():
    with pytest.raises(run_state.RunStateError):
        run_state.load_checkpoint(MemorySaver(), CONFIG, {"t": "nope", "b": "!!"})


def test_graph_state_round_trip_keeps_variables_and_history():
    gs = GraphState(chat_id="chat-1", memory_enabled=True)
    from langchain_classic.memory import ConversationBufferWindowMemory

    gs.history_memory = ConversationBufferWindowMemory(k=10)
    gs.save_context("hello", "human")
    gs.save_context("hi there", "AI")
    gs.set_variable("llm_1", "output", "answer text")
    gs.set_variable("input_1", "files", [{"name": "a.txt", "size": 3}])
    gs.set_variable("start_1", "custom_variables", {"k": "v"})

    data = run_state.dump_graph_state(gs)

    fresh = GraphState(chat_id="chat-1", memory_enabled=True)
    fresh.history_memory = ConversationBufferWindowMemory(k=10)
    run_state.load_graph_state(fresh, data)

    assert fresh.variables_pool == gs.variables_pool
    assert [type(m) for m in fresh.history_memory.chat_memory.messages] == [HumanMessage, AIMessage]
    assert [m.content for m in fresh.history_memory.chat_memory.messages] == ["hello", "hi there"]


def test_unserializable_variable_names_the_variable():
    gs = GraphState()
    gs.set_variable("tool_1", "handle", object())
    with pytest.raises(run_state.RunStateError) as exc:
        run_state.dump_graph_state(gs)
    assert "tool_1.handle" in str(exc.value)


def _stub_engine(saver, graph, counters):
    engine = GraphEngine.__new__(GraphEngine)
    engine.graph_config = copy.deepcopy(CONFIG)
    engine.graph = graph
    engine._saver = saver
    engine.graph_state = GraphState()
    from langchain_classic.memory import ConversationBufferWindowMemory

    engine.graph_state.history_memory = ConversationBufferWindowMemory(k=10)
    engine.nodes_map = {nid: SimpleNamespace(current_step=n) for nid, n in counters.items()}
    engine.status = WorkflowStatus.INPUT.value
    engine.reason = ""
    return engine


def test_engine_export_and_import_restore_counters_and_status():
    saver, graph, _ = _run_to_pause()
    engine = _stub_engine(saver, graph, {"a": 3, "b": 0, "c": 1})
    engine.graph_state.set_variable("a", "out", "x")

    data = engine.export_run_state()

    new_saver = MemorySaver()
    new_graph, _ = _build(new_saver)
    fresh = _stub_engine(new_saver, new_graph, {"a": 0, "b": 0, "c": 0})
    fresh.status = WorkflowStatus.RUNNING.value
    fresh.import_run_state(data)

    assert {k: v.current_step for k, v in fresh.nodes_map.items()} == {"a": 3, "b": 0, "c": 1}
    assert fresh.graph_state.variables_pool == {"a": {"out": "x"}}
    assert fresh.status == WorkflowStatus.INPUT.value
    assert fresh.graph.get_state(fresh.graph_config).next == ("b",)


def test_import_rejects_a_snapshot_for_other_nodes():
    saver, graph, _ = _run_to_pause()
    data = _stub_engine(saver, graph, {"a": 1, "b": 0, "c": 0}).export_run_state()

    new_saver = MemorySaver()
    new_graph, _ = _build(new_saver)
    other = _stub_engine(new_saver, new_graph, {"a": 0, "z": 0})
    with pytest.raises(run_state.RunStateError):
        other.import_run_state(data)


def test_import_rejects_an_unknown_format_version():
    saver, graph, _ = _run_to_pause()
    data: dict[str, Any] = _stub_engine(saver, graph, {"a": 0, "b": 0, "c": 0}).export_run_state()
    data["format"] = 999
    new_saver = MemorySaver()
    new_graph, _ = _build(new_saver)
    with pytest.raises(run_state.RunStateError):
        _stub_engine(new_saver, new_graph, {"a": 0, "b": 0, "c": 0}).import_run_state(data)


def test_nodes_without_a_step_counter_are_skipped():
    saver, graph, _ = _run_to_pause()
    engine = _stub_engine(saver, graph, {"a": 2, "b": 0, "c": 0})
    engine.nodes_map["b_fake"] = SimpleNamespace()  # e.g. the output placeholder node
    data = engine.export_run_state()
    assert set(data["steps"]) == {"a", "b", "c"}

    new_saver = MemorySaver()
    new_graph, _ = _build(new_saver)
    fresh = _stub_engine(new_saver, new_graph, {"a": 0, "b": 0, "c": 0})
    fresh.nodes_map["b_fake"] = SimpleNamespace()
    fresh.import_run_state(data)
    assert fresh.nodes_map["a"].current_step == 2


def test_retrieval_documents_and_citation_items_survive_the_round_trip():
    """What knowledge/RAG nodes keep in the variable pool: LangChain documents and pydantic citation items."""
    from langchain_core.documents import Document

    from bisheng.citation.domain.schemas.citation_schema import CitationRegistryItemSchema

    docs = [Document(page_content="第二十条 内容", metadata={"citation_key": "k1", "source": "a.pdf", "page": 3})]
    item = CitationRegistryItemSchema.model_validate(
        {
            "citationId": "c1",
            "type": "rag",
            "sourcePayload": {"type": "rag"},
        }
    )
    gs = GraphState()
    gs.set_variable("rag_1", "source_documents", docs)
    gs.set_variable("rag_1", "citation_items", [item])
    gs.set_variable("code_1", "result", {"n": 1, "ok": True, "tags": ["a", "b"], "none": None})

    data = run_state.dump_graph_state(gs)
    fresh = GraphState()
    run_state.load_graph_state(fresh, data)

    assert fresh.variables_pool["rag_1"]["source_documents"] == docs
    assert fresh.variables_pool["rag_1"]["citation_items"] == [item]
    assert fresh.variables_pool["code_1"]["result"] == {"n": 1, "ok": True, "tags": ["a", "b"], "none": None}
