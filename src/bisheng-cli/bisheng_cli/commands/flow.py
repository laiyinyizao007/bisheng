"""`bisheng flow` — workflows as code: compile, diff, push, run, and tool registration.

Every verb is a thin wire between pieces that are tested on their own — the
declaration loader (``flow.spec``), the overlay (``flow.overlay``), the compiler
and validator, the diff, and the client layer. What lives here is the order of
operations and what each verb refuses to claim.

* **Local checks first.** A declaration is loaded and quick-checked before any
  request leaves (AC-11); a workflow id is resolved before anything is compiled.
* **The CLI judges no permission.** Every refusal is the platform's, translated.
* **Push adds, never rewrites (AC-25).** It creates a version and switches to
  it. Content only the platform has is shown and needs an explicit yes; with
  nobody at the keyboard there is no yes (AC-26). "Only the platform has" is
  judged against the last definition this machine pushed when there is one
  (``flow.binding``), so a value the YAML itself moved past is not mistaken for
  a web edit. A live workflow gets the version but not the switch, and the run
  says so instead of "pushed" (AC-27).
* **Run never waits for a person who is not there (AC-33).** A one-shot input
  is used once; the next pause ends the command with a resume token carrying
  the session id *and* the waiting message's id (AC-31).
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from bisheng_cli import credentials
from bisheng_cli.cli import confirm
from bisheng_cli.errors import (
    EXIT_CANCELLED,
    EXIT_DEFECT,
    EXIT_LOCAL_INVALID,
    EXIT_OK,
    EXIT_PRECHECK_FAILED,
    EXIT_USAGE,
    CliError,
)
from bisheng_cli.flow import binding
from bisheng_cli.flow.client import FlowClient, parse_flow_id, parse_version_id
from bisheng_cli.flow.compiler import compile_spec
from bisheng_cli.flow.diff import count, online_only_items, render
from bisheng_cli.flow.diff import diff as diff_definitions
from bisheng_cli.flow.overlay import apply_overlay
from bisheng_cli.flow.spec import load_spec
from bisheng_cli.flow.validate import validate
from bisheng_cli.http import PlatformClient
from bisheng_cli.output import Emitter

COMMAND = "flow"
ALLOW_DROP_FLAG = "--allow-drop-online-only"
LIVE_WORKFLOW = 10525
#: The reason a node carries when the run was stopped (``IgnoreException("stop by user")``, nodes/base.py).
STOPPED_BY_USER = "stop by user"

#: Events after which the run waits for the caller (``RedisCallback._INPUT_CATEGORIES``).
WAITING_EVENTS = frozenset({"input", "output_with_input_msg", "output_with_choose_msg"})
#: The one input type whose items are all fields to fill; every other pause has exactly one
#: (a dialog input sends its text field first, then optional attachments such as
#: ``user_input_file`` — untyped — and the file pickers; the platform requires only the first).
_FORM_INPUT = "form_input"
_RESUME_PREFIX = "r1."

#: What ``POST /api/v2/tools`` accepts (``OpenToolTypeCreate`` / ``OpenToolCreate``, both ``extra="forbid"``).
_TOOL_TYPE_KEYS = (
    "name",
    "description",
    "logo",
    "server_host",
    "auth_method",
    "api_key",
    "auth_type",
    "is_preset",
    "openapi_schema",
    "parameter_name",
    "api_location",
)
_TOOL_KEYS = ("name", "desc", "logo", "tool_key", "api_params", "extra")


def _interactive(emitter: Emitter) -> bool:
    """Is a person at the keyboard? Both ends must be a terminal."""
    return emitter.is_tty and bool(getattr(sys.stdin, "isatty", lambda: False)())


def _ask(prompt: str) -> str:
    return input(prompt)


def run(args: Any, emitter: Emitter) -> int:
    verbs = {"compile": _compile, "diff": _diff, "push": _push, "run": _run, "tool": _tool}
    verb = getattr(args, "flow_command", None)
    if verb not in verbs:
        emitter.error("用法: bisheng flow {compile,diff,push,run,tool} <声明文件> …（bisheng flow <动词> -h 查看参数）")
        return EXIT_USAGE
    return verbs[verb](args, emitter)


# ---- shared plumbing -----------------------------------------------------------------


def _platform(args: Any, emitter: Emitter, profile: credentials.Profile) -> PlatformClient:
    return PlatformClient(
        profile.base_url,
        api_key=profile.api_key,
        trust_env=not getattr(args, "no_proxy", False),
        timeout=getattr(args, "timeout", None),
        emitter=emitter,
    )


def _load(args: Any) -> dict[str, Any]:
    spec = load_spec(args.spec)
    overlay = getattr(args, "overlay", None)
    return apply_overlay(spec, overlay) if overlay else spec


def _build(spec: dict[str, Any], client: FlowClient, emitter: Emitter) -> dict[str, Any]:
    """Compile against the platform's catalogue and validate; every issue reported at once."""
    templates = client.node_templates()
    tools = client.tools()
    definition = compile_spec(spec, templates, tools)
    issues = validate(definition, templates, tools)
    if issues:
        emitter.error(f"校验未通过，共 {len(issues)} 处：")
        for issue in issues:
            emitter.error(f"  · {issue['message']}")
        raise CliError(
            f"工作流定义未通过校验（{len(issues)} 处）",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="按上面列出的问题修正声明文件后重试。",
            details={"issues": issues},
        )
    emitter.info(f"校验通过：{len(definition['nodes'])} 个节点，{len(definition['edges'])} 条连线")
    return definition


