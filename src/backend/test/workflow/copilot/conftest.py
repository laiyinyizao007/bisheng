"""Shared fixtures for the F007 copilot tests: a scripted model and a small catalogue.

``scripted_llm`` replaces ``LLMService.get_bisheng_llm``: each model call takes the
next scripted answer (a string streamed in small chunks, or an exception raised on
the first chunk) and records the messages it was given and the factory kwargs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from bisheng.workflow.domain.services.copilot import resources as resources_module
from bisheng.workflow.domain.services.copilot import service as service_module
from bisheng.workflow.domain.services.copilot.resources import Catalogue, Resource


@pytest.fixture(autouse=True)
def _fresh_catalogue_cache():
    resources_module._catalogue_cache.clear()
    yield
    resources_module._catalogue_cache.clear()


def tool_child(name: str, tool_key: str, params: list[str], child_id: int) -> dict[str, Any]:
    return {
        "id": child_id,
        "name": name,
        "tool_key": tool_key,
        "node_template": {
            "id": child_id,
            "tool_key": tool_key,
            "type": "tool",
            "name": name,
            "description": "",
            "group_params": [
                {"name": "工具参数", "params": [{"key": p, "type": "var_textarea", "value": ""} for p in params]},
                {"name": "输出", "params": [{"key": "output", "type": "var", "global": "key", "value": ""}]},
            ],
        },
    }


def make_catalogue(**overrides: Any) -> Catalogue:
    base = {
        "models": [Resource(1, "通义千问"), Resource(2, "深度求索")],
        "agent_models": [Resource(1, "通义千问")],
        "knowledge": [Resource(11, "研报库")],
        "qa_knowledge": [],
        "tools": [{"id": 3, "name": "invest", "children": [tool_child("submit_candidate", "k7", ["name"], 7)]}],
        "default_model_id": 1,
        "default_agent_model_id": 1,
    }
    base.update(overrides)
    return Catalogue(**base)


class ScriptedLLM:
    def __init__(self, script: list[Any], state: dict[str, Any]) -> None:
        self._script = script
        self._state = state

    async def astream(self, messages: list[Any]):
        self._state["calls"].append({"messages": [(type(m).__name__, m.content) for m in messages]})
        answer = self._script.pop(0)
        if self._state["gate"] is not None:
            try:
                await self._state["gate"].wait()  # lets a test hold the call open and cancel it
            except asyncio.CancelledError:
                self._state["cancelled"] = True
                raise
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):  # (reasoning, reply): reasoning first, as the platform LLM streams it
            reasoning, answer = answer
            for start in range(0, len(reasoning), 7):
                yield SimpleNamespace(content="", additional_kwargs={"reasoning_content": reasoning[start : start + 7]})
        for start in range(0, len(answer), 7):
            yield SimpleNamespace(content=answer[start : start + 7])


@pytest.fixture
def scripted_llm(monkeypatch: pytest.MonkeyPatch):
    state: dict[str, Any] = {"calls": [], "factory": [], "gate": None, "cancelled": False}

    def install(*answers: Any) -> dict[str, Any]:
        script = list(answers)

        async def factory(**kwargs: Any) -> ScriptedLLM:
            state["factory"].append(kwargs)
            return ScriptedLLM(script, state)

        monkeypatch.setattr(service_module.LLMService, "get_bisheng_llm", factory)
        return state

    return install
