# ruff: noqa: RUF001, RUF002, RUF003 — docstrings quote the Chinese acceptance criteria verbatim.
"""F005 AC-32 — a failed run on POST /api/v2/workflow/invoke names the node that failed.

The open face drops every ``node_run`` event (they are progress noise for an
API caller), and the run's closing error carries only the exception text
(``WorkFlowTaskOtherError`` → 10541). The node that failed is named in exactly
one place: its own ``node_run`` end event, the one with ``reason`` set. That one
event is let through; every other ``node_run`` stays dropped, so a successful
run produces exactly the stream it produced before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bisheng.api.services.workflow import WorkFlowService
from bisheng.api.v1.schemas import ChatResponse
from bisheng.workflow.common.workflow import WorkflowStatus
from bisheng.workflow.domain.services.published_workflow_service import PublishedWorkflowService


def _node_run(kind: str, *, reason: str | None = None) -> ChatResponse:
    message = {"unique_id": "exec-1", "node_id": "llm_code", "name": "写代码"}
    if kind == "end":
        message.update(reason=reason, log_data=[{"key": "secret_input", "value": "x"}], input_data={"a": 1})
    return ChatResponse(message=message, category="node_run", type=kind, flow_id="f", chat_id="c")


def _output(text: str) -> ChatResponse:
    return ChatResponse(
        message={"node_id": "output_1", "name": "输出", "msg": text}, category="output_msg", type="over"
    )


class _Callback:
    def __init__(self, events: list[ChatResponse], status: str) -> None:
        self._events = events
        self._status = status
        self.cleared = False

    async def get_response_until_break(self):
        for event in self._events:
            yield event

    async def async_get_workflow_status(self):
        return {"status": self._status}

    async def async_clear_workflow_status(self):
        self.cleared = True


async def _collect(events: list[ChatResponse], status: str, *, stream: bool = False) -> list:
    invocation = SimpleNamespace(callback=_Callback(events, status))
    return [out async for out in PublishedWorkflowService.iter_events(invocation, stream=stream)]


@pytest.mark.parametrize("stream", [False, True])
async def test_failed_node_end_event_is_passed_through(stream: bool) -> None:
    failed = _node_run("end", reason="model 7 is offline")
    outputs = await _collect([_node_run("start"), failed], WorkflowStatus.FAILED.value, stream=stream)

    assert [o.data for o in outputs] == [failed]


@pytest.mark.parametrize("stream", [False, True])
async def test_other_node_run_events_stay_dropped(stream: bool) -> None:
    started, finished, out = _node_run("start"), _node_run("end"), _output("hi")
    outputs = await _collect([started, finished, out], WorkflowStatus.SUCCESS.value, stream=stream)

    # The successful stream is unchanged: no node_run of any kind, then close.
    assert [o.data for o in outputs if not o.close] == [out]
    assert outputs[-1].close is True


def test_failed_node_event_names_node_and_reason() -> None:
    event = WorkFlowService.convert_chat_response_to_workflow_event(_node_run("end", reason="model 7 is offline"))

    assert event.event == "node_run"
    assert (event.node_id, event.node_name, event.node_execution_id) == ("llm_code", "写代码", "exec-1")
    assert event.output_schema is not None and event.output_schema.message == "model 7 is offline"


def test_failed_node_event_carries_no_node_inputs_or_logs() -> None:
    """Only the reason goes out: node inputs and logs can hold whatever the run was fed."""
    event = WorkFlowService.convert_chat_response_to_workflow_event(_node_run("end", reason="boom"))
    dumped = event.model_dump_json()

    assert "secret_input" not in dumped and "input_data" not in dumped and "log_data" not in dumped
