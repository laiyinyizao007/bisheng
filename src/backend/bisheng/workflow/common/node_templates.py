# ruff: noqa: RUF002, RUF003 - Chinese prose, full-width punctuation is intended
"""工作流节点模板——后端持有的唯一真相源。

12 类节点的模板此前只存在于前端（``src/frontend/platform/src/controllers/API/workflow.ts``
的 ``workflowTemplate`` 字面量）。本模块把它逐字搬到后端，前端与命令行工具都经接口
取同一份数据，模板变更不再需要两处同步。

**搬来的内容只有结构与键，没有界面文案**（AC-03）：

- 参数级 ``label`` / ``description`` / ``placeholder`` / ``help`` 一律是 ``"true"`` 占位符
  或空串——界面文案在前端本地化资源 ``public/locales/*/flow.json`` 的
  ``node.<type>.<param_key>`` 下，由前端 ``initNode`` 用 ``t()`` 替换；
- 分组名（``group_params[].name``）是**前端 i18n 键**，前端按
  ``t(`node.${node.type}.${cate.name}`)`` 渲染（如 ``node.llm.模型设置``）。
  键本身是中文，但它是键不是文案——译文仍在前端资源里，本模块不做任何本地化。

**不要在这里加界面文案。** 模板发布"有哪些分组、哪些参数键"与迁移当时的 `value`
默认值快照（前端建新节点要用它，黄金测试把默认值一并锁住）；参数的合法性由前端
与运行时各自负责。有意变更模板时，更新 `test/workflow/fixtures/node_templates_golden.json`
并在提交说明里写清原因。
"""

from __future__ import annotations

from typing import Any

