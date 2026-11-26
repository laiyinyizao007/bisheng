"""F008 evaluation: the platform's HTTP routes the runner drives (design §6.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class EvalError(RuntimeError):
    """A platform call failed; the message is scrubbed by the caller before it is recorded."""


@dataclass
class Turn:
    """What one copilot request returned."""

    text: str = ""
    questions: list[dict[str, Any]] | None = None
    proposal: dict[str, Any] | None = None
    issues: list[str] | None = None
    error: str | None = None
    reasoning_chars: int = 0
    events: list[str] = field(default_factory=list)

    def question_lines(self) -> list[str] | None:
        """Each question with its options, exactly as the drawer's ``historyText`` renders them."""
        if not self.questions:
            return None
        return [
            f"Q: {q.get('question', '')} (options: {' / '.join(map(str, q.get('options') or []))})"
            for q in self.questions
        ]

    def history_text(self) -> str:
        """The assistant turn as the next request's history sees it (design §5)."""
        lines = self.question_lines() or []
        return "\n".join([self.text, *lines]).strip()


def _data(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        raise EvalError(f"HTTP {response.status_code} on {response.request.method} {response.request.url.path}")
    body = response.json()
    if isinstance(body, dict) and body.get("status_code") not in (None, 200, 201):
        raise EvalError(
            f"status_code {body.get('status_code')} on {response.request.url.path}: {body.get('status_message')}"
        )
    return body.get("data") if isinstance(body, dict) else body


class BishengApi:
    def __init__(self, client: httpx.Client, *, token: str, tenant: str | None = None) -> None:
        self._client = client
        self.secrets: tuple[str, ...] = (token,)
        # The session cookie the browser would carry; never logged.
        self._client.headers["Cookie"] = f"access_token_cookie={token}"
        if tenant:
            self._client.headers["X-Management-Tenant"] = tenant

    def platform_switches(self) -> dict[str, Any]:
        """Platform switches a case may depend on (``requires`` in a case), as ``/api/v1/env`` reports them."""
        workflow = (_data(self._client.get("/api/v1/env")) or {}).get("workflow") or {}
        return {"code_node_enabled": bool(workflow.get("code_node_enabled"))}

    def templates(self) -> list[dict[str, Any]]:
        return _data(self._client.get("/api/v1/workflow/node_templates")) or []

    def tools(self) -> list[dict[str, Any]]:
        data = _data(self._client.get("/api/v1/tool"))
        return data if isinstance(data, list) else (data or {}).get("data") or []

    def default_model(self) -> int:
        data = _data(self._client.get("/api/v1/llm/workflow")) or {}
        model_id = (data.get("data") or data).get("model_id")
        if model_id is None:
            raise EvalError("the workflow default model is not configured")
        return int(model_id)

    def create_flow(self, name: str) -> str:
        data = _data(
            self._client.post("/api/v1/workflow/create", json={"name": name, "data": {"nodes": [], "edges": []}})
        )
        return str(data["id"])

    def delete_flow(self, flow_id: str) -> None:
        _data(self._client.delete(f"/api/v1/flows/{flow_id}"))

    def copilot(self, flow_id: str, canvas: dict[str, Any], history: list[dict[str, str]], message: str) -> Turn:
        turn = Turn()
        request = {"flow_id": flow_id, "canvas": canvas, "history": history, "message": message}
        with self._client.stream(
            "POST", "/api/v1/workflow/copilot", json=request, timeout=httpx.Timeout(30, read=400)
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise EvalError(f"HTTP {response.status_code} on copilot")
            for line in response.iter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind = event.get("type")
                turn.events.append(kind)
                if kind == "message":
                    turn.text = event.get("text") or ""
                elif kind == "delta" and not turn.text:
                    pass  # the final ``message`` carries the reply
                elif kind == "reasoning":
                    turn.reasoning_chars += len(event.get("text") or "")
                elif kind == "ask":
                    turn.questions = event.get("questions") or []
                elif kind == "proposal":
                    turn.proposal = event.get("canvas")
                elif kind == "issues":
                    turn.issues = event.get("issues") or []
                elif kind == "error":
                    turn.error = f"{event.get('code')}: {event.get('message')}"
        return turn