def _target(args: Any, profile: credentials.Profile) -> tuple[str, bool]:
    raw, explicit = binding.resolve_flow_id(args.spec, profile.base_url, getattr(args, "flow_id", None))
    return parse_flow_id(raw), explicit


def _bind(args: Any, emitter: Emitter, profile: credentials.Profile, fid: str, *, name: str | None = None) -> bool:
    """Record the binding; a write failure is a warning, never a reason to hide what the platform did."""
    try:
        binding.save_binding(args.spec, profile.base_url, fid, name=name)
        return True
    except (OSError, CliError) as exc:
        reason = exc.message if isinstance(exc, CliError) else exc.__class__.__name__
        emitter.warn(
            f"工作流绑定没有写入 {binding.STATE_DIR}/{binding.FLOW_REF_NAME}（{reason}）；"
            f"之后的命令请带 --flow-id {fid}。"
        )
        return False


# ---- compile ---------------------------------------------------------------------------


def _compile(args: Any, emitter: Emitter) -> int:
    spec = _load(args)
    profile = credentials.load_selected(args)
    with _platform(args, emitter, profile) as platform:
        definition = _build(spec, FlowClient(platform, cache_path=binding.template_cache_path(args.spec)), emitter)

    text = json.dumps(definition, ensure_ascii=False, indent=2)
    out = Path(args.out) if args.out else None
    if out is not None:
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise CliError(
                f"无法写出 {out}（{exc.__class__.__name__}）",
                exit_code=EXIT_LOCAL_INVALID,
                next_step="换一个可写的 --out 路径。",
            ) from None
        emitter.info(f"已写出 {out}")
    else:
        emitter.output(text)
    emitter.result(
        COMMAND,
        ok=True,
        exit_code=EXIT_OK,
        data={
            "definition": definition,
            "nodes": len(definition["nodes"]),
            "edges": len(definition["edges"]),
            "out": str(out) if out is not None else None,
        },
    )
    return EXIT_OK


# ---- diff ------------------------------------------------------------------------------


def _diff(args: Any, emitter: Emitter) -> int:
    profile = credentials.load_selected(args)
    fid, explicit = _target(args, profile)
    if args.list_versions:
        return _list_versions(args, emitter, profile, fid, explicit)

    vid = parse_version_id(args.version_id) if args.version_id is not None else None
    spec = _load(args)
    with _platform(args, emitter, profile) as platform:
        client = FlowClient(platform, cache_path=binding.template_cache_path(args.spec))
        local = _build(spec, client, emitter)
        online = client.version_definition(fid, vid) if vid is not None else client.current_definition(fid)
    if explicit:
        _bind(args, emitter, profile, fid)

    result = diff_definitions(local, online)
    where = f"版本 {vid}" if vid is not None else "线上当前版本"
    emitter.info(f"对比基准：工作流 {fid} 的{where}")
    for line in render(result):
        emitter.info(line)
    emitter.result(
        COMMAND,
        ok=True,
        exit_code=EXIT_OK,
        data={
            "baseline": {"flow_id": fid, "version_id": vid},
            "identical": result["identical"],
            "count": count(result),
            "diff": result,
        },
    )
    return EXIT_OK


