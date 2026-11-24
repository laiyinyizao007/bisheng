"""The CLI's door into the shared declaration rules (F007 design decision 1).

The rules live in ``bisheng_flow_dsl`` (one copy, also used by the platform). This
module only translates their ``DslError`` into the CLI's ``CliError`` — exit code and
next step are CLI presentation and stay here. No rule may be added to this file.
"""

from __future__ import annotations

from typing import Any

from bisheng_cli.errors import EXIT_DEFECT, EXIT_LOCAL_INVALID, CliError
from bisheng_flow_dsl import compiler as _compiler
from bisheng_flow_dsl import diff as _diff
from bisheng_flow_dsl.diff import count, online_only_items, render
from bisheng_flow_dsl.errors import DslError
from bisheng_flow_dsl.validate import validate

__all__ = ["compile_spec", "count", "diff", "online_only_items", "render", "resolve_tool", "validate"]

_NEXT_STEP = {
    "compile_issues": "按提示修正声明文件后重试。",
    "platform_shape": "确认平台版本与 CLI 匹配（平台可能过旧或过新）；加 --verbose 查看请求。",
    "online_shape": "线上数据不是平台画布会产生的形状：请带上工作流 id 与本条输出报平台故障；不要据此 push。",
    "local_shape": "先用 bisheng flow compile 确认声明能编译通过，再做 diff。",
}


def as_cli_error(error: DslError) -> CliError:
    """Same message and details; exit code from who is at fault, next step from the reason."""
    return CliError(
        error.message,
        exit_code=EXIT_DEFECT if error.kind == "defect" else EXIT_LOCAL_INVALID,
        next_step=_NEXT_STEP.get(error.reason, ""),
        details=error.details,
    )


def compile_spec(spec: dict[str, Any], templates: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        return _compiler.compile_spec(spec, templates, tools)
    except DslError as error:
        raise as_cli_error(error) from None


def resolve_tool(tools: Any, reference: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        return _compiler.resolve_tool(tools, reference)
    except DslError as error:
        raise as_cli_error(error) from None


def diff(local: Any, online: Any) -> dict[str, Any]:
    try:
        return _diff.diff(local, online)
    except DslError as error:
        raise as_cli_error(error) from None
