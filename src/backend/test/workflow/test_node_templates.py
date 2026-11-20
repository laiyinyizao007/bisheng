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
逐字比对；前端删掉那份常量之后，这条用例自动退场——那时后端就是唯一真相源。
"""

from __future__ import annotations

import json
from pathlib import Path

from bisheng.workflow.common.node_templates import NODE_TEMPLATES, get_tool_node_template

TEST_DIR = Path(__file__).resolve().parent
GOLDEN_PATH = TEST_DIR / "fixtures" / "node_templates_golden.json"

#: 仓库根（src/backend/test/workflow/ -> 上溯四级）
REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND_TEMPLATE = REPO_ROOT / "src" / "frontend" / "platform" / "src" / "controllers" / "API" / "workflow.ts"
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
    """解析前端的 ``workflowTemplate`` 字面量；文件不在（T042 已删）返回 None。

    **文件在但解析不了不算"退场"**——常量被改名、移动或源码临时坏掉时，
    这里必须让下面的对照测试红，而不是静默失去 live 对照。
    """
    if not FRONTEND_TEMPLATE.exists():
        return None
    return _parse_frontend_literal(FRONTEND_TEMPLATE.read_text(encoding="utf-8"))


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