def _list_versions(args: Any, emitter: Emitter, profile: credentials.Profile, fid: str, explicit: bool) -> int:
    with _platform(args, emitter, profile) as platform:
        rows = FlowClient(platform).list_versions(fid)
    if explicit:
        _bind(args, emitter, profile, fid)
    versions = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "is_current": bool(row.get("is_current")),
            "description": row.get("description"),
            "update_time": row.get("update_time") or row.get("create_time"),
        }
        for row in rows
    ]
    emitter.info(f"工作流 {fid} 的版本（{len(versions)} 个）：")
    for version in versions:
        mark = "  ← 当前" if version["is_current"] else ""
        emitter.info(f"  {version['id']}  {version['name']}  {version['update_time'] or ''}{mark}")
    emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data={"flow_id": fid, "versions": versions})
    return EXIT_OK


# ---- push ------------------------------------------------------------------------------


def _push(args: Any, emitter: Emitter) -> int:
    profile = credentials.load_selected(args)
    if args.create:
        bound = binding.read_binding(args.spec, profile.base_url)
        if bound:
            raise CliError(
                f"{Path(args.spec).name} 已绑定工作流 {bound['flow_id']}，--create 会另建一条",
                exit_code=EXIT_USAGE,
                next_step="推送到已绑定的工作流就去掉 --create；确实要另建一条，先删掉 "
                f"{binding.STATE_DIR}/{binding.FLOW_REF_NAME} 里这条绑定。",
            )
        fid, explicit = None, False
    else:
        fid, explicit = _target(args, profile)
    spec = _load(args)

    with _platform(args, emitter, profile) as platform:
        client = FlowClient(platform, cache_path=binding.template_cache_path(args.spec))
        local = _build(spec, client, emitter)
        if fid is None:
            return _create(args, emitter, profile, client, spec, local)

        online = client.current_definition(fid)
        if explicit:
            _bind(args, emitter, profile, fid)
        result = diff_definitions(local, online)
        if result["identical"]:
            emitter.info(f"本地定义与工作流 {fid} 的线上当前版本一致（不计画布坐标与视图），未新建版本。")
            emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data={"flow_id": fid, "pushed": False})
            return EXIT_OK

        dropped = _confirm_online_only(args, emitter, profile, fid, result, online)
        name = args.name or _default_version_name()
        row = client.create_version(fid, name, local, description=args.description)
        vid, vname = row["id"], row.get("name") or name
        if args.no_activate:
            emitter.info(f"已新建版本 {vid}（{vname}），未切换当前版本（--no-activate）。")
            emitter.result(
                COMMAND,
                ok=True,
                exit_code=EXIT_OK,
                data=_pushed(fid, vid, vname, activated=False, dropped=dropped),
            )
            return EXIT_OK
        _activate(client, fid, vid, vname)

    binding.save_last_push(args.spec, profile.base_url, fid, local, version_id=vid)
    emitter.info(f"已推送到工作流 {fid}：新版本 {vid}（{vname}）已切为当前版本。")
    emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data=_pushed(fid, vid, vname, activated=True, dropped=dropped))
    return EXIT_OK


def _pushed(fid: str, vid: int, vname: str, *, activated: bool, dropped: tuple[int, str | None]) -> dict[str, Any]:
    return {
        "flow_id": fid,
        "pushed": True,
        "version_id": vid,
        "version_name": vname,
        "activated": activated,
        "online_only_dropped": dropped[0],
        "online_only_confirmation": dropped[1],
    }


