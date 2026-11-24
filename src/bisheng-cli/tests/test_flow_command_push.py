"""T034 — ``bisheng flow push``.

Push is the one flow command that writes, so most of this file is about what it
must refuse or must not claim:

* it adds a version and never rewrites one (AC-25) — no PUT ever leaves;
* content only the platform has is shown and needs an explicit yes, and with
  nobody at the keyboard "no answer" is a refusal, not a yes (AC-26);
* a live workflow gets the version but not the switch, and the run says so
  instead of "pushed" (AC-27);
* a missing permission bit is named (AC-28);
* success names the new version (AC-29).

"Only the platform has it" is judged against the last definition this machine
pushed when there is one: a value that differs online only because the YAML
moved on since the last push is not somebody's web edit, and asking about it on
every push would train everyone to pass the flag blindly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bisheng_cli.errors import EXIT_DEFECT, EXIT_FORBIDDEN, EXIT_OK, EXIT_PRECHECK_FAILED, EXIT_USAGE
from tests.helpers.flow_command import (
    BASE,
    FLOW_ID,
    FLOW_PATH,
    FLOWS_PATH,
    VERSIONS_PATH,
    bodies,
    flow_row,
    local_definition,
    login,
    result,
    run_cli,
    serve_catalogue,
    write_project,
)
from tests.helpers.flow_fixtures import find_param
from tests.helpers.platform_mock import PlatformMock, v2_error, v2_ok

NEW_VERSION = 42


@pytest.fixture
def spec(home_dir, tmp_path: Path) -> Path:
    login()
    return write_project(tmp_path / "proj")


def _without(definition: dict, node_id: str) -> dict:
    """``definition`` minus one node and its edges — so the local side only *adds*."""
    out = copy.deepcopy(definition)
    out["nodes"] = [n for n in out["nodes"] if n["id"] != node_id]
    out["edges"] = [e for e in out["edges"] if node_id not in (e["source"], e["target"])]
    return out


def _with_web_node(definition: dict) -> dict:
    out = copy.deepcopy(definition)
    extra = copy.deepcopy(next(n for n in out["nodes"] if n["id"] == "llm_1"))
    extra["id"] = extra["data"]["id"] = "llm_web"
    out["nodes"].append(extra)
    return out


def _with_prompt(definition: dict, prompt: str) -> dict:
    out = copy.deepcopy(definition)
    find_param(next(n for n in out["nodes"] if n["id"] == "llm_1"), "system_prompt")["value"] = prompt
    return out


def _echo_version(request) -> object:
    """The platform's create-version reply: the new row, under the name it was sent."""
    return v2_ok({"id": NEW_VERSION, "name": json.loads(request.content)["name"], "flow_id": FLOW_ID})


def _platform(online: dict, *, activate=None, version_reply=None) -> PlatformMock:
    mock = serve_catalogue(PlatformMock()).get(FLOW_PATH, flow_row(online))
    mock.post(VERSIONS_PATH, version_reply or _echo_version)
    mock.post(f"{VERSIONS_PATH}/{NEW_VERSION}/activate", activate or v2_ok(None))
    return mock


def _push(spec: Path, monkeypatch, mock, *extra: str, **kw) -> tuple[int, dict, str]:
    code, out, err = run_cli(
        ["flow", "push", str(spec), "--flow-id", FLOW_ID, "--json", *extra], monkeypatch, mock, **kw
    )
    return code, result(out), err


def test_push_creates_new_version(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_without(local_definition(), "tool_1"))
    code, final, _ = _push(spec, monkeypatch, mock, "--name", "第二版")

    assert code == EXIT_OK, final
    (body,) = bodies(mock, "POST", VERSIONS_PATH)
    assert body["data"] == local_definition()
    assert body["name"] == "第二版"


def test_push_activates_new_version(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_without(local_definition(), "tool_1"))
    code, final, _ = _push(spec, monkeypatch, mock)

    assert code == EXIT_OK
    assert f"{VERSIONS_PATH}/{NEW_VERSION}/activate" in mock.paths_called()
    assert final["data"]["activated"] is True


def test_existing_versions_untouched(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_without(local_definition(), "tool_1"))
    _push(spec, monkeypatch, mock)

    assert [r.method for r in mock.calls if r.method not in ("GET", "POST")] == []
    writes = [r.url.path for r in mock.calls if r.method == "POST"]
    assert writes == [VERSIONS_PATH, f"{VERSIONS_PATH}/{NEW_VERSION}/activate"]


def test_online_only_content_refused_without_tty(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_with_web_node(local_definition()))
    code, final, err = _push(spec, monkeypatch, mock)

    assert code == EXIT_USAGE
    assert "--allow-drop-online-only" in err
    assert "llm_web" in err  # what would be left behind is shown before refusing
    assert [(i["kind"], i["id"]) for i in final["failure"]["details"]["online_only"]] == [("node", "llm_web")]
    assert bodies(mock, "POST", VERSIONS_PATH) == []


