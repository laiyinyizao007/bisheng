"""T036 — ``bisheng flow run``.

Runs go through the platform's open invoke face, streamed: the session id rides
on the first event, so even a long round can be continued (the non-streaming
reply only carried it once the whole round was over).

What is pinned:

* events are shown as they come (AC-30);
* a pause names the node and what it waits for, and a continuation sends
  **both** the session id and the id of the message that is waiting (AC-31);
* a failed node is named, with its reason — not just "the run ended" (AC-32);
* with nobody at the keyboard the command never waits for one: a one-shot
  input is used once, and the next pause ends the command with everything
  needed to continue (AC-33).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from bisheng_cli.errors import (
    EXIT_CANCELLED,
    EXIT_DEFECT,
    EXIT_LOCAL_INVALID,
    EXIT_OK,
    EXIT_PRECHECK_FAILED,
    EXIT_USAGE,
    EXIT_WAIT_TIMEOUT,
)
from tests.helpers.flow_command import (
    FLOW_ID,
    INVOKE_PATH,
    bodies,
    events,
    login,
    result,
    run_cli,
    sse,
    write_project,
)
from tests.helpers.platform_mock import PlatformMock, v2_error

SESSION = "chat1_async_task_id"


def _event(kind: str, **fields) -> dict:
    base = {
        "event": kind,
        "message_id": None,
        "status": "end",
        "node_id": None,
        "node_name": None,
        "node_execution_id": None,
        "output_schema": None,
        "input_schema": None,
    }
    return {**base, **fields}


def _output(text: str, node: str = "output_1") -> dict:
    return _event("output_msg", node_id=node, node_name="结果", message_id="40", output_schema={"message": text})


def _dialog_wait(message_id: str = "38") -> dict:
    return _event(
        "input",
        node_id="input_1",
        node_name="研究想法",
        message_id=message_id,
        input_schema={
            "input_type": "dialog_input",
            "value": [
                {"key": "user_input", "type": "text", "required": True, "value": ""},
                {"key": "dialog_files_content", "type": "dialog_file", "value": []},
            ],
        },
    )


def _form_wait() -> dict:
    return _event(
        "input",
        node_id="form_1",
        node_name="参数表",
        message_id="39",
        input_schema={
            "input_type": "form_input",
            "value": [
                {"key": "symbol", "type": "text", "label": "品种", "required": True},
                {"key": "period", "type": "select", "label": "周期", "required": True, "options": ["15m", "1d"]},
            ],
        },
    )


CLOSE = _event("close")


def _failed_close(code: int = 10541, reason: str = "model 7 is offline") -> dict:
    message = {"status_code": code, "status_message": "Workflow task execution failed", "data": {"exception": reason}}
    return _event("close", output_schema={"message": message})


@pytest.fixture
def spec(home_dir, tmp_path: Path) -> Path:
    login()
    return write_project(tmp_path / "proj")


def _run(spec: Path, monkeypatch, replies, *extra: str, **kw) -> tuple[int, str, str, PlatformMock]:
    mock = PlatformMock().post(INVOKE_PATH, list(replies))
    code, out, err = run_cli(["flow", "run", str(spec), "--flow-id", FLOW_ID, *extra], monkeypatch, mock, **kw)
    return code, out, err, mock


def test_events_are_streamed(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    guide = _event("guide_word", output_schema={"message": "你好，说说你的想法"})
    code, out, err, mock = _run(spec, monkeypatch, [sse(guide, _output("回测通过"), CLOSE)], "--json")

    assert code == EXIT_OK
    (body,) = bodies(mock, "POST", INVOKE_PATH)
    assert body == {"workflow_id": FLOW_ID, "stream": True}
    shown = [e for e in events(out) if e["event"] == "flow_event"]
    assert [e["type"] for e in shown] == ["guide_word", "output_msg", "close"]
    assert "你好，说说你的想法" in err and "回测通过" in err
    final = result(out)
    assert final["data"]["status"] == "finished" and final["data"]["session_id"] == SESSION


def test_stream_chunks_are_not_flooded(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        _event("stream_msg", status="stream", node_id="llm_1", output_schema={"message": c}) for c in ("回", "测")
    ]
    done = _event("stream_msg", status="end", node_id="llm_1", node_name="写代码", output_schema={"message": "回测"})
    code, out, err, _ = _run(spec, monkeypatch, [sse(*chunks, done, CLOSE)], "--json")

    assert code == EXIT_OK
    assert [e["type"] for e in events(out) if e["event"] == "flow_event"] == ["stream_msg", "close"]
    assert err.count("回测") == 1


def test_reports_paused_node_and_expected_input(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _ = _run(spec, monkeypatch, [sse(_output("先看结果"), _dialog_wait())], "--json")

    assert code == EXIT_OK
    data = result(out)["data"]
    assert data["status"] == "waiting_input"
    assert (data["node_id"], data["message_id"], data["session_id"]) == ("input_1", "38", SESSION)
    assert [f["key"] for f in data["fields"]] == ["user_input"]
    assert "input_1" in err and "研究想法" in err and "user_input" in err
    assert data["resume"] and data["resume"] in err  # the way to continue is printed, not only emitted


def test_continuation_sends_session_and_message_id(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [sse(_dialog_wait()), sse(_output("收到"), CLOSE)]
    code, out, _, mock = _run(spec, monkeypatch, replies, "--input", "做 EB 的均值回归", "--json")

    assert code == EXIT_OK
    first, second = bodies(mock, "POST", INVOKE_PATH)
    assert first == {"workflow_id": FLOW_ID, "stream": True}
    assert second == {
        "workflow_id": FLOW_ID,
        "stream": True,
        "session_id": SESSION,
        "message_id": 38,
        "input": {"input_1": {"user_input": "做 EB 的均值回归"}},
    }
    assert result(out)["data"]["status"] == "finished"


def test_resume_continues_across_invocations(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run(spec, monkeypatch, [sse(_dialog_wait())], "--json")
    token = result(out)["data"]["resume"]

    code, out, _, mock = _run(spec, monkeypatch, [sse(_output("收到"), CLOSE)], "--resume", token, "--input", "hi")

    assert code == EXIT_OK
    (body,) = bodies(mock, "POST", INVOKE_PATH)
    assert body["session_id"] == SESSION and body["message_id"] == 38
    assert body["input"] == {"input_1": {"user_input": "hi"}}


def test_resume_without_input_is_a_usage_error(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run(spec, monkeypatch, [sse(_dialog_wait())], "--json")
    token = result(out)["data"]["resume"]
    code, _, err, mock = _run(spec, monkeypatch, [sse(CLOSE)], "--resume", token)

    assert code == EXIT_USAGE and "--input" in err
    assert mock.calls == []


@pytest.mark.parametrize("token", ["garbage", "r1.e30", "r1.!!!"])
def test_bad_resume_token_is_refused_locally(spec: Path, monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    code, _, _, mock = _run(spec, monkeypatch, [sse(CLOSE)], "--resume", token, "--input", "x")

    assert code == EXIT_USAGE
    assert mock.calls == []


def test_resume_for_another_workflow_is_refused(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run(spec, monkeypatch, [sse(_dialog_wait())], "--json")
    token = result(out)["data"]["resume"]
    other = "0" * 31 + "1"
    mock = PlatformMock().post(INVOKE_PATH, sse(CLOSE))
    code, _, err = run_cli(
        ["flow", "run", str(spec), "--flow-id", other, "--resume", token, "--input", "x"], monkeypatch, mock
    )

    assert code == EXIT_USAGE and FLOW_ID in err
    assert mock.calls == []


def test_resume_against_a_different_binding_says_how_to_fix_it(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, out, _, _ = _run(spec, monkeypatch, [sse(_dialog_wait())], "--json")
    token = result(out)["data"]["resume"]
    from bisheng_cli.flow import binding

    binding.save_binding(spec, "http://platform.test", "0" * 31 + "1")  # the spec is bound elsewhere now
    mock = PlatformMock().post(INVOKE_PATH, sse(CLOSE))
    code, _, err = run_cli(["flow", "run", str(spec), "--resume", token, "--input", "x"], monkeypatch, mock)

    assert code == EXIT_USAGE and mock.calls == []
    assert f"--flow-id {FLOW_ID}" in err


def test_run_needs_the_declaration_to_exist(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = PlatformMock().post(INVOKE_PATH, sse(CLOSE))
    missing = spec.parent / "typo.yaml"
    code, _, err = run_cli(["flow", "run", str(missing), "--flow-id", FLOW_ID], monkeypatch, mock)

    assert code == EXIT_LOCAL_INVALID and "typo.yaml" in err
    assert mock.calls == []


def test_form_needs_json_input(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _, err, mock = _run(spec, monkeypatch, [sse(_form_wait())], "--input", "EB")

    assert code == EXIT_USAGE
    assert "symbol" in err and "period" in err and "--input-json" in err
    assert len(mock.calls) == 1  # the paused round, nothing sent after it

    replies = [sse(_form_wait()), sse(CLOSE)]
    code, _, _, mock = _run(spec, monkeypatch, replies, "--input-json", '{"symbol": "EB", "period": "15m"}')
    assert code == EXIT_OK
    assert bodies(mock, "POST", INVOKE_PATH)[1]["input"] == {"form_1": {"symbol": "EB", "period": "15m"}}


def test_input_file_is_read_as_text(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = spec.parent / "idea.md"
    source.write_text("第一行\n第二行\n", encoding="utf-8")
    _, _, _, mock = _run(spec, monkeypatch, [sse(_dialog_wait()), sse(CLOSE)], "--input-file", str(source))

    assert bodies(mock, "POST", INVOKE_PATH)[1]["input"] == {"input_1": {"user_input": "第一行\n第二行\n"}}


def test_node_failure_reports_node_and_reason(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failed = _event(
        "node_run",
        node_id="llm_1",
        node_name="写代码",
        node_execution_id="e1",
        output_schema={"message": "model 7 is offline"},
    )
    code, out, err, _ = _run(spec, monkeypatch, [sse(failed, _failed_close())], "--json")

    assert code == EXIT_PRECHECK_FAILED
    failure = result(out)["failure"]
    assert failure["code"] == 10541
    assert failure["details"]["node_id"] == "llm_1" and failure["details"]["reason"] == "model 7 is offline"
    assert "llm_1" in err and "写代码" in err and "model 7 is offline" in err
    assert "运行结束" not in err


def test_failure_without_a_named_node_says_so(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _ = _run(spec, monkeypatch, [sse(_failed_close(10540, "busy"))], "--json")

    assert code == EXIT_PRECHECK_FAILED
    assert result(out)["failure"]["details"]["node_id"] is None
    assert "10540" in err and "平台没有给出失败节点" in err


def test_no_tty_does_not_block(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `run_cli` fails the test if anything prompts; interactive=False is the default.
    code, out, _, mock = _run(spec, monkeypatch, [sse(_dialog_wait())], "--json")

    assert code == EXIT_OK
    assert len(mock.calls) == 1
    assert result(out)["data"]["status"] == "waiting_input"


def test_one_shot_input_is_used_once(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [sse(_dialog_wait("38")), sse(_dialog_wait("41"))]
    code, out, _, mock = _run(spec, monkeypatch, replies, "--input", "一次", "--json")

    assert code == EXIT_OK
    assert len(mock.calls) == 2
    data = result(out)["data"]
    assert data["status"] == "waiting_input" and data["message_id"] == "41"


def test_interactive_run_prompts_until_the_end(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [sse(_dialog_wait("38")), sse(_dialog_wait("41")), sse(_output("完成"), CLOSE)]
    code, _, _, mock = _run(spec, monkeypatch, replies, interactive=True, answers=["第一轮", "结束"])

    assert code == EXIT_OK
    sent = [b.get("input") for b in bodies(mock, "POST", INVOKE_PATH)]
    assert sent == [None, {"input_1": {"user_input": "第一轮"}}, {"input_1": {"user_input": "结束"}}]


def test_stream_timeout_names_the_session(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stalls(httpx.SyncByteStream):
        def __iter__(self):
            line = json.dumps({"session_id": SESSION, "data": _output("第一段")}, ensure_ascii=False)
            yield f"data: {line}\n\n".encode()
            raise httpx.ReadTimeout("no event")

    stalled = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stalls())
    code, out, err, _ = _run(spec, monkeypatch, [stalled], "--json", "--wait-timeout", "30")

    assert code == EXIT_WAIT_TIMEOUT
    assert result(out)["failure"]["details"]["session_id"] == SESSION
    assert "30" in err and SESSION in err and "不可达" not in err


def test_platform_refusal_before_the_stream(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, _, err, _ = _run(spec, monkeypatch, [v2_error(10543, "Invalid user input", http_status=400)])

    assert code == EXIT_LOCAL_INVALID
    assert "10543" in err and "--resume" in err


def test_input_with_no_pause_is_reported_unused(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _ = _run(spec, monkeypatch, [sse(_output("完成"), CLOSE)], "--input", "多余", "--json")

    assert code == EXIT_OK
    assert "未使用" in err
    assert result(out)["data"]["input_used"] is False


def test_round_without_close_or_pause_is_not_finished(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The platform sends ``close`` only on success; a stream that just stops has not finished."""
    done = _event("stream_msg", status="end", node_id="llm_1", output_schema={"message": "半截"})
    code, out, err, _ = _run(spec, monkeypatch, [sse(done)], "--json")

    assert code == EXIT_DEFECT
    assert result(out)["ok"] is False
    assert "运行完成" not in err


def test_envelope_without_events_is_not_finished(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.helpers.platform_mock import v2_ok

    code, out, err, _ = _run(spec, monkeypatch, [v2_ok({"session_id": SESSION})], "--json")

    assert code == EXIT_DEFECT
    assert result(out)["ok"] is False
    assert "运行完成" not in err


def test_pause_without_session_issues_no_unusable_token(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _ = _run(spec, monkeypatch, [sse(_dialog_wait(), session_id="")], "--json")

    assert code == EXIT_DEFECT
    assert "--resume" not in err
    assert "resume" not in (result(out)["failure"]["details"] or {})


def test_user_stop_is_not_a_node_failure(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = _event("node_run", node_id="llm_1", node_name="写代码", output_schema={"message": "stop by user"})
    code, out, err, _ = _run(spec, monkeypatch, [sse(stopped)], "--json")

    assert code == EXIT_CANCELLED
    assert result(out)["ok"] is False
    assert result(out)["failure"]["details"]["node_id"] == "llm_1"
    assert "出错" not in err and "停止" in err
