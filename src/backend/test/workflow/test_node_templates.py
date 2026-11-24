# ruff: noqa: RUF001, RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""F005 T001 — 工作流节点模板的黄金对照。

模板从前端的 ``workflowTemplate`` 常量搬到后端之后，"节点模板只有一份真相"这句话
需要有人守着。这个文件把迁移当时的模板冻结成一份黄金快照
（``fixtures/node_templates_golden.json``），任何对模板的改动都必须让本文件变红——
除非改动是有意的，那就在本文件里更新黄金快照并说明为什么。

三道检查：

1. 后端模块与黄金快照**逐字相等**（含 ``value`` 默认值——前端建新节点时要用它）；
2. 12 类节点齐全，且每个分组名都是前端 locale 里**已经存在的 i18n 键**——后端只
   发布键，文案仍归前端资源（AC-03）；
3. 参数级 ``label`` / ``description`` / ``placeholder`` / ``help`` 一律是 ``"true"``
   或空串，不含界面文案（AC-03）。

另有一道对照：前端源码还在时（迁移之前的前端常量尚未删除），本文件与前端字面量
逐字比对；前端删掉那份常量、并改为请求后端接口之后（T042），这条用例退场——
那时后端就是唯一真相源，黄金快照继续守着它。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bisheng.workflow.common.node_templates import NODE_TEMPLATES, get_tool_node_template

TEST_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = TEST_DIR / "fixtures" / "node_templates_golden.json"

#: 仓库根（src/backend/test/workflow/ -> 上溯四级）
REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_TEMPLATE = REPO_ROOT / "src" / "frontend" / "platform" / "src" / "controllers" / "API" / "workflow.ts"
#: The endpoint the frontend fetches templates from once its literal is gone (T042).
FRONTEND_ENDPOINT = "/api/v1/workflow/node_templates"
LOCALE_DIR = REPO_ROOT / "src" / "frontend" / "platform" / "public" / "locales"
LOCALES = ("zh-Hans", "en-US", "ja")

#: 12 类节点，一类不多一类不少。
EXPECTED_TYPES = [
    "start",
    "input",
    "output",
    "llm",
    "agent",
    "qa_retriever",
    "rag",
    "knowledge_retriever",
    "report",
    "code",
    "condition",
    "end",
]


def _golden() -> list[dict]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _locale_nodes(locale: str) -> dict:
    data = json.loads((LOCALE_DIR / locale / "flow.json").read_text(encoding="utf-8"))
    return data["node"]


def _frontend_literal() -> list[dict] | None:
    """解析前端的 ``workflowTemplate`` 字面量；T042 删掉它之后返回 None。

    **"退场"必须有正面证据**：常量不在了，同一文件里的 ``getWorkflowNodeTemplate``
    必须改为请求后端接口——否则常量只是被改名、移动或删坏了，这里必须红，
    而不是静默失去 live 对照。文件本身不在也算红（那个函数住在这个文件里）。
    """
    assert FRONTEND_TEMPLATE.exists(), f"前端模板接口文件不见了：{FRONTEND_TEMPLATE}"
    source = FRONTEND_TEMPLATE.read_text(encoding="utf-8")
    if not _has_template_copy(source):
        assert _fetches_from_backend(source), (
            "前端 workflowTemplate 常量不见了，但 getWorkflowNodeTemplate 也没改为请求"
            f" {FRONTEND_ENDPOINT}——这条 live 对照不允许静默退场。"
        )
        return None
    return _parse_frontend_literal(source)


#: A template literal in any spelling: the old constant name, or object keys named
#: ``group_params`` in any quote style (code that *reads* nodes uses ``.group_params``).
_TEMPLATE_COPY = re.compile(r"\bworkflowTemplate\s*=|(?:['\"`]group_params['\"`]|[{,]\s*group_params)\s*:")


def _has_template_copy(source: str) -> bool:
    return _TEMPLATE_COPY.search(source) is not None


def _function_body(source: str, name: str) -> str:
    """The text of ``export const|function|async function <name>`` up to the next top-level ``export``."""
    match = re.search(rf"^export (?:const|function|async function) {name}\b", source, re.MULTILINE)
    assert match, f"前端找不到 {name}——它被改名或移走了？"
    rest = source[match.end() :]
    following = re.search(r"^export ", rest, re.MULTILINE)
    return rest[: following.start()] if following else rest