def _default_version_name() -> str:
    """Time for the reader, a random tail so two pushes in one second never collide (10502)."""
    return time.strftime("cli-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def _create(
    args: Any,
    emitter: Emitter,
    profile: credentials.Profile,
    client: FlowClient,
    spec: dict[str, Any],
    local: dict[str, Any],
) -> int:
    description = spec.get("description") if isinstance(spec.get("description"), str) else None
    row = client.create_flow(str(spec["name"]), local, description=description)
    fid = parse_flow_id(row["id"])
    version_id = row["version_id"]
    bound = _bind(args, emitter, profile, fid, name=str(spec["name"]))
    binding.save_last_push(args.spec, profile.base_url, fid, local, version_id=version_id)
    emitter.info(
        f"已新建工作流 {fid}（{spec['name']}），当前版本 {version_id}；标识已记入 {binding.STATE_DIR}/{binding.FLOW_REF_NAME}。"
    )
    emitter.result(
        COMMAND,
        ok=True,
        exit_code=EXIT_OK,
        data={
            "flow_id": fid,
            "created": True,
            "pushed": True,
            "version_id": version_id,
            "activated": True,
            "binding_saved": bound,
        },
    )
    return EXIT_OK


def _confirm_online_only(
    args: Any,
    emitter: Emitter,
    profile: credentials.Profile,
    fid: str,
    result: dict[str, Any],
    online: dict[str, Any],
) -> tuple[int, str | None]:
    """Show what the new version will no longer carry and get a yes for it (AC-26).

    Returns ``(items, how)``: how many online-only items the new version replaces, and
    why that was allowed — ``"confirmed"`` (a yes or the flag) or ``"unchanged_since_last_push"``.
    The count is reported either way, so the machine result says what the human text says.
    """
    items = online_only_items(result)
    if not items:
        return 0, None
    base = binding.read_last_push(args.spec, profile.base_url, fid)
    if base is not None and diff_definitions(base, online)["identical"]:
        # Online is exactly what this machine pushed last: nothing was edited on the
        # web since, and the differing values are the YAML's own previous ones.
        emitter.info(f"线上当前版本就是本机上次推送的内容，{len(items)} 处旧取值来自上次推送，无需确认。")
        return len(items), "unchanged_since_last_push"

    emitter.warn(f"线上有 {len(items)} 处本地没有的内容；推送后它们只保留在旧版本里，新版本与当前版本都不再包含：")
    for item in items:
        emitter.error(f"  + {_item_line(item)}")
    details = {"flow_id": fid, "online_only": items}
    try:
        agreed = confirm(
            "确认推送，并让当前版本不再包含以上内容?",
            assume_yes=args.allow_drop_online_only,
            is_tty=_interactive(emitter),
            flag_name=ALLOW_DROP_FLAG,
            reader=_ask,
        )
    except CliError as err:
        err.details = details
        raise
    if not agreed:
        raise CliError(
            "已按用户要求取消本次推送",
            exit_code=EXIT_USAGE,
            next_step="先用 bisheng flow diff 看清线上改动，把要保留的写回 YAML 后再推送。",
            details=details,
        )
    return len(items), "confirmed"


def _item_line(item: dict[str, Any]) -> str:
    kind = item["kind"]
    if kind == "node":
        return f"节点 {item.get('id')}（{item.get('type')}{'，' + str(item['name']) if item.get('name') else ''}）"
    if kind == "edge":
        handle = item.get("sourceHandle")
        return f"连线 {item.get('source')} → {item.get('target')}" + (f"（分支 {handle}）" if handle else "")
    what = f"字段 {item.get('field')}" if kind == "field" else f"参数 {item.get('key')}"
    value = json.dumps(item.get("online"), ensure_ascii=False, default=repr)
    if len(value) > 120:
        value = value[:117] + "…"
    return f"节点 {item.get('node')} 的{what} 线上取值 {value}"


def _activate(client: FlowClient, fid: str, vid: int, vname: str) -> None:
    try:
        client.activate_version(fid, vid)
    except CliError as err:
        details = {"flow_id": fid, "version_id": vid, "version_name": vname, "platform_details": err.details}
        if err.code == LIVE_WORKFLOW:
            raise CliError(
                f"版本 {vid}（{vname}）已建好，但工作流 {fid} 处于上线状态，平台不允许切换当前版本——这次推送没有生效",
                exit_code=EXIT_PRECHECK_FAILED,
                code=err.code,
                next_step=f"先在平台上下线该工作流，再到版本列表把版本 {vid} 设为当前版本（或下线后重跑 push）。",
                platform_message=err.platform_message,
                details=details,
                hints=err.hints,
            ) from err
        raise CliError(
            f"版本 {vid}（{vname}）已建好，但切换为当前版本失败：{err.message}",
            exit_code=err.exit_code,
            code=err.code,
            next_step=err.next_step,
            platform_message=err.platform_message,
            details=details,
            hints=err.hints,
        ) from err


