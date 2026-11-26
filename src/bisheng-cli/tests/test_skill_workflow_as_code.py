"""The 「工作流声明」 skill pack, checked against the compiler, validator and node templates.

The pack (``src/backend/bisheng/dev_toolkit/skills/workflow-as-code/``) is what an AI
coding tool reads before writing a ``flow.yaml``. Its value is that it is *right*: a
parameter key the templates dropped, a comparison operator the engine stopped
accepting, or an example that no longer compiles turns the pack into confident,
wrong instructions — worse than none. So every machine-checkable claim it makes is
pinned here (F006 AC-10 … AC-12):

* each ``<!-- params:<type> -->`` table lists **exactly** the template's parameter
  keys for that node type, and there is one table per template type;
* the 「编译必查」 column marks exactly the parameters the validator refuses empty;
* each ``<!-- tabs:<type> -->`` table lists exactly the template's tab options;
* the field tables equal the compiler's accepted key sets, and the operator / logic
  tables equal the validator's and compiler's sets;
* ``example/flow.yaml`` compiles and validates with zero issues against the
  platform's own templates, and a broken copy is reported.

Templates are read from the backend source with ``ast`` (no backend import), same as
``test_platform_contract.py``; a missing backend tree fails instead of skipping.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from bisheng_cli.commands.skills import DEFAULT_PACKS
from bisheng_cli.flow.dsl import compile_spec
from bisheng_cli.flow.spec import load_spec
from bisheng_flow_dsl import compiler, validate

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "src/backend/bisheng/dev_toolkit/skills/workflow-as-code"
SKILL_MD = PACK_DIR / "SKILL.md"
EXAMPLE = PACK_DIR / "example/flow.yaml"
NODE_TEMPLATES_SRC = REPO_ROOT / "src/backend/bisheng/workflow/common/node_templates.py"
ENGINE_CONDITION_SRC = REPO_ROOT / "src/backend/bisheng/workflow/common/condition.py"

_MARKER = re.compile(r"<!--\s*([a-z_]+):?([a-z_]*)\s*-->")
_FIRST_CELL = re.compile(r"^\|\s*`([^`]+)`\s*\|")


def _templates() -> list[dict[str, Any]]:
    tree = ast.parse(NODE_TEMPLATES_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "NODE_TEMPLATES":
            return ast.literal_eval(node.value)
    pytest.fail(f"NODE_TEMPLATES not found in {NODE_TEMPLATES_SRC}")


def _tables() -> dict[tuple[str, str], list[list[str]]]:
    """``(kind, name)`` → rows (cells) of the table right after each ``<!-- kind:name -->``."""
    lines = SKILL_MD.read_text(encoding="utf-8").splitlines()
    tables: dict[tuple[str, str], list[list[str]]] = {}
    for index, line in enumerate(lines):
        match = _MARKER.fullmatch(line.strip())
        if not match:
            continue
        key = (match.group(1), match.group(2))
        assert key not in tables, f"marker {line.strip()} appears twice"
        body = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith("|"):
            body.append(lines[cursor])
            cursor += 1
        assert len(body) >= 2, f"marker {line.strip()} is not followed by a table"
        # Drop the header and the |---| separator.
        tables[key] = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in body[2:]]
    return tables


def _keys(rows: list[list[str]]) -> list[str]:
    keys = []
    for row in rows:
        match = _FIRST_CELL.match("| " + row[0] + " |")
        assert match, f"first cell must be a `code` key: {row}"
        keys.append(match.group(1))
    return keys


def _template_params(template: dict[str, Any]) -> list[str]:
    return [p["key"] for group in template.get("group_params") or [] for p in group.get("params") or []]


TEMPLATES = _templates()
TABLES = _tables()


def test_one_param_table_per_template_type() -> None:
    documented = {name for kind, name in TABLES if kind == "params"}
    assert documented == {t["type"] for t in TEMPLATES}
    assert len(TEMPLATES) == 12


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["type"])
def test_param_table_matches_template_keys(template: dict[str, Any]) -> None:
    rows = TABLES[("params", template["type"])]
    documented = _keys(rows)
    expected = _template_params(template)
    assert len(documented) == len(set(documented)), f"{template['type']}: duplicate rows"
    missing = sorted(set(expected) - set(documented))
    invented = sorted(set(documented) - set(expected))
    assert not missing and not invented, (
        f"node {template['type']}: keys in the template but not in SKILL.md {missing}; "
        f"keys in SKILL.md but not in the template {invented}"
    )


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["type"])
def test_must_fill_column_matches_validator(template: dict[str, Any]) -> None:
    rows = TABLES[("params", template["type"])]
    marked = {key for key, row in zip(_keys(rows), rows, strict=True) if "是" in row[1]}
    assert marked == set(validate.NON_EMPTY_PARAMS.get(template["type"], ())), template["type"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["type"])
def test_required_params_are_documented(template: dict[str, Any]) -> None:
    documented = set(_keys(TABLES[("params", template["type"])]))
    for required in validate.REQUIRED_PARAMS.get(template["type"], ()):
        alternatives = required if isinstance(required, tuple) else (required,)
        assert documented & set(alternatives), f"{template['type']}: none of {alternatives} documented"


def test_tab_tables_match_template_options() -> None:
    with_tabs = {
        t["type"]: [o["key"] for o in t["tab"]["options"]] for t in TEMPLATES if isinstance(t.get("tab"), dict)
    }
    documented = {name: _keys(rows) for (kind, name), rows in TABLES.items() if kind == "tabs"}
    assert documented == with_tabs


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("top", compiler.TOP_LEVEL_KEYS),
        ("node", compiler.NODE_KEYS),
        ("edge", compiler.EDGE_KEYS),
        ("case", compiler.CASE_KEYS),
        ("condition", compiler.CONDITION_KEYS),
    ],
)
def test_field_tables_match_compiler(marker: str, expected: frozenset[str]) -> None:
    assert set(_keys(TABLES[("keys", marker)])) == set(expected)


def test_operator_and_logic_tables_match_engine() -> None:
    assert set(_keys(TABLES[("values", "op")])) == set(validate.COMPARISON_OPERATORS)
    assert tuple(_keys(TABLES[("values", "logic")])) == compiler._LOGICS


def _check(spec: dict[str, Any]) -> list[dict[str, Any]]:
    definition = compile_spec(spec, TEMPLATES, [])
    return validate.validate(definition, TEMPLATES, [])


EXAMPLES = sorted(PACK_DIR.glob("example/*.yaml"))


def test_examples_exist() -> None:
    assert EXAMPLE in EXAMPLES and len(EXAMPLES) >= 2


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example_compiles_and_validates_clean(example: Path) -> None:
    assert _check(load_spec(example)) == []


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.name)
def test_example_model_ids_are_numbers(example: Path) -> None:
    # The web editor saves model_id as a number; a quoted "1" would make every diff
    # against a web-edited flow report a change the user never made.
    for node in load_spec(example)["nodes"]:
        if "model_id" in (node.get("params") or {}):
            assert isinstance(node["params"]["model_id"], int), node["id"]


def test_broken_example_is_reported(tmp_path: Path) -> None:
    broken = tmp_path / "flow.yaml"
    broken.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("{{#analyst.output#}}", "{{#analist.output#}}"), encoding="utf-8"
    )
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/analyst.md").write_text("x", encoding="utf-8")
    issues = _check(load_spec(broken))
    assert [(i["code"], i.get("node"), i.get("reference")) for i in issues] == [
        ("reference_unknown_node", "reply", "{{#analist.output#}}")
    ]


def test_cli_syncs_exactly_the_packs_the_platform_ships() -> None:
    # F006 AC-13: a pack on disk the CLI does not sync never reaches anyone's agent;
    # a pack the CLI syncs that the platform lacks is a guaranteed skip warning.
    shipped = {path.parent.name for path in PACK_DIR.parent.glob("*/SKILL.md")}
    assert set(DEFAULT_PACKS) == shipped


def test_default_tab_row_matches_template_default() -> None:
    # An AI that writes no `tab` gets the template's default; the row the skill calls
    # 「默认」 must be that one, or every flow it writes silently runs in the other mode.
    for template in TEMPLATES:
        if not isinstance(template.get("tab"), dict):
            continue
        rows = TABLES[("tabs", template["type"])]
        defaults = [key for key, row in zip(_keys(rows), rows, strict=True) if "默认" in row[1]]
        assert defaults == [template["tab"]["value"]], template["type"]


def _engine_constants(class_name: str) -> set[str]:
    tree = ast.parse(ENGINE_CONDITION_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant)
            }
    pytest.fail(f"{class_name} not found in {ENGINE_CONDITION_SRC}")


def test_operator_and_logic_tables_match_the_engine_source() -> None:
    # Pinned to the engine itself, not only to the CLI's copy of it: an operator the
    # engine adds or drops must show up here even if the CLI has not caught up.
    assert set(_keys(TABLES[("values", "op")])) == _engine_constants("ComparisonType")
    assert set(_keys(TABLES[("values", "logic")])) == _engine_constants("LogicType")
