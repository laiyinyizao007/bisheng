# ruff: noqa: RUF001, RUF002, RUF003 -- Chinese test copy with full-width punctuation
"""F007 AC-23: the platform runs the very same declaration rules as the ``bisheng`` CLI.

The rules live once in ``src/backend/bisheng_flow_dsl`` (design decision 1). The CLI side is
pinned by ``src/bisheng-cli/tests/test_dsl_boundary.py``; this file pins the platform side:
the backend imports that same directory, and the F006 examples compile and validate here
exactly as they do in the CLI test suite.
"""

from pathlib import Path

import pytest
import yaml

import bisheng_flow_dsl
from bisheng.workflow.common.node_templates import NODE_TEMPLATES
from bisheng_flow_dsl.compiler import compile_spec
from bisheng_flow_dsl.errors import DslError
from bisheng_flow_dsl.validate import validate

BACKEND_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = BACKEND_ROOT / "bisheng/dev_toolkit/skills/workflow-as-code/example"


def _load_example(path: Path) -> dict:
    """Test-only loader: resolves ``!include`` relative to the example.

    The platform itself must never do this (design decision 6: on a server
    ``!include`` is an arbitrary file read); it is here only to load the
    examples the CLI loads.
    """

    class _Loader(yaml.SafeLoader):
        pass

    def _include(loader: yaml.SafeLoader, node: yaml.Node) -> str:
        return (path.parent / loader.construct_scalar(node)).read_bytes().decode("utf-8")

    _Loader.add_constructor("!include", _include)
    return yaml.load(path.read_bytes().decode("utf-8"), Loader=_Loader)  # noqa: S506 -- SafeLoader subclass


def test_backend_imports_the_shared_package_directory():
    assert Path(bisheng_flow_dsl.__file__).resolve().parent == (BACKEND_ROOT / "bisheng_flow_dsl").resolve()


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.yaml")), ids=lambda p: p.name)
def test_f006_examples_compile_and_validate_on_the_platform(example: Path):
    definition = compile_spec(_load_example(example), NODE_TEMPLATES, [])
    assert validate(definition, NODE_TEMPLATES, []) == []


def test_a_broken_example_gets_the_same_issue_code_as_in_the_cli(tmp_path: Path):
    source = (EXAMPLES / "flow.yaml").read_bytes().decode("utf-8")
    assert source.count("{{#analyst.output#}}") >= 1
    broken = tmp_path / "flow.yaml"
    broken.write_bytes(source.replace("{{#analyst.output#}}", "{{#analist.output#}}").encode("utf-8"))
    (tmp_path / "prompts").mkdir()
    for prompt in (EXAMPLES / "prompts").iterdir():
        (tmp_path / "prompts" / prompt.name).write_bytes(prompt.read_bytes())
    issues = validate(compile_spec(_load_example(broken), NODE_TEMPLATES, []), NODE_TEMPLATES, [])
    assert "reference_unknown_node" in {issue["code"] for issue in issues}


def test_platform_safe_load_refuses_include():
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.safe_load("system_prompt: !include /etc/passwd")


def test_compile_problems_surface_as_dsl_error_not_cli_error():
    with pytest.raises(DslError) as excinfo:
        compile_spec({"name": "x", "nodes": [{"id": "a", "type": "nope"}], "edges": []}, NODE_TEMPLATES, [])
    assert excinfo.value.kind == "invalid"
    assert excinfo.value.reason == "compile_issues"
