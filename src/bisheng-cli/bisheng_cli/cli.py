"""Argument surface. Standard-library `argparse`, no click/typer.

The dependency budget (design D12) buys two runtime dependencies and this is not
one of them: a subcommand parser with auto-generated `--help` is exactly what
argparse already does. What we give up is colour and shell completion, neither of
which any acceptance criterion asks for — AC-04 asks for "usable with no TTY and
machine-readable", which is orthogonal to looking nice.

A command is either registered or absent — never a stub. Registering a name so it
can answer "not in this version" would make `--help` advertise it, an agent would
call it, and it would get a non-standard failure. `DEFERRED_COMMANDS` is kept as
the (now empty) list of names announced in the epilogue but not registered, so
the test that guards "announced ≠ registered" keeps its shape for the next round
that defers something.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

from bisheng_cli import __version__
from bisheng_cli.errors import EXIT_USAGE, CliError

SUBCOMMANDS = ("init", "login", "deploy", "logs", "skills", "dev", "platforms", "flow")
DEFERRED_COMMANDS: tuple[str, ...] = ()

_EPILOG = (
    "命令: login / deploy / logs / skills sync / dev / platforms / flow。\n"
    "目标平台: 默认是最近一次 login 的平台；--platform <地址> 指定另一个已登录的平台，"
    "bisheng platforms use <地址> 切换默认。\n"
    "机器可读输出: 加 --json，NDJSON 走 stdout、人读文本走 stderr，最后一行恒为 result 事件。"
)


def _add_global_flags(parser: argparse.ArgumentParser, *, mirror: bool) -> None:
    """Global flags, defined on the root parser and mirrored onto each subcommand.

    argparse only accepts a root-level option *before* the subcommand, so a bare
    definition would make `bisheng deploy --json` fail with "unrecognized
    arguments" — and that is the order everyone writes, including this feature's
    own verification script. Mirroring the flags onto every subparser makes both
    orders work.

    The mirrors must use `default=SUPPRESS`. argparse parses a subcommand into a
    fresh namespace and then copies every key of it onto the outer one, so a
    mirror carrying an ordinary default would overwrite the value already parsed
    from `bisheng --json deploy` with `False`. SUPPRESS leaves the key out of the
    sub-namespace entirely when the flag was not given, so whatever the root
    parser decided survives.
    """

    def default(value: object) -> object:
        return argparse.SUPPRESS if mirror else value

    parser.add_argument(
        "--verbose",
        action="store_true",
        default=default(False),
        help="打印请求方法 / 路径 / 状态码 / 耗时（Authorization 头恒掩码）",
    )
    parser.add_argument("--quiet", action="store_true", default=default(False), help="只输出错误")
    parser.add_argument(
        "--json", action="store_true", dest="json_mode", default=default(False), help="机器可读 NDJSON 输出（stdout）"
    )
    parser.add_argument("--timeout", type=float, default=default(None), help="普通请求读超时秒数（默认 60）")
    parser.add_argument(
        "--no-proxy",
        action="store_true",
        default=default(False),
        help="忽略 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 环境变量",
    )
    # AC-12: selects among platforms that were each logged into on their own.
    # It never creates a profile — an address with no stored key is exit 3.
    parser.add_argument(
        "--platform",
        metavar="BASE_URL",
        default=default(None),
        help="作用于这个已登录的平台（默认：最近一次 login 或 platforms use 选定的平台）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bisheng",
        description="BiSheng 开发者 CLI：登录平台、本地运行、发布托管应用、查看运行日志。",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"bisheng {__version__}")
    _add_global_flags(parser, mirror=False)

    subparsers = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")

    init = subparsers.add_parser("init", help="Create an application bundle project")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--runtime", choices=("python3.11", "node20", "static"), default="python3.11")

    login = subparsers.add_parser("login", help="校验服务账号密钥并把凭据写入本地用户目录")
    login.add_argument("base_url", metavar="BASE_URL", help="目标平台地址，例如 http://bisheng.example.com")
    key_source = login.add_mutually_exclusive_group()
    key_source.add_argument(
        "--api-key", dest="api_key", default=None, help="直接传入密钥（会进入 shell 历史，建议改用其它方式）"
    )
    key_source.add_argument("--api-key-stdin", action="store_true", help="从标准输入读取密钥（非交互环境首选）")

    deploy = subparsers.add_parser("deploy", help="打包当前项目并发布到平台")
    deploy.add_argument("path", metavar="PATH", nargs="?", default=".", help="项目根，默认当前目录")
    deploy.add_argument("--app-id", dest="app_id", default=None, help="显式指定目标应用标识（覆盖 .bisheng/app.json）")
    deploy.add_argument("--wait", action="store_true", help="等待审批与上线结果落定")
    deploy.add_argument("--wait-timeout", type=int, default=1800, help="--wait 的最长等待秒数，默认 1800")
    deploy.add_argument("--confirm-schema-change", action="store_true", help="确认本次发布包含应用数据表结构变更")
    from bisheng_cli.release_notes import message

    notes_source = deploy.add_mutually_exclusive_group()
    notes_source.add_argument("--release-notes", default=None, help=message("text_help"))
    notes_source.add_argument("--release-notes-file", default=None, help=message("file_help"))
    deploy.add_argument("--yes", action="store_true", help="跳过目标应用确认（非交互环境必须显式带上）")
    deploy.add_argument("--dry-run", action="store_true", help="只打包与本地校验，打印体量统计，不上传")
    deploy.add_argument(
        "--no-agents-note",
        dest="no_agents_note",
        action="store_true",
        help="不要在项目 AGENTS.md 里写入技能包位置说明",
    )

    logs = subparsers.add_parser("logs", help="查看自己名下应用的运行日志")
    logs.add_argument("--app-id", dest="app_id", default=None, help="显式指定应用标识（覆盖 .bisheng/app.json）")
    logs.add_argument("--tail", type=int, default=200, help="返回最后 N 行，默认 200")
    logs.add_argument("--since", default=None, help="起始时间：epoch 秒，或 30m / 2h / 7d 相对窗口")
    logs.add_argument("--keyword", default=None, help="只返回含该关键字的行（服务端过滤，行数可能少于 --tail）")
    logs.add_argument("--follow", action="store_true", help="持续拉取（短轮询 3 秒）")

    # `skills` is the first two-token command: a plain subparser whose own
    # subparsers carry the verb. Dispatch (main.py) keys off the top-level
    # `command`, so `commands/skills.py` owns the `sync`-vs-missing branch.
    skills = subparsers.add_parser("skills", help="同步平台的开发者技能包并接入本机 AI 编程工具")
    skills_sub = skills.add_subparsers(dest="skills_command", metavar="{sync}")
    sync = skills_sub.add_parser("sync", help="拉取平台当前版本的技能包并接入本机 AI 编程工具（幂等，单向覆盖）")

    # `dev` takes no identity argument of any kind (AC-25): the injected identity
    # is the logged-in service account, full stop. The two port flags are the
    # only knobs — the proxy is the entry the developer opens, the app port is
    # what the app process receives as PORT.
    dev = subparsers.add_parser("dev", help="本地运行应用，并像托管环境一样注入身份与平台接线环境变量")
    dev.add_argument("path", metavar="PATH", nargs="?", default=".", help="项目根，默认当前目录")
    dev.add_argument(
        "--port",
        type=int,
        default=None,
        help="本地访问入口（迷你代理）监听的端口，默认取 bisheng-app.yaml 的 port",
    )
    dev.add_argument(
        "--app-port",
        dest="app_port",
        type=int,
        default=None,
        help="应用进程实际监听的端口（注入为 PORT），默认自动挑一个空闲端口",
    )

    platforms = subparsers.add_parser("platforms", help="列出已登录的平台，或切换默认平台")
    platforms_sub = platforms.add_subparsers(dest="platforms_command", metavar="{list,use}")
    platforms_list = platforms_sub.add_parser("list", help="列出本机已登录的平台（密钥恒掩码）")
    platforms_use = platforms_sub.add_parser("use", help="把某个已登录的平台设为默认")
    platforms_use.add_argument("base_url", metavar="BASE_URL", help="已登录过的平台地址")

    flow_leaves = _add_flow(subparsers)

    # Every leaf that can receive a flag needs the mirror, or `--json` after it
    # is "unrecognized arguments". `sync` is where `bisheng skills sync --json`
    # lands; `skills` covers `bisheng skills --json sync`.
    for subparser in (
        init,
        login,
        deploy,
        logs,
        skills,
        sync,
        dev,
        platforms,
        platforms_list,
        platforms_use,
        *flow_leaves,
    ):
        _add_global_flags(subparser, mirror=True)

    return parser


def _add_flow(subparsers: argparse._SubParsersAction) -> list[argparse.ArgumentParser]:
    """`bisheng flow <verb>` (F005). Returns every parser that needs the global-flag mirror.

    Two- and three-token, like `skills` / `platforms`: `flow` carries the verb,
    `flow tool` carries register / list. No flag names another identity (AC-42):
    every write is made as the key's own resource owner.
    """
    flow = subparsers.add_parser(
        "flow", help="工作流即代码：编译 / 对比 / 推送 / 运行 / 导出 YAML 声明的工作流，注册工具"
    )
    verbs = flow.add_subparsers(dest="flow_command", metavar="{compile,diff,push,run,export,tool}")
    spec_help = "工作流声明文件（YAML）；工作流绑定记在它旁边的 .bisheng/flow.json"
    overlay_help = "Python 覆盖层：只能改已声明参数的取值、追加未声明的节点与连线"

    compile_ = verbs.add_parser("compile", help="把声明编译成平台工作流定义并做结构校验（只读，不写平台）")
    compile_.add_argument("spec", metavar="SPEC", help=spec_help)
    compile_.add_argument("--overlay", default=None, help=overlay_help)
    compile_.add_argument("--out", default=None, help="把定义写到这个文件（默认打印到标准输出）")

    diff = verbs.add_parser("diff", help="对比本地声明与线上工作流（默认线上当前版本）")
    diff.add_argument("spec", metavar="SPEC", help=spec_help)
    diff.add_argument("--overlay", default=None, help=overlay_help)
    diff.add_argument(
        "--flow-id", dest="flow_id", default=None, help="目标工作流 id（默认取 .bisheng/flow.json 的绑定）"
    )
    baseline = diff.add_mutually_exclusive_group()
    baseline.add_argument("--version-id", dest="version_id", default=None, help="以这个历史版本为对比基准")
    baseline.add_argument("--list-versions", dest="list_versions", action="store_true", help="只列出工作流的版本")

    push = verbs.add_parser("push", help="为工作流新建一个版本承载本地定义，并切为当前版本")
    push.add_argument("spec", metavar="SPEC", help=spec_help)
    push.add_argument("--overlay", default=None, help=overlay_help)
    target = push.add_mutually_exclusive_group()
    target.add_argument(
        "--flow-id", dest="flow_id", default=None, help="目标工作流 id（默认取 .bisheng/flow.json 的绑定）"
    )
    target.add_argument("--create", action="store_true", help="在平台上新建工作流（名称与描述取自声明）并绑定")
    push.add_argument("--name", default=None, help="新版本的名称（默认 cli-<时间>）")
    push.add_argument("--description", default=None, help="新版本的描述")
    push.add_argument(
        "--allow-drop-online-only",
        dest="allow_drop_online_only",
        action="store_true",
        help="线上有本地没有的内容时也推送（它们保留在旧版本里）；非交互环境下必须显式带上",
    )
    push.add_argument("--no-activate", dest="no_activate", action="store_true", help="只建版本，不切为当前版本")

    run = verbs.add_parser("run", help="运行线上工作流，呈现事件流；停在等待输入时接受输入续跑")
    run.add_argument("spec", metavar="SPEC", help=spec_help)
    run.add_argument(
        "--flow-id", dest="flow_id", default=None, help="目标工作流 id（默认取 .bisheng/flow.json 的绑定）"
    )
    one_shot = run.add_mutually_exclusive_group()
    one_shot.add_argument("--input", default=None, help="一次性输入：用于下一次等待输入（单字段）")
    one_shot.add_argument("--input-file", dest="input_file", default=None, help="同 --input，从 UTF-8 文本文件读取")
    one_shot.add_argument("--input-json", dest="input_json", default=None, help="按字段给值的 JSON 对象（表单输入用）")
    run.add_argument("--resume", default=None, help="上一次运行停住时给出的续跑凭据")
    run.add_argument(
        "--wait-timeout",
        dest="wait_timeout",
        type=float,
        default=None,
        help="等平台发来下一个事件的最长秒数（默认 240）",
    )

    export = verbs.add_parser("export", help="把线上工作流导出成声明文件（声明表达不了的内容逐项列出）")
    export.add_argument(
        "spec", metavar="SPEC", help="要写出的声明文件（YAML）；工作流绑定记在它旁边的 .bisheng/flow.json"
    )
    export.add_argument(
        "--flow-id", dest="flow_id", default=None, help="来源工作流 id（默认取 .bisheng/flow.json 的绑定）"
    )
    export.add_argument("--version-id", dest="version_id", default=None, help="导出这个历史版本（默认线上当前版本）")
    export.add_argument("--force", action="store_true", help="声明文件已存在时覆盖它")

    tool = verbs.add_parser("tool", help="注册 / 列出工作流可引用的自定义工具")
    tool_verbs = tool.add_subparsers(dest="tool_command", metavar="{register,list}")
    register = tool_verbs.add_parser("register", help="按 OpenAPI 描述注册工具类型，报出各操作标识")
    register.add_argument("openapi", metavar="OPENAPI", help="OpenAPI 描述文件（YAML 或 JSON）")
    register.add_argument("--name", default=None, help="工具类型名（默认取 OpenAPI 的 info.title）")
    tool_list = tool_verbs.add_parser("list", help="列出可用的工具类型及其操作")

    return [flow, compile_, diff, push, run, export, tool, register, tool_list]


def confirm(
    prompt: str,
    *,
    assume_yes: bool,
    is_tty: bool,
    flag_name: str,
    reader: Callable[[str], str] = input,
) -> bool:
    """Ask for confirmation, or refuse when there is nobody to ask.

    "No TTY" must never mean "assume yes". The whole point of the guard is that
    the destructive case (overwriting somebody else's app, applying a schema
    change) is the one that happens unattended.
    """
    if assume_yes:
        return True
    if not is_tty:
        raise CliError(
            f"非交互环境下无法确认：{prompt}",
            exit_code=EXIT_USAGE,
            next_step=f"确认无误后带上 {flag_name} 重新执行。",
        )
    answer = reader(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")
