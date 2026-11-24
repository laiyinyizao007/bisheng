"""F007 design decision 1: the shared declaration rules stay neutral, the CLI stays decoupled.

``bisheng_flow_dsl`` lives under src/backend/ and is imported by the platform AND bundled into
this wheel. The CLI test run puts ../backend on sys.path (pyproject pythonpath) so it always
tests the live rules -- which also makes the platform's own ``bisheng`` package importable here.
These checks keep that from turning into coupling the wheel smoke test would only catch later.
"""

import ast
from pathlib import Path

import pytest

CLI_ROOT = Path(__file__).resolve().parents[1]
DSL_ROOT = CLI_ROOT.parent / "backend" / "bisheng_flow_dsl"

#: What each tree may never import (top-level module names).
FORBIDDEN = {
    DSL_ROOT: {"bisheng", "bisheng_cli", "bisheng_langchain"},
    CLI_ROOT / "bisheng_cli": {"bisheng", "bisheng_langchain"},
}


def _imported_roots(path: Path) -> set[str]:
    roots = set()
    for node in ast.walk(ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_neutral_package_exists_with_its_modules():
    for name in ("__init__", "errors", "compiler", "validate", "diff"):
        assert (DSL_ROOT / f"{name}.py").is_file(), name


@pytest.mark.parametrize("tree", sorted(FORBIDDEN, key=str), ids=lambda p: p.name)
def test_no_forbidden_imports(tree: Path):
    files = sorted(tree.rglob("*.py"))
    assert files, tree
    offenders = {
        str(path.relative_to(tree)): sorted(_imported_roots(path) & FORBIDDEN[tree])
        for path in files
        if _imported_roots(path) & FORBIDDEN[tree]
    }
    assert offenders == {}


def test_the_cli_tests_run_the_live_rules():
    import bisheng_flow_dsl

    assert Path(bisheng_flow_dsl.__file__).resolve().parent == DSL_ROOT.resolve()
