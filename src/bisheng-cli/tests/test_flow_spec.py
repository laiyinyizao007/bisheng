"""F005 T018 — loading a workflow declaration (``flow.yaml``).

The loader is the boundary AC-10 draws: a declaration is *data*. Reading one must
never execute anything in it, so the file goes through a ``SafeLoader`` subclass
whose only addition is ``!include`` (read a text file, return its content).

Local quick checks only (AC-11): parseable, top level is a mapping, required
top-level keys present. Node types, wiring and references are judged later by
the compiler / validator — the loader fills no defaults and rules on nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError
from bisheng_cli.flow.spec import REQUIRED_TOP_LEVEL, load_spec

MINIMAL = "name: wf\nnodes:\n  - id: start_1\n    type: start\nedges: []\n"


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_declaration_loads_verbatim(tmp_path: Path) -> None:
    spec = load_spec(write(tmp_path, "flow.yaml", MINIMAL))
    assert spec == {"name": "wf", "nodes": [{"id": "start_1", "type": "start"}], "edges": []}


def test_python_object_tag_is_rejected(tmp_path: Path) -> None:
    """``!!python/object`` would make reading the file run code — refuse, say why, run nothing."""
    # ``os.mkdir`` is a module-level callable, so an unsafe loader really runs it —
    # a payload that even ``unsafe_load`` cannot resolve would prove nothing.
    marker = tmp_path / "pwned"
    payload = f"name: wf\nnodes: []\nx: !!python/object/apply:os.mkdir ['{marker.as_posix()}']\n"
    with pytest.raises(CliError) as excinfo:
        load_spec(write(tmp_path, "flow.yaml", payload))
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    assert "安全" in excinfo.value.message
    assert not marker.exists(), "the declaration was executed"


def test_include_expands_relative_to_spec_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``!include`` resolves against the declaration's directory, not the CWD."""
    project = tmp_path / "project"
    write(project, "prompts/system.md", "你是研究助手。\n第二行")
    spec_path = write(
        project,
        "flow.yaml",
        "name: wf\nnodes:\n  - id: llm_1\n    type: llm\n    params:\n      system_prompt: !include prompts/system.md\n",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    spec = load_spec(spec_path)

    assert spec["nodes"][0]["params"]["system_prompt"] == "你是研究助手。\n第二行"


def test_included_text_is_not_parsed_or_evaluated(tmp_path: Path) -> None:
    """The included file is text, full stop — YAML/templating inside it stays literal."""
    write(tmp_path, "p.md", "a: !!python/object/apply:os.system ['echo hi']\n{{#x.y#}}")
    spec = load_spec(write(tmp_path, "flow.yaml", "name: wf\nnodes: []\nprompt: !include p.md\n"))
    assert spec["prompt"] == "a: !!python/object/apply:os.system ['echo hi']\n{{#x.y#}}"


@pytest.mark.parametrize("missing", REQUIRED_TOP_LEVEL)
def test_missing_top_level_field_rejected_without_request(tmp_path: Path, missing: str) -> None:
    """AC-11: refused locally, naming the missing key. ``no_network`` (autouse) fails any request."""
    fields = {"name": "name: wf\n", "nodes": "nodes: []\n"}
    text = "".join(value for key, value in fields.items() if key != missing)
    with pytest.raises(CliError) as excinfo:
        load_spec(write(tmp_path, "flow.yaml", text))
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    assert missing in excinfo.value.message
    assert missing in excinfo.value.details["missing"]


def test_all_missing_fields_reported_together(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        load_spec(write(tmp_path, "flow.yaml", "description: only\n"))
    assert sorted(excinfo.value.details["missing"]) == sorted(REQUIRED_TOP_LEVEL)


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n", "42\n", ""])
def test_non_mapping_top_level_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(CliError) as excinfo:
        load_spec(write(tmp_path, "flow.yaml", text))
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    assert "映射" in excinfo.value.message


@pytest.mark.parametrize(("key", "text"), [("nodes", "name: wf\nnodes: {}\n"), ("edges", MINIMAL.replace("[]", "x"))])
def test_list_shaped_keys_must_be_lists(tmp_path: Path, key: str, text: str) -> None:
    with pytest.raises(CliError) as excinfo:
        load_spec(write(tmp_path, "flow.yaml", text))
    assert key in excinfo.value.message


def test_missing_include_file_reports_path(tmp_path: Path) -> None:
    spec_path = write(tmp_path, "flow.yaml", "name: wf\nnodes: []\np: !include prompts/nope.md\n")
    with pytest.raises(CliError) as excinfo:
        load_spec(spec_path)
    resolved = str((tmp_path / "prompts" / "nope.md").resolve())
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID
    assert "prompts/nope.md" in excinfo.value.message
    assert excinfo.value.details["resolved_path"] == resolved


def test_missing_declaration_file_is_local_error(tmp_path: Path) -> None:
    with pytest.raises(CliError) as excinfo:
        load_spec(tmp_path / "absent.yaml")
    assert excinfo.value.exit_code == EXIT_LOCAL_INVALID


def test_include_on_non_scalar_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CliError):
        load_spec(write(tmp_path, "flow.yaml", "name: wf\nnodes: []\np: !include [a, b]\n"))