def _fetches_from_backend(source: str) -> bool:
    """``getWorkflowNodeTemplate`` itself requests the endpoint (any quote style).

    Comments are not stripped: a regex stripper is not string-aware and turns ``"https://…"``
    into a false red, while an endpoint call that only survives in a comment inside this one
    function is a far less likely shape than a URL string.
    """
    body = _function_body(source, "getWorkflowNodeTemplate")
    return re.search(rf"axios\.get\(\s*(['\"`]){re.escape(FRONTEND_ENDPOINT)}\1", body) is not None


def _parse_frontend_literal(source: str) -> list[dict]:
    marker = "const workflowTemplate = "
    assert marker in source, (
        "前端 workflowTemplate 常量找不到——它被改名或移动了？"
        "在 T042 删除该常量之前，这条 live 对照不允许静默退场。"
    )
    start = source.index("[", source.index(marker))
    depth = 0
    end = -1
    for index in range(start, len(source)):
        char = source[index]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end > 0, "前端 workflowTemplate 字面量的括号不闭合——前端源码处于坏状态？"
    blob = source[start:end]
    # JS 字面量允许尾逗号与行注释，JSON 不允许——先剥掉再解析。
    blob = _strip_trailing_commas(_strip_line_comments(blob))
    return json.loads(blob)


