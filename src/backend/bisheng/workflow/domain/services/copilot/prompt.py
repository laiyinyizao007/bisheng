# ruff: noqa: RUF001 -- the prompt is Chinese copy with full-width punctuation
"""What the copilot's model is told (F007 design decision 5).

System message = ``PREFACE`` + the packaged ``workflow-as-code`` SKILL.md verbatim
+ the usable resources by name. The skill pack is the one source of node knowledge
(AC-22): nothing here restates parameters. Chapters 1-2 of the skill are written for
a command-line assistant (compile / push / stop and ask); the preface overrides them
explicitly instead of slicing the file, which would break on the first heading edit.

Every turn is grounded on the canvas as sent (its declaration goes in the last user
message); earlier assistant turns keep their text but lose any YAML (AC-07).
Oversized input raises 10562 — a proposal built on part of a canvas is worse than none.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bisheng.common.errcode.flow import WorkflowCopilotTooLargeError
from bisheng.workflow.domain.services.copilot.resources import Catalogue

#: The packaged skill (read-only file shipped with the backend; constitution C8 is about shared state).
SKILL_PATH = Path(__file__).resolve().parents[4] / "dev_toolkit" / "skills" / "workflow-as-code" / "SKILL.md"
MAX_DECLARATION_CHARS = 60_000
#: Rounds of conversation: user turns, and all turns at twice that (a client can send only assistant turns).
MAX_HISTORY_TURNS = 20
#: The history's text in all, after assistant YAML is dropped (each turn is also capped by the request schema).
MAX_HISTORY_CHARS = 60_000
RESOURCES_HEADING = "## 你可以使用的资源（只写名称）"
CODE_NODE_OFF = "本平台已关闭代码节点：不要使用 `code` 类型的节点；用户要求时，说明该能力已被平台关闭。"
_ROLES = frozenset({"user", "assistant"})
_YAML_BLOCK = re.compile(r"```ya?ml\b.*?```", re.DOTALL | re.IGNORECASE)

PREFACE = """\
# 你的角色

你是毕昇工作流编辑页里的 copilot。用户在编辑一条工作流，你根据他们的话生成或修改这条工作流。
你的回答会先给用户看差异，用户点「应用」才会落到画布上；你不能保存、上线或运行工作流。

# 回答格式（优先于下面技能文档里的任何说法）

- 先用一两句话说明你打算做什么。
- 需要给出方案时，紧接着输出**一个** ```yaml 代码块，内容是**完整的**工作流声明（全部节点和连线，不是片段）。
- 用户只是提问、或需求不清楚时，只回答文字、不要输出代码块；需求不清楚就追问。
- 下面技能文档的第 1、2 章是写给命令行助手的（编译、推送、停下问人），在这里**不适用**：
  不要让用户运行任何命令，不要提 push，不要向用户索要资源 id。
- 不要使用 `!include`，长文本直接写进 YAML（多行用 `|`）。
- 不要写 `position`，布局由平台安排。
- 值为 `<已隐藏>` 的参数是平台隐藏的密码、密钥等敏感值：原样保留在原位置，不要改写，也不要复制到别的节点或参数；
  新节点需要这类值时留空，由用户自己在画布上填写。

# 修改已有工作流

- 用户消息里会给出画布当前的声明。**保留所有现有节点的 id**；用户没要求改的节点、参数、连线一律原样保留。
- 每个新增或改动的节点都必须能从开始节点沿连线走到；新增或改动的节点里，除 `end` 外都要有出边。

# 资源（模型、知识库、工具）一律写名称

平台会把名称换成用户有权使用的资源；对不上的会留给用户在画布上选。写法：
- 模型参数（`model_id` 等）：写模型名称，例如 `model_id: 通义千问`。Agent 节点只能用「Agent 可用模型」里的名称。
- 知识库参数：`{type: knowledge, value: [{label: 知识库名称}]}`；问答知识库：`[{label: 名称}]`。
- Agent 的工具列表：`[{label: 工具名}]`；工具节点：`tool: 工具名`。
- 只用下面清单里的名称；清单里没有用户说的资源时，照用户的原话写名称，不要编造。
"""


def skill_text() -> str:
    return SKILL_PATH.read_bytes().decode("utf-8")


def build_messages(
    *,
    declaration_yaml: str,
    history: list[dict[str, Any]],
    message: str,
    catalogue: Catalogue,
    code_node_enabled: bool,
) -> list[dict[str, str]]:
    """``[{"role", "content"}]``: system, the earlier turns (text only), then this turn."""
    if len(declaration_yaml) > MAX_DECLARATION_CHARS:
        raise WorkflowCopilotTooLargeError()
    turns = _history(history)
    if (
        sum(1 for t in turns if t["role"] == "user") > MAX_HISTORY_TURNS
        or len(turns) > 2 * MAX_HISTORY_TURNS
        or sum(len(t["content"]) for t in turns) > MAX_HISTORY_CHARS
    ):
        raise WorkflowCopilotTooLargeError()

    system = "\n\n".join(
        part
        for part in (
            PREFACE,
            CODE_NODE_OFF if not code_node_enabled else "",
            "# 工作流声明技能文档\n\n" + skill_text(),
            _resources(catalogue),
        )
        if part
    )
    current = f"画布当前的声明：\n\n```yaml\n{declaration_yaml}```\n\n我的需求：{message}"
    return [{"role": "system", "content": system}, *turns, {"role": "user", "content": current}]


def retry_messages(messages: list[dict[str, str]], answer: str, issues: list[str]) -> list[dict[str, str]]:
    """The same conversation plus the rejected answer and why it was rejected."""
    listed = "\n".join(f"- {issue}" for issue in issues)
    feedback = f"上面的方案没有通过平台校验：\n{listed}\n\n请修正后重新输出完整的工作流声明（一个 ```yaml 代码块）。"
    return [*messages, {"role": "assistant", "content": answer}, {"role": "user", "content": feedback}]


def _history(history: list[dict[str, Any]]) -> list[dict[str, str]]:
    turns = []
    for item in history:
        role = item.get("role")
        if role not in _ROLES:
            raise ValueError(f"history role {role!r} is not allowed")
        content = str(item.get("content") or "")
        if role == "assistant":
            content = _YAML_BLOCK.sub("", content).strip()
        turns.append({"role": role, "content": content})
    return turns


def _resources(catalogue: Catalogue) -> str:
    def names(items: list[Any]) -> str:
        return "、".join(r.name for r in items) or "（无）"

    return "\n".join(
        [
            RESOURCES_HEADING,
            "",
            f"- 模型：{names(catalogue.models)}",
            f"- Agent 可用模型：{names(catalogue.agent_models)}",
            f"- 文档知识库：{names(catalogue.knowledge)}",
            f"- 问答知识库：{names(catalogue.qa_knowledge)}",
            f"- 工具：{names(catalogue.tool_children())}",
        ]
    )