# ---- run -------------------------------------------------------------------------------


def _run(args: Any, emitter: Emitter) -> int:
    token = _decode_resume(args.resume) if args.resume else None
    one_shot = _one_shot_input(args)
    if token is not None and one_shot is None:
        raise CliError(
            "续跑需要给出输入",
            exit_code=EXIT_USAGE,
            next_step="带上 --input <文本>、--input-file <文件> 或 --input-json <对象> 之一。",
        )
    if not Path(args.spec).is_file():
        raise CliError(
            f"声明文件不存在：{args.spec}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="run 用声明文件定位它绑定的工作流（.bisheng/flow.json）；检查路径。",
        )
    profile = credentials.load_selected(args)
    fid = _run_target(args, profile, token)

    session: str | None = None
    message: int | None = None
    inputs: dict[str, Any] | None = None
    used = False
    if token is not None:
        session, message = token["session_id"], int(token["message_id"])
        inputs = _inputs_for(token["node_id"], token["fields"], one_shot, token=args.resume)
        used = True

    printer = _EventPrinter(emitter)
    with _platform(args, emitter, profile) as platform:
        client = FlowClient(platform)
        while True:
            round_ = client.invoke(
                fid,
                session_id=session,
                message_id=message,
                inputs=inputs,
                read_timeout=args.wait_timeout,
                on_event=printer,
            )
            session = round_["session_id"]
            _raise_on_failure(round_["events"], session)
            pause = _pause(round_["events"])
            if pause is None:
                if not any(e.get("event") == "close" for e in round_["events"]):
                    raise CliError(
                        "平台没有给出运行结束事件：这一轮既没有正常结束、也没有停在等待输入，结果未知",
                        exit_code=EXIT_DEFECT,
                        next_step="到平台上查看这次会话的实际状态；持续出现请带上 --json 输出报平台故障。",
                        details={"session_id": session, "events": len(round_["events"])},
                    )
                break
            if not session:
                raise CliError(
                    "平台停在等待输入，但没有给出会话标识，这次运行无法续跑",
                    exit_code=EXIT_DEFECT,
                    next_step="这是平台与 CLI 之间的契约缺陷：请带上 --json 输出报平台故障。",
                    details={"node_id": pause.get("node_id"), "message_id": pause.get("message_id")},
                )
            fields = _fields(pause)
            resume = _encode_resume(fid, session, pause, fields)
            if one_shot is not None and not used:
                inputs = _inputs_for(pause["node_id"], [f["key"] for f in fields], one_shot, token=resume)
                used = True
            elif _interactive(emitter):
                inputs = {pause["node_id"]: _prompt_fields(pause, fields)}
            else:
                return _report_waiting(args, emitter, fid, session, pause, fields, resume, used)
            message = _message_id(pause)

    if one_shot is not None and not used:
        emitter.warn("给出的输入未使用：这次运行没有停在等待输入的节点。")
    emitter.info(f"运行完成（会话 {session}）。")
    data = {"flow_id": fid, "status": "finished", "session_id": session, "input_used": used if one_shot else None}
    emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data=data)
    return EXIT_OK


def _run_target(args: Any, profile: credentials.Profile, token: dict[str, Any] | None) -> str:
    if token is not None and not args.flow_id and binding.read_binding(args.spec, profile.base_url) is None:
        return token["workflow_id"]
    fid, _ = _target(args, profile)
    if token is not None and token["workflow_id"] != fid:
        raise CliError(
            f"续跑凭据属于工作流 {token['workflow_id']}，不是 {fid}",
            exit_code=EXIT_USAGE,
            next_step=f"续跑只能接着原来的会话：用 --flow-id {token['workflow_id']} 指向凭据所属的工作流。",
        )
    return fid