def _strip_line_comments(source: str) -> str:
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == chr(92):
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(source) and source[index + 1] == "/":
            while index < len(source) and source[index] != "\n":
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_trailing_commas(source: str) -> str:
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(source):
        char = source[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == chr(92):
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            look = index + 1
            while look < len(source) and source[look] in " \t\r\n":
                look += 1
            if look < len(source) and source[look] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def test_templates_cover_all_twelve_types():
    assert [tpl["type"] for tpl in NODE_TEMPLATES] == EXPECTED_TYPES


def test_template_group_params_match_golden():
    golden = _golden()
    assert len(NODE_TEMPLATES) == len(golden), "模板条数与黄金快照不一致——是否有类型被增删？"
    for actual, expected in zip(NODE_TEMPLATES, golden, strict=True):
        assert actual == expected, (
            f"节点模板 {expected['type']!r} 与黄金快照不一致。"
            f"如果这是有意的变更，请更新 fixtures/node_templates_golden.json 并在提交说明里写清为什么。"
        )


def test_matches_frontend_source_when_available():
    frontend = _frontend_literal()
    if frontend is None:
        # 前端那份常量已被删除（T042）——后端此刻就是唯一真相源，本条对照退场。
        return
    assert NODE_TEMPLATES == frontend, "后端模板与前端常量不一致——两边必须逐字相同"


def test_frontend_keeps_no_template_copy():
    """T042 之后的常驻断言：前端只从后端取模板，源码里不再有任何一份副本（AC-01/AC-05）。"""
    source = FRONTEND_TEMPLATE.read_text(encoding="utf-8")
    assert not _has_template_copy(source), "前端模板接口文件里又出现了模板字面量"
    assert _fetches_from_backend(source), f"getWorkflowNodeTemplate 没有请求 {FRONTEND_ENDPOINT}"


def test_template_carries_no_ui_copy():
    """参数级文案位一律是 ``"true"`` 占位符或空串（AC-03）。"""
    offenders: list[str] = []
    for tpl in NODE_TEMPLATES:
        for group in tpl["group_params"]:
            for param in group.get("params", []):
                for field in ("label", "description", "placeholder", "help"):
                    value = param.get(field)
                    if value in (None, "", "true", []):
                        continue
                    offenders.append(f"{tpl['type']}.{param.get('key')}.{field}={value!r}")
    assert not offenders, f"模板里出现了界面文案：{offenders}"


def test_group_names_are_frontend_i18n_keys():
    """分组名是前端已有的 i18n 键——译文归前端资源，后端只发布键（AC-03）。"""
    nodes_by_locale = {locale: _locale_nodes(locale) for locale in LOCALES}
    missing: list[str] = []
    for tpl in NODE_TEMPLATES:
        for group in tpl["group_params"]:
            name = group.get("name")
            if not name:
                continue
            for locale, node in nodes_by_locale.items():
                if name not in node.get(tpl["type"], {}):
                    missing.append(f"{locale}: node.{tpl['type']}.{name}")
    assert not missing, f"分组名在前端 locale 里找不到对应键：{missing}"


def test_tool_node_template_contract_is_frozen():
    """工具节点模板的形状契约（AC-04）。

    前端 ``getToolTree`` 的拼装逻辑已搬进 ``get_tool_node_template``——本用例把
    后端产出的形状**冻结**住（这是迁移后的唯一实现，前端改动须经 T042 的
    前端收口回到这份实现，而不是再长一份）。输出参数 ``label`` 发布的是 i18n
    键 ``output`` 而非中文文案，由前端按 ``node.tool.output`` 渲染。
    """
    tool_info = {
        "id": 7,
        "tool_key": "tool_key_1",
        "name": "query_api",
        "desc": "查询接口",
        "api_params": [
            {"name": "query", "description": "检索词", "required": True},
            {"name": "limit", "description": None, "required": False},
        ],
    }
    template = get_tool_node_template(tool_info)
    assert template["id"] == 7
    assert template["tool_key"] == "tool_key_1"
    assert template["type"] == "tool"
    assert template["name"] == "query_api"
    assert template["description"] == "查询接口"

    params_group, output_group = template["group_params"]
    assert [p["key"] for p in params_group["params"]] == ["query", "limit"]
    assert params_group["params"][0] == {
        "key": "query",
        "label": "query",
        "type": "var_textarea",
        "test": "input",
        "desc": "检索词",
        "required": True,
        "value": "",
    }
    assert output_group["params"] == [
        {
            "global": "key",
            "key": "output",
            "label": "output",
            "type": "var",
            "value": "",
        }
    ]


def test_tool_node_template_tolerates_missing_api_params():
    """没有 api_params 的工具也要能生成模板——不能因为一个空列表炸掉。"""
    template = get_tool_node_template({"id": 1, "tool_key": "k", "name": "n", "desc": "d"})
    assert template["group_params"][0]["params"] == []
    assert template["group_params"][1]["params"][0]["key"] == "output"


CLI_TEMPLATE_FIXTURE = REPO_ROOT / "src" / "bisheng-cli" / "tests" / "fixtures" / "flow" / "node_templates.json"


def test_cli_fixture_matches_backend_templates():
    """F005 Wave 3：CLI 编译器/校验器的测试夹具是 NODE_TEMPLATES 的冻结副本，必须逐字相等。

    CLI 与后端包零耦合，测不到后端常量——它的单测只能对着一份快照跑。这条用例
    把快照钉在真值上：后端模板一改，这里变红，提醒同批刷新 CLI 夹具
    （否则 CLI 的编译/校验用例会对着过期的模板全绿）。夹具只给测试用，CLI 运行时
    从不回落到任何模板副本（AC-05）。
    """
    assert json.loads(CLI_TEMPLATE_FIXTURE.read_text(encoding="utf-8")) == NODE_TEMPLATES


@pytest.mark.parametrize(
    "literal",
    [
        'const workflowTemplate = [{"type": "start"}]',
        "const workflowTemplate=[{'group_params': [], 'type': 'start'}]",
        "const nodes = [{ group_params: [] }]",
        'const nodes = [{"group_params": []}]',
    ],
)
def test_copy_detector_recognizes_any_spelling(literal):
    """G8 W6 R2: a copy written with single quotes or without the old spacing must not pass as "no copy"."""
    assert _has_template_copy(literal)


def test_copy_detector_ignores_code_that_reads_nodes():
    assert not _has_template_copy("const { version_key } = nodeData.group_params[0].params[0].value")


@pytest.mark.parametrize(
    "declaration",
    [
        'export const getWorkflowNodeTemplate = async () => {\n    const u = "https://x";\n    return axios.get("/api/v1/workflow/node_templates")\n}',
        "export async function getWorkflowNodeTemplate() {\n    return axios.get('/api/v1/workflow/node_templates')\n}",
    ],
)
def test_backend_fetch_detector_accepts_ordinary_refactors(declaration):
    assert _fetches_from_backend(declaration)


def test_backend_fetch_detector_rejects_another_endpoint():
    assert not _fetches_from_backend('export const getWorkflowNodeTemplate = () => axios.get("/api/v1/elsewhere")')