#: 12 类节点的模板。结构与取值逐字来自前端的 ``workflowTemplate`` 字面量，
#: 不要"顺手整理"——它是一份对照快照，改动会让黄金对照测试失败。
NODE_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "start_xxx",
        "name": "true",
        "description": "true",
        "type": "start",
        "v": "3",
        "group_params": [
            {
                "name": "开场引导",
                "params": [
                    {
                        "key": "guide_word",
                        "label": "true",
                        "value": "",
                        "type": "textarea",
                        "placeholder": "true",
                    },
                    {
                        "key": "guide_question",
                        "label": "true",
                        "value": [],
                        "type": "input_list",
                        "placeholder": "true",
                        "help": "true",
                    },
                ],
            },
            {
                "name": "全局变量",
                "params": [
                    {
                        "key": "user_info",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "value": "",
                    },
                    {
                        "key": "current_time",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "value": "",
                    },
                    {
                        "key": "chat_history",
                        "global": "key",
                        "type": "chat_history_num",
                        "value": 10,
                    },
                    {
                        "key": "preset_question",
                        "label": "true",
                        "global": "item:input_list",
                        "type": "input_list",
                        "value": [],
                        "placeholder": "true",
                        "help": "true",
                    },
                    {
                        "key": "custom_variables",
                        "label": "true",
                        "global": "item:input_list",
                        "type": "global_var",
                        "value": [],
                        "help": "true",
                    },
                ],
            },
        ],
    },
    {
        "id": "input_xxx",
        "name": "true",
        "description": "true",
        "type": "input",
        "v": "3",
        "tab": {
            "value": "dialog_input",
            "options": [
                {
                    "label": "true",
                    "key": "dialog_input",
                    "help": "true",
                },
                {
                    "label": "true",
                    "key": "form_input",
                    "help": "true",
                },
            ],
        },
        "group_params": [
            {
                "name": "接收文本",
                "params": [
                    {
                        "key": "user_input",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "tab": "dialog_input",
                    },
                ],
            },
            {
                "name": "",
                "groupKey": "inputfile",
                "params": [
                    {
                        "groupTitle": True,
                        "key": "user_input_file",
                        "tab": "dialog_input",
                        "value": True,
                    },
                    {
                        "key": "file_parse_mode",
                        "type": "select_parsemode",
                        "tab": "dialog_input",
                        "value": "extract_text",
                    },
                    {
                        "key": "dialog_files_content",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "tab": "dialog_input",
                    },
                    {
                        "key": "dialog_files_content_size",
                        "label": "true",
                        "type": "char_number",
                        "min": 0,
                        "value": 15000,
                        "tab": "dialog_input",
                    },
                    {
                        "key": "dialog_file_accept",
                        "label": "true",
                        "type": "select_fileaccept",
                        "value": "all",
                        "tab": "dialog_input",
                    },
                    {
                        "key": "dialog_image_files",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "tab": "dialog_input",
                        "help": "true",
                    },
                    {
                        "key": "dialog_file_paths",
                        "global": "key",
                        "label": "true",
                        "type": "var",
                        "tab": "dialog_input",
                        "help": "true",
                    },
                ],
            },
            {
                "name": "",
                "groupKey": "custom",
                "params": [
                    {
                        "groupTitle": True,
                        "key": "recommended_questions_flag",
                        "label": "true",
                        "hidden": "true",
                        "tab": "dialog_input",
                        "help": "true",
                        "value": False,
                    },
                    {
                        "key": "recommended_llm",
                        "label": "true",
                        "type": "bisheng_model",
                        "tab": "dialog_input",
                        "value": "",
                        "placeholder": "true",
                        "required": True,
                    },
                    {
                        "key": "recommended_system_prompt",
                        "label": "true",
                        "tab": "dialog_input",
                        "type": "var_textarea",
                        "value": "true",
                        "required": True,
                    },
                    {
                        "key": "recommended_history_num",
                        "label": "true",
                        "type": "slide",
                        "tab": "dialog_input",
                        "help": "true",
                        "scope": [
                            1,
                            10,
                        ],
                        "step": 1,
                        "value": 2,
                    },
                ],
            },
            {
                "name": "",
                "params": [
                    {
                        "key": "form_input",
                        "global": "item:form_input",
                        "label": "true",
                        "type": "form",
                        "tab": "form_input",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "output_xxx",
        "name": "true",
        "description": "true",
        "type": "output",
        "v": "2",
        "group_params": [
            {
                "params": [
                    {
                        "key": "message",
                        "label": "true",
                        "global": "key",
                        "type": "var_textarea_file",
                        "required": True,
                        "placeholder": "true",
                        "value": {
                            "msg": "",
                            "files": [],
                        },
                    },
                    {
                        "key": "output_result",
                        "label": "true",
                        "global": "value.type=input",
                        "type": "output_form",
                        "required": True,
                        "value": {
                            "type": "",
                            "value": "",
                        },
                        "options": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "llm_xxx",
        "name": "true",
        "description": "true",
        "type": "llm",
        "v": "2",
        "tab": {
            "value": "single",
            "options": [
                {
                    "label": "true",
                    "key": "single",
                },
                {
                    "label": "true",
                    "key": "batch",
                },
            ],
        },
        "group_params": [
            {
                "params": [
                    {
                        "key": "batch_variable",
                        "label": "true",
                        "global": "self",
                        "type": "user_question",
                        "test": "var",
                        "value": [],
                        "required": True,
                        "linkage": "output",
                        "placeholder": "true",
                        "help": "true",
                        "tab": "batch",
                    },
                ],
            },
            {
                "name": "模型设置",
                "params": [
                    {
                        "key": "model_id",
                        "label": "true",
                        "type": "bisheng_model",
                        "value": "",
                        "required": True,
                        "placeholder": "true",
                    },
                    {
                        "key": "temperature",
                        "label": "true",
                        "type": "slide",
                        "scope": [
                            0,
                            2,
                        ],
                        "step": 0.1,
                        "value": 1,
                    },
                ],
            },
            {
                "name": "提示词",
                "params": [
                    {
                        "key": "system_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "test": "var",
                        "value": "",
                    },
                    {
                        "key": "user_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "test": "var",
                        "value": "",
                        "required": True,
                    },
                    {
                        "key": "image_prompt",
                        "label": "true",
                        "type": "image_prompt",
                        "value": [],
                        "help": "true",
                    },
                ],
            },
            {
                "name": "输出",
                "params": [
                    {
                        "key": "output_user",
                        "label": "true",
                        "type": "switch",
                        "help": "true",
                        "value": True,
                    },
                    {
                        "key": "output",
                        "global": "code:value.map(el => ({ label: el.label, value: el.key }))",
                        "label": "true",
                        "help": "true",
                        "type": "var",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "agent_xxx",
        "name": "true",
        "description": "true",
        "type": "agent",
        "v": "2",
        "tab": {
            "value": "single",
            "options": [
                {
                    "label": "true",
                    "key": "single",
                },
                {
                    "label": "true",
                    "key": "batch",
                },
            ],
        },
        "group_params": [
            {
                "params": [
                    {
                        "key": "batch_variable",
                        "label": "true",
                        "required": True,
                        "type": "user_question",
                        "test": "var",
                        "global": "self",
                        "value": [],
                        "linkage": "output",
                        "placeholder": "true",
                        "tab": "batch",
                        "help": "true",
                    },
                ],
            },
            {
                "name": "模型设置",
                "params": [
                    {
                        "key": "model_id",
                        "label": "true",
                        "type": "agent_model",
                        "required": True,
                        "value": "",
                        "placeholder": "true",
                    },
                    {
                        "key": "temperature",
                        "label": "true",
                        "type": "slide",
                        "scope": [
                            0,
                            2,
                        ],
                        "step": 0.1,
                        "value": 1,
                    },
                ],
            },
            {
                "name": "提示词",
                "params": [
                    {
                        "key": "system_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "test": "var",
                        "value": "true",
                        "placeholder": "true",
                        "required": True,
                    },
                    {
                        "key": "user_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "test": "var",
                        "value": "",
                        "placeholder": "true",
                        "required": True,
                    },
                    {
                        "key": "chat_history_flag",
                        "label": "true",
                        "type": "slide_switch",
                        "scope": [
                            0,
                            100,
                        ],
                        "step": 1,
                        "value": {
                            "flag": True,
                            "value": 50,
                        },
                        "help": "true",
                    },
                    {
                        "key": "image_prompt",
                        "label": "true",
                        "type": "image_prompt",
                        "value": "",
                        "help": "true",
                    },
                ],
            },
            {
                "name": "知识库",
                "params": [
                    {
                        "key": "knowledge_id",
                        "label": "true",
                        "type": "knowledge_select_multi",
                        "placeholder": "true",
                        "value": {
                            "type": "knowledge",
                            "value": [],
                        },
                    },
                    {
                        "key": "user_auth",
                        "label": "true",
                        "type": "switch",
                        "value": False,
                        "help": "true",
                    },
                ],
            },
            {
                "name": "数据库",
                "params": [
                    {
                        "key": "sql_agent",
                        "type": "sql_config",
                        "value": {
                            "open": False,
                            "db_address": "",
                            "db_name": "",
                            "db_username": "",
                            "db_password": "",
                        },
                    },
                ],
            },
            {
                "name": "工具",
                "params": [
                    {
                        "key": "tool_list",
                        "label": "true",
                        "type": "add_tool",
                        "value": [],
                    },
                ],
            },
            {
                "name": "输出",
                "params": [
                    {
                        "key": "output_user",
                        "label": "true",
                        "type": "switch",
                        "help": "true",
                        "value": True,
                    },
                    {
                        "key": "output",
                        "global": "code:value.map(el => ({ label: el.label, value: el.key }))",
                        "label": "true",
                        "type": "var",
                        "help": "true",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "qa_retriever_xxx",
        "name": "true",
        "description": "true",
        "type": "qa_retriever",
        "v": "1",
        "group_params": [
            {
                "name": "检索设置",
                "params": [
                    {
                        "key": "user_question",
                        "label": "true",
                        "type": "var_select",
                        "test": "var",
                        "value": "",
                        "required": True,
                        "placeholder": "true",
                    },
                    {
                        "key": "qa_knowledge_id",
                        "label": "true",
                        "type": "qa_select_multi",
                        "value": [],
                        "required": True,
                        "placeholder": "true",
                    },
                    {
                        "key": "score",
                        "label": "true",
                        "type": "slide",
                        "value": 0.8,
                        "scope": [
                            0.01,
                            0.99,
                        ],
                        "step": 0.01,
                        "help": "true",
                    },
                ],
            },
            {
                "name": "输出",
                "params": [
                    {
                        "key": "retrieved_result",
                        "label": "true",
                        "type": "var",
                        "global": "key",
                        "value": "",
                    },
                ],
            },
        ],
    },
    {
        "id": "rag_xxx",
        "name": "true",
        "description": "true",
        "type": "rag",
        "v": "2",
        "group_params": [
            {
                "name": "知识库检索设置",
                "params": [
                    {
                        "key": "user_question",
                        "label": "true",
                        "global": "self=user_prompt",
                        "type": "user_question",
                        "test": "var",
                        "help": "true",
                        "linkage": "output_user_input",
                        "value": [],
                        "placeholder": "true",
                        "required": True,
                    },
                    {
                        "key": "knowledge",
                        "label": "true",
                        "type": "knowledge_select_multi",
                        "placeholder": "true",
                        "value": {
                            "type": "knowledge",
                            "value": [],
                        },
                        "required": True,
                    },
                    {
                        "key": "metadata_filter",
                        "label": "true",
                        "type": "metadata_filter",
                        "value": {},
                    },
                    {
                        "key": "advanced_retrieval_switch",
                        "label": "true",
                        "type": "search_switch",
                        "value": {},
                    },
                    {
                        "key": "retrieved_result",
                        "label": "true",
                        "type": "var",
                        "global": "self=user_prompt",
                    },
                ],
            },
            {
                "name": "AI回复生成设置",
                "params": [
                    {
                        "key": "system_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "value": "true",
                        "required": True,
                    },
                    {
                        "key": "user_prompt",
                        "label": "true",
                        "type": "var_textarea",
                        "value": "true",
                        "test": "var",
                        "required": True,
                    },
                    {
                        "key": "model_id",
                        "label": "true",
                        "type": "bisheng_model",
                        "value": "",
                        "required": True,
                        "placeholder": "true",
                    },
                    {
                        "key": "temperature",
                        "label": "true",
                        "type": "slide",
                        "scope": [
                            0,
                            2,
                        ],
                        "step": 0.1,
                        "value": 1,
                    },
                ],
            },
            {
                "name": "输出",
                "params": [
                    {
                        "key": "output_user",
                        "label": "true",
                        "type": "switch",
                        "value": True,
                        "help": "true",
                    },
                    {
                        "key": "output_user_input",
                        "label": "true",
                        "type": "var",
                        "help": "true",
                        "global": "code:value.map(el => ({ label: el.label, value: el.key }))",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "knowledge_retriever_xxx",
        "name": "true",
        "description": "true",
        "type": "knowledge_retriever",
        "v": "1",
        "group_params": [
            {
                "name": "知识库检索设置",
                "params": [
                    {
                        "key": "user_question",
                        "label": "true",
                        "global": "self=user_prompt",
                        "type": "user_question",
                        "test": "var",
                        "help": "true",
                        "linkage": "retrieved_result",
                        "value": [],
                        "placeholder": "true",
                        "required": True,
                    },
                    {
                        "key": "knowledge",
                        "label": "true",
                        "type": "knowledge_select_multi",
                        "placeholder": "true",
                        "value": {
                            "type": "knowledge",
                            "value": [],
                        },
                        "required": True,
                    },
                    {
                        "key": "metadata_filter",
                        "label": "true",
                        "type": "metadata_filter",
                        "value": {},
                    },
                    {
                        "key": "advanced_retrieval_switch",
                        "label": "true",
                        "type": "search_switch",
                        "value": {},
                    },
                ],
            },
            {
                "name": "输出",
                "params": [
                    {
                        "key": "retrieved_result",
                        "label": "true",
                        "type": "var",
                        "global": "code:value.map(el => ({ label: el.label, value: el.key }))",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "report_xxx",
        "name": "true",
        "description": "true",
        "type": "report",
        "v": "1",
        "group_params": [
            {
                "params": [
                    {
                        "key": "report_info",
                        "label": "true",
                        "placeholder": "true",
                        "required": True,
                        "type": "report",
                        "value": {},
                    },
                ],
            },
        ],
    },
    {
        "id": "code_xxx",
        "name": "true",
        "description": "true",
        "type": "code",
        "v": "1",
        "group_params": [
            {
                "name": "入参",
                "params": [
                    {
                        "key": "code_input",
                        "type": "code_input",
                        "test": "input",
                        "required": True,
                        "value": [
                            {
                                "key": "arg1",
                                "type": "input",
                                "label": "",
                                "value": "",
                            },
                            {
                                "key": "arg2",
                                "type": "input",
                                "label": "",
                                "value": "",
                            },
                        ],
                    },
                ],
            },
            {
                "name": "执行代码",
                "params": [
                    {
                        "key": "code",
                        "type": "code",
                        "required": True,
                        "value": "def main(arg1: str, arg2: str) -> dict: \n    return {'result1': arg1, 'result2': arg2}",
                    },
                ],
            },
            {
                "name": "出参",
                "params": [
                    {
                        "key": "code_output",
                        "type": "code_output",
                        "global": "code:value.map(el => ({ label: el.key, value: el.key }))",
                        "required": True,
                        "value": [
                            {
                                "key": "result1",
                                "type": "str",
                            },
                            {
                                "key": "result2",
                                "type": "str",
                            },
                        ],
                    },
                ],
            },
        ],
    },
    {
        "id": "condition_xxx",
        "name": "true",
        "description": "true",
        "type": "condition",
        "v": "1",
        "group_params": [
            {
                "params": [
                    {
                        "key": "condition",
                        "label": "",
                        "type": "condition",
                        "value": [],
                    },
                ],
            },
        ],
    },
    {
        "id": "end_xxx",
        "name": "true",
        "description": "true",
        "type": "end",
        "v": "1",
        "group_params": [],
    },
]

#: 工具节点模板的两个分组名。它们是**前端 i18n 键**（``node.tool.工具参数`` /
#: ``node.tool.输出``），前端按 ``t(`node.${node.type}.${cate.name}`)`` 渲染；
#: 译文在前端 ``public/locales/*/flow.json`` 里，本模块不做本地化。
TOOL_PARAMS_GROUP_NAME = "工具参数"
TOOL_OUTPUT_GROUP_NAME = "输出"

#: 工具输出参数的键。``label`` 用这个键而不是中文——前端的参数标题按
#: ``t('node.tool.output.label')`` 取，那个译文在前端资源里。
TOOL_OUTPUT_PARAM_KEY = "output"


def get_tool_node_template(tool_info: dict[str, Any]) -> dict[str, Any]:
    """由一条工具信息生成工具节点的模板。

    形状与前端 ``util/flowUtils.ts`` 的 ``getToolTree`` 逐字一致——它是那次
    "工具节点模板生成逻辑从前端搬到后端"的搬迁目标，前端改成直接消费后端产物
    （AC-04）。

    :param tool_info: 工具类型下的一条子工具，字段与 ``GptsToolsBase`` 对齐
        （``id`` / ``tool_key`` / ``name`` / ``desc`` / ``api_params``）。
    :return: 可直接作为工作流节点 ``data`` 的工具节点模板。
    """
    api_params = tool_info.get("api_params") or []
    params = [
        {
            "key": item.get("name"),
            "label": item.get("name"),
            "type": "var_textarea",
            "test": "input",
            "desc": item.get("description"),
            "required": item.get("required"),
            "value": "",
        }
        for item in api_params
    ]
    return {
        "id": tool_info.get("id"),
        "tool_key": tool_info.get("tool_key"),
        "type": "tool",
        "name": tool_info.get("name"),
        "description": tool_info.get("desc"),
        "group_params": [
            {
                "name": TOOL_PARAMS_GROUP_NAME,
                "params": params,
            },
            {
                "name": TOOL_OUTPUT_GROUP_NAME,
                "params": [
                    {
                        "global": "key",
                        "key": TOOL_OUTPUT_PARAM_KEY,
                        "label": TOOL_OUTPUT_PARAM_KEY,
                        "type": "var",
                        "value": "",
                    }
                ],
            },
        ],
    }