def _one_shot_input(args: Any) -> str | dict[str, Any] | None:
    if args.input is not None:
        return args.input
    if args.input_file is not None:
        path = Path(args.input_file)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError(
                f"无法读取输入文件 {path}（{exc.__class__.__name__}）",
                exit_code=EXIT_LOCAL_INVALID,
                next_step="确认文件存在且是 UTF-8 文本。",
            ) from None
    if args.input_json is not None:
        try:
            value = json.loads(args.input_json)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise CliError(
                "--input-json 必须是一个 JSON 对象（字段名 → 取值）",
                exit_code=EXIT_USAGE,
                next_step='例如 --input-json \'{"symbol": "EB", "period": "15m"}\'。',
            )
        return value
    return None


def _inputs_for(node_id: str, keys: list[str], value: str | dict[str, Any], *, token: str) -> dict[str, Any]:
    """The ``input`` body for one waiting node: ``{node_id: {field: value}}``."""
    if isinstance(value, dict):
        return {node_id: value}
    if len(keys) == 1:
        return {node_id: {keys[0]: value}}
    raise CliError(
        f"节点 {node_id} 等待 {len(keys)} 个字段（{'、'.join(keys) or '无'}），一段文本填不了",
        exit_code=EXIT_USAGE,
        next_step=f"改用 --input-json 按字段给值，并用 --resume {token} 接着这次会话。",
        details={"node_id": node_id, "fields": keys, "resume": token},
    )