def test_online_only_content_allowed_with_flag(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_with_web_node(local_definition()))
    code, final, err = _push(spec, monkeypatch, mock, "--allow-drop-online-only")

    assert code == EXIT_OK
    assert "llm_web" in err  # still shown, even when pre-approved
    assert final["data"]["online_only_dropped"] == 1
    assert final["data"]["online_only_confirmation"] == "confirmed"
    assert len(bodies(mock, "POST", VERSIONS_PATH)) == 1


@pytest.mark.parametrize(("answer", "pushed"), [("y", True), ("n", False)])
def test_online_only_content_requires_confirmation_with_tty(
    spec: Path, monkeypatch: pytest.MonkeyPatch, answer: str, pushed: bool
) -> None:
    mock = _platform(_with_web_node(local_definition()))
    code, _, err = _push(spec, monkeypatch, mock, interactive=True, answers=[answer])

    assert "llm_web" in err
    assert (code == EXIT_OK) is pushed
    assert len(bodies(mock, "POST", VERSIONS_PATH)) == (1 if pushed else 0)
    if not pushed:
        assert code == EXIT_USAGE


def test_last_pushed_definition_is_the_baseline_for_online_only(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # First push from this machine: the YAML prompt replaces an older online one.
    # With no record of an earlier push, that older value counts as online-only.
    older = _with_prompt(local_definition(), "旧提示词")
    code, _, _ = _push(spec, monkeypatch, _platform(older), "--allow-drop-online-only")
    assert code == EXIT_OK

    # The YAML moves on; online still holds exactly what was pushed last time.
    spec.parent.joinpath("prompts", "system.md").write_text("新提示词", encoding="utf-8")
    code, final, err = _push(spec, monkeypatch, _platform(local_definition()))
    assert code == EXIT_OK, final  # nothing was edited on the web, so nothing to confirm
    # ...but the result still says what the new version replaced, same as the human text.
    assert final["data"]["online_only_dropped"] == 1
    assert final["data"]["online_only_confirmation"] == "unchanged_since_last_push"
    assert "1 处" in err

    # Now somebody edits the prompt on the web after that push: confirmation is back.
    edited = _with_prompt(_with_prompt(local_definition(), "新提示词"), "网页改的")
    code, final, _ = _push(spec, monkeypatch, _platform(edited))
    assert code == EXIT_USAGE
    assert [i["kind"] for i in final["failure"]["details"]["online_only"]] == ["param"]


def test_last_push_record_stays_out_of_git(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _push(spec, monkeypatch, _platform(_without(local_definition(), "tool_1")))

    cache = spec.parent / ".bisheng" / "cache"
    assert (cache / ".gitignore").read_text("utf-8").strip() == "*"
    assert any(p.name != ".gitignore" for p in cache.iterdir())
    flow_json = (spec.parent / ".bisheng" / "flow.json").read_text("utf-8")
    assert "nodes" not in flow_json  # the binding holds an id, never a definition


def test_online_workflow_reports_offline_hint(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = v2_error(10525, "Workflow is live and not editable", http_status=200)
    mock = _platform(_without(local_definition(), "tool_1"), activate=live)
    code, final, err = _push(spec, monkeypatch, mock)

    assert code == EXIT_PRECHECK_FAILED
    assert final["ok"] is False and final["failure"]["code"] == 10525
    assert str(NEW_VERSION) in err and "下线" in err
    assert "推送成功" not in err and "已切为当前版本" not in err
    assert final["failure"]["details"]["version_id"] == NEW_VERSION


def test_missing_permission_reports_which_scope(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    denied = v2_error(26003, "scope missing", required="workflow:write")
    mock = _platform(_without(local_definition(), "tool_1"), version_reply=denied)
    code, final, err = _push(spec, monkeypatch, mock)

    assert code == EXIT_FORBIDDEN
    assert "workflow:write" in err
    assert final["ok"] is False
    assert f"{VERSIONS_PATH}/{NEW_VERSION}/activate" not in mock.paths_called()


def test_reports_new_version_id_and_name(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_without(local_definition(), "tool_1"))
    code, final, err = _push(spec, monkeypatch, mock)

    assert code == EXIT_OK
    name = bodies(mock, "POST", VERSIONS_PATH)[0]["name"]
    assert name.startswith("cli-")
    assert final["data"]["version_id"] == NEW_VERSION and final["data"]["version_name"] == name
    assert str(NEW_VERSION) in err and name in err


def test_no_activate_only_creates_version(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(_without(local_definition(), "tool_1"))
    code, final, _ = _push(spec, monkeypatch, mock, "--no-activate")

    assert code == EXIT_OK
    assert final["data"]["activated"] is False
    assert not [p for p in mock.paths_called() if p.endswith("/activate")]


def test_identical_definition_creates_nothing(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = _platform(local_definition())
    code, final, err = _push(spec, monkeypatch, mock)

    assert code == EXIT_OK
    assert final["data"]["pushed"] is False
    assert [r.method for r in mock.calls] == ["GET", "GET", "GET"]
    assert "一致" in err


def test_create_makes_the_workflow_and_binds_it(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).post(
        FLOWS_PATH, v2_ok({"id": FLOW_ID, "name": "研究", "version_id": 7, "data": local_definition()})
    )
    code, out, err = run_cli(["flow", "push", str(spec), "--create", "--json"], monkeypatch, mock)

    assert code == EXIT_OK
    (body,) = bodies(mock, "POST", FLOWS_PATH)
    assert body == {"name": "研究", "description": "人工驱动的研究循环", "data": local_definition()}
    final = result(out)
    assert final["data"]["flow_id"] == FLOW_ID and final["data"]["version_id"] == 7
    store = json.loads((spec.parent / ".bisheng" / "flow.json").read_text("utf-8"))
    assert store["flows"][BASE][spec.name]["flow_id"] == FLOW_ID
    assert FLOW_ID in err


def test_create_refuses_when_already_bound(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _push(spec, monkeypatch, _platform(_without(local_definition(), "tool_1")))  # binds FLOW_ID
    mock = PlatformMock()
    code, _, err = run_cli(["flow", "push", str(spec), "--create"], monkeypatch, mock)

    assert code == EXIT_USAGE
    assert FLOW_ID in err
    assert mock.calls == []


def test_create_and_flow_id_are_exclusive(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_cli(["flow", "push", str(spec), "--create", "--flow-id", FLOW_ID], monkeypatch, PlatformMock())


def test_corrupted_last_push_record_falls_back_to_asking(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    older = _with_prompt(local_definition(), "旧提示词")
    _push(spec, monkeypatch, _platform(older), "--allow-drop-online-only")
    record = spec.parent / ".bisheng" / "cache" / "last_push.json"
    store = json.loads(record.read_text("utf-8"))
    (entry,) = store["entries"].values()
    entry["definition"]["nodes"][0]["data"]["name"] = "hand-edited"  # no longer matches its checksum
    record.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")

    spec.parent.joinpath("prompts", "system.md").write_text("新提示词", encoding="utf-8")
    code, _, _ = _push(spec, monkeypatch, _platform(local_definition()))
    assert code == EXIT_USAGE  # an untrustworthy record is no record: ask again


def test_default_version_names_do_not_collide(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names = set()
    for _ in range(5):
        mock = _platform(_without(local_definition(), "tool_1"))
        _push(spec, monkeypatch, mock, "--no-activate")
        names.add(bodies(mock, "POST", VERSIONS_PATH)[0]["name"])
    assert len(names) == 5


def test_binding_file_is_replaced_atomically(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    replaced: list[str] = []
    real = os.replace

    def spy(src, dst):
        replaced.append(str(dst))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    _push(spec, monkeypatch, _platform(_without(local_definition(), "tool_1")))
    assert str(spec.parent / ".bisheng" / "flow.json") in replaced


def test_create_reports_the_workflow_when_the_binding_cannot_be_written(
    spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bisheng_cli.flow import binding

    def refuse(*_a, **_k):
        raise PermissionError("read-only checkout")

    monkeypatch.setattr(binding, "_replace", refuse)
    mock = serve_catalogue(PlatformMock()).post(
        FLOWS_PATH, v2_ok({"id": FLOW_ID, "name": "研究", "version_id": 7, "data": local_definition()})
    )
    code, out, err = run_cli(["flow", "push", str(spec), "--create", "--json"], monkeypatch, mock)

    assert code == EXIT_OK  # the workflow exists on the platform: that is the outcome to report
    final = result(out)
    assert final["data"]["flow_id"] == FLOW_ID and final["data"]["binding_saved"] is False
    assert FLOW_ID in err and "--flow-id" in err  # how to reach it next time without a binding


def test_create_reply_without_version_is_not_success(spec: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mock = serve_catalogue(PlatformMock()).post(FLOWS_PATH, v2_ok({"id": FLOW_ID, "name": "研究"}))
    code, out, err = run_cli(["flow", "push", str(spec), "--create", "--json"], monkeypatch, mock)

    assert code == EXIT_DEFECT
    assert result(out)["ok"] is False
    assert FLOW_ID in err  # the workflow may exist; say which one to check