def _pause(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    waiting = [e for e in events if e.get("event") in WAITING_EVENTS]
    return waiting[-1] if waiting else None


def _fields(pause: dict[str, Any]) -> list[dict[str, Any]]:
    schema = pause.get("input_schema") if isinstance(pause.get("input_schema"), dict) else {}
    items = schema.get("value") if isinstance(schema.get("value"), list) else []
    keyed = [item for item in items if isinstance(item, dict) and isinstance(item.get("key"), str)]
    if schema.get("input_type") != _FORM_INPUT:
        # One field: the text item the platform keys the input on (sent first today; picked
        # by type so the attachments' order never decides what --input fills).
        text = [item for item in keyed if item.get("type") == "text"]
        keyed = (text or keyed)[:1]
    fields = []
    for item in keyed:
        fields.append(
            {
                "key": item["key"],
                "label": item.get("label"),
                "type": item.get("type"),
                "required": bool(item.get("required")),
                "options": item.get("options"),
            }
        )
    return fields


def _message_id(pause: dict[str, Any]) -> int:
    raw = pause.get("message_id")
    text = str(raw) if raw is not None else ""
    if not (text.isascii() and text.isdecimal()):
        raise CliError(
            f"平台的等待输入事件没有可用的消息标识（{raw!r}）",
            exit_code=EXIT_DEFECT,
            next_step="这是平台与 CLI 之间的契约缺陷：请带上本条输出报平台故障。",
        )
    return int(text)


def _prompt_fields(pause: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    _message_id(pause)  # refuse before asking, not after
    values: dict[str, Any] = {}
    for field in fields:
        options = field.get("options")
        hint = f"（可选：{'、'.join(map(str, options))}）" if isinstance(options, list) and options else ""
        values[field["key"]] = _ask(f"{field.get('label') or field['key']}{hint}: ")
    return values


def _report_waiting(
    args: Any,
    emitter: Emitter,
    fid: str,
    session: str | None,
    pause: dict[str, Any],
    fields: list[dict[str, Any]],
    resume: str,
    used: bool,
) -> int:
    _message_id(pause)
    keys = [f["key"] for f in fields]
    name = pause.get("node_name")
    emitter.info(
        f"工作流停在节点 {pause.get('node_id')}"
        + (f"（{name}）" if name else "")
        + f"，等待输入：{'、'.join(keys) or '无字段'}"
    )
    flag = "--input '<内容>'" if len(keys) == 1 else '--input-json \'{"字段": "取值"}\''
    emitter.info(f"续跑：bisheng flow run {args.spec} --resume {resume} {flag}")
    emitter.result(
        COMMAND,
        ok=True,
        exit_code=EXIT_OK,
        data={
            "flow_id": fid,
            "status": "waiting_input",
            "session_id": session,
            "message_id": pause.get("message_id"),
            "node_id": pause.get("node_id"),
            "node_name": name,
            "fields": fields,
            "resume": resume,
            "input_used": used,
        },
    )
    return EXIT_OK


def _raise_on_failure(events: list[dict[str, Any]], session: str | None) -> None:
    """A failed run names the node and the reason (AC-32), or says the platform named none."""
    failed = [e for e in events if e.get("event") == "node_run" and _text(e)]
    closing = [e for e in events if e.get("event") == "close" and isinstance(_message(e), dict)]
    error = _message(closing[-1]) if closing else None
    if not failed and not (isinstance(error, dict) and error.get("status_code") not in (None, 200)):
        return
    node = failed[-1] if failed else None
    if node is not None and _text(node) == STOPPED_BY_USER:
        raise CliError(
            f"运行已被停止（停在节点 {node.get('node_id')}）",
            exit_code=EXIT_CANCELLED,
            next_step="有人在平台上停止了这次运行；需要时重新运行。",
            details={"session_id": session, "node_id": node.get("node_id"), "node_name": node.get("node_name")},
        )
    code = error.get("status_code") if isinstance(error, dict) else None
    data = error.get("data") if isinstance(error, dict) and isinstance(error.get("data"), dict) else {}
    reason = _text(node) if node else str(data.get("exception") or (error or {}).get("status_message") or "")
    if node is not None:
        name = node.get("node_name")
        message = f"运行失败：节点 {node.get('node_id')}" + (f"（{name}）" if name else "") + f" 出错：{reason}"
    else:
        message = f"运行失败：{reason or '平台没有给出原因'}（平台没有给出失败节点）"
    raise CliError(
        message,
        exit_code=EXIT_PRECHECK_FAILED,
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        next_step="按原因修正声明或节点配置（必要时到平台上看这次会话的运行日志），push 后重新运行。",
        platform_message=str((error or {}).get("status_message") or ""),
        details={
            "session_id": session,
            "node_id": node.get("node_id") if node else None,
            "node_name": node.get("node_name") if node else None,
            "reason": reason,
            "platform": error,
        },
    )


def _message(event: dict[str, Any]) -> Any:
    schema = event.get("output_schema")
    return schema.get("message") if isinstance(schema, dict) else None


def _text(event: dict[str, Any] | None) -> str:
    message = _message(event) if event else None
    if message is None:
        return ""
    return message if isinstance(message, str) else json.dumps(message, ensure_ascii=False, default=repr)


class _EventPrinter:
    """Shows each event as it arrives: one ``flow_event`` line under ``--json``, a line of text for people."""

    def __init__(self, emitter: Emitter) -> None:
        self._emitter = emitter

    def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "stream_msg" and event.get("status") != "end":
            return  # token chunks; the end event carries the whole text
        self._emitter.event(COMMAND, "flow_event", type=kind, data=event)
        who = event.get("node_name") or event.get("node_id") or ""
        text = _text(event)
        if kind in ("guide_word", "guide_question"):
            self._emitter.info(f"[引导] {text}")
        elif kind in ("output_msg", "stream_msg", "output_with_input_msg", "output_with_choose_msg"):
            self._emitter.info(f"[输出 {who}] {text}" if who else f"[输出] {text}")
        elif kind == "node_run" and text:
            self._emitter.error(f"[失败] 节点 {event.get('node_id')}（{event.get('node_name') or ''}）：{text}")


def _encode_resume(fid: str, session: str | None, pause: dict[str, Any], fields: list[dict[str, Any]]) -> str:
    payload = {
        "w": fid,
        "s": session,
        "m": str(pause.get("message_id")),
        "n": pause.get("node_id"),
        "f": [f["key"] for f in fields],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _RESUME_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_resume(token: str) -> dict[str, Any]:
    bad = CliError(
        "续跑凭据无法识别",
        exit_code=EXIT_USAGE,
        next_step="原样复制上一次 bisheng flow run 输出里 --resume 后面的那一串。",
    )
    if not token.startswith(_RESUME_PREFIX):
        raise bad
    body = token[len(_RESUME_PREFIX) :]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(payload, dict):
        raise bad
    session, message, node, fields = payload.get("s"), payload.get("m"), payload.get("n"), payload.get("f")
    if not (
        isinstance(session, str)
        and session
        and isinstance(message, str)
        and message.isascii()
        and message.isdecimal()
        and isinstance(node, str)
        and node
        and isinstance(fields, list)
        and all(isinstance(f, str) for f in fields)
    ):
        raise bad
    try:
        workflow = parse_flow_id(payload.get("w"))
    except CliError:
        raise bad from None
    return {"workflow_id": workflow, "session_id": session, "message_id": message, "node_id": node, "fields": fields}


# ---- tool ------------------------------------------------------------------------------


def _tool(args: Any, emitter: Emitter) -> int:
    sub = getattr(args, "tool_command", None)
    if sub == "register":
        return _tool_register(args, emitter)
    if sub == "list":
        return _tool_list(args, emitter)
    emitter.error("用法: bisheng flow tool {register <openapi 文件>,list}")
    return EXIT_USAGE


def _tool_register(args: Any, emitter: Emitter) -> int:
    path = Path(args.openapi)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(
            f"无法读取 OpenAPI 描述文件 {path}（{exc.__class__.__name__}）",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="确认路径正确、文件是 UTF-8 文本（YAML 或 JSON）。",
        ) from None
    profile = credentials.load_selected(args)
    with _platform(args, emitter, profile) as platform:
        client = FlowClient(platform)
        draft = client.parse_tool_schema(content)
        row = client.register_tool(_registration(draft, name=args.name))

    operations = [
        {"id": c.get("id"), "name": c.get("name"), "tool_key": c.get("tool_key")}
        for c in row.get("children") or []
        if isinstance(c, dict)
    ]
    tool_type = {"id": row.get("id"), "name": row.get("name")}
    if not operations:
        raise CliError(
            f"工具类型 {tool_type['name']} 的注册回复里没有任何操作，无法确认注册结果可用",
            exit_code=EXIT_DEFECT,
            next_step="到平台的工具页核对这次注册；OpenAPI 里没有 operationId 的接口不会成为可用操作。",
            details={"tool_type": tool_type},
        )
    emitter.info(f"工具类型 {tool_type['name']}（id {tool_type['id']}）已注册，{len(operations)} 个操作：")
    for op in operations:
        emitter.info(f"  {tool_type['name']}/{op['name']}  tool_key={op['tool_key']}")
    emitter.info("工作流声明里用 tool: <操作名> 引用（重名时写 <工具类型>/<操作名>）。")
    emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data={"tool_type": tool_type, "operations": operations})
    return EXIT_OK


def _registration(draft: dict[str, Any], *, name: str | None) -> dict[str, Any]:
    """The parse draft narrowed to what the register face accepts, unset fields left unset."""
    body = {k: draft[k] for k in _TOOL_TYPE_KEYS if draft.get(k) is not None}
    if name:
        body["name"] = name
    body["children"] = [
        {k: child[k] for k in _TOOL_KEYS if child.get(k) is not None}
        for child in draft.get("children") or []
        if isinstance(child, dict)
    ]
    return body


def _tool_list(args: Any, emitter: Emitter) -> int:
    profile = credentials.load_selected(args)
    with _platform(args, emitter, profile) as platform:
        rows = FlowClient(platform).tools()
    listed = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "description": row.get("description"),
            "operations": [
                {"id": c.get("id"), "name": c.get("name"), "tool_key": c.get("tool_key"), "desc": c.get("desc")}
                for c in row.get("children") or []
            ],
        }
        for row in rows
    ]
    emitter.info(f"可用工具类型 {len(listed)} 个：")
    for tool_type in listed:
        emitter.info(f"{tool_type['name']}（id {tool_type['id']}）")
        for op in tool_type["operations"]:
            emitter.info(f"  {tool_type['name']}/{op['name']}  tool_key={op['tool_key']}")
    emitter.result(COMMAND, ok=True, exit_code=EXIT_OK, data={"tools": listed})
    return EXIT_OK
