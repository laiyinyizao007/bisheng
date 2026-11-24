"""The CLI's test doubles, checked against the server they claim to imitate.

Why this file exists: when the open-API base was swapped from F049 to beta2's
F053, `GET /api/v2/auth/whoami` stopped sending `subject_kind` /
`service_account: {id, name}` / `resource_owner: {user_id, user_name}` and
started sending `actor_kind` / `actor_id` / `actor_name` / `resource_owner:
{user_id}`. Not one of the 254 tests in this suite noticed, because they all
assert against `tests/helpers/platform_mock.py` — and the mock had not moved.
A green suite against a server that does not exist is worse than a red one.

**Why read the server's source instead of importing it.** `pyproject.toml`
budgets this package at two runtime dependencies and describes it as having
"zero coupling to the backend package"; the backend needs fastapi, sqlmodel and
the rest, none of which exist in this project's environment, so
`import bisheng.open_api...` would turn the whole file into a collection error
rather than a check. `ast` parses the declaration without importing anything and
without adding a dependency — the field names are what matters, and they are
right there in the source. The cost is that a field added dynamically (there are
none) would be missed.

If the backend tree is not next to this package, these tests **fail** rather
than skip: a silent skip is the same failure mode this file was written to
close.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

from bisheng_cli import devdb, devproxy
from bisheng_cli.commands.login import resource_owner_user_id
from tests.helpers.platform_mock import OPEN_API_HTTP_STATUS, WHOAMI_FIELDS, whoami_ok

REPO_ROOT = Path(__file__).resolve().parents[3]
WHOAMI_SCHEMA = REPO_ROOT / "src/backend/bisheng/open_api/domain/schemas/credential.py"
OPEN_API_ERRCODES = REPO_ROOT / "src/backend/bisheng/common/errcode/open_api.py"
# `bisheng dev` mirrors these two; INV-32 says the names must be identical.
APP_PROXY_HEADERS = REPO_ROOT / "src/app-proxy/app_proxy/headers.py"
APP_PROXY_CONFIG = REPO_ROOT / "src/app-proxy/app_proxy/config.py"
APP_PROXY_ROUTING = REPO_ROOT / "src/app-proxy/app_proxy/routing.py"
RUNTIME_LIFECYCLE = REPO_ROOT / "src/runtime-manager/runtime_manager/lifecycle.py"
RUNTIME_ENTRYPOINT = REPO_ROOT / "src/runtime-manager/runtime_manager/templates/python3.11/entrypoint.sh.j2"
#: §5 of this document is the one definition of what gets injected into an app.
#: `build_env` implements the part runtime-manager owns; the model face's three
#: names are filled by F055 T056 from the app's runtime credential, so the
#: document is the only place both halves are written down together.
RUNTIME_ENV_CONTRACT = REPO_ROOT / "features/v3.0.0/054-app-domain-runtime/contracts-runtime-manager.md"


def _module(path: Path) -> ast.Module:
    if not path.is_file():
        pytest.fail(
            f"找不到服务端契约源文件 {path}。"
            "这个测试要拿服务端的真实模型来核对 CLI 的测试桩，找不到就等于没核对——"
            "所以它失败而不是跳过。若后端目录被移动，请改这里的路径。"
        )
    return ast.parse(path.read_text(encoding="utf-8"))


def _class_fields(path: Path, name: str) -> list[str]:
    """Annotated field names declared directly on a pydantic model, in order."""
    for node in _module(path).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id != "model_config"
            ]
    pytest.fail(f"{path.name} 里没有 {name} 了——服务端契约被改名或移走，CLI 侧必须跟着改")
    raise AssertionError("unreachable")


def _error_codes(path: Path) -> dict[int, int]:
    """`{Code: http_status}` for every error class declared in the module."""
    codes: dict[int, int] = {}
    for node in _module(path).body:
        if not isinstance(node, ast.ClassDef):
            continue
        values: dict[str, int] = {}
        for stmt in node.body:
            target = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target = stmt.target.id
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target = stmt.targets[0].id
            if target in ("Code", "http_status") and isinstance(stmt.value, ast.Constant):
                values[target] = stmt.value.value
        if "Code" in values:
            codes[values["Code"]] = values.get("http_status", 400)
    return codes


def _module_constant(path: Path, name: str) -> Any:
    """The literal value assigned to a module-level `NAME = <literal>` (or annotated)."""
    for node in _module(path).body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == name and value is not None:
            return ast.literal_eval(_unwrap_call(value))
    pytest.fail(f"{path.name} 里没有模块级常量 {name}——服务端契约被改名或移走，CLI 侧必须跟着改")
    raise AssertionError("unreachable")


def _unwrap_call(value: ast.expr) -> ast.expr:
    """`frozenset({...})` → the set literal inside, so `literal_eval` can read it."""
    if isinstance(value, ast.Call) and value.args and isinstance(value.func, ast.Name):
        return value.args[0]
    return value


RUNTIME_PACKAGE = "runtime_manager"
#: The local both runtime functions build their environment in.
ENV_VAR = "env"


def _imported_function(module: ast.Module, name: str) -> tuple[Path, str] | None:
    """`(source file, original name)` for `name` imported from the runtime package, else None.

    Handles `from runtime_manager.<mod> import f [as g]`, `from runtime_manager import f`,
    and a module that is a package (`<mod>/__init__.py`).
    """
    root = REPO_ROOT / "src/runtime-manager"
    for node in module.body:
        if not (isinstance(node, ast.ImportFrom) and node.module):
            continue
        if node.module != RUNTIME_PACKAGE and not node.module.startswith(RUNTIME_PACKAGE + "."):
            continue
        for alias in node.names:
            if (alias.asname or alias.name) == name:
                base = root / node.module.replace(".", "/")
                source = base.with_suffix(".py") if base.with_suffix(".py").is_file() else base / "__init__.py"
                return source, alias.name
    return None


def _is_env(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == ENV_VAR


def _mentions_env(node: ast.AST) -> bool:
    return any(_is_env(sub) for sub in ast.walk(node) if isinstance(sub, ast.expr))


#: Methods that only read `env`; calling one as a bare statement writes nothing.
_ENV_READS = frozenset({"get", "keys", "values", "items", "copy"})


def _always_set_keys(path: Path, function: str, *, follow: bool) -> list[str]:
    """String keys `function` sets on `env` on every call.

    Recognised writes: `env.update({"K": ...})`, `env["K"] = ...`, `env = dict(...)`
    (the seed), and `env = helper(...)` where `helper` comes from the runtime package
    (read the same way, one hop). Statements nested in `with` / `try` count; anything
    under `if` / `for` / `while` is conditional — the attachment handle, the egress
    proxy, the per-language base-path spellings — and is not part of the
    always-injected contract this CLI mirrors.

    Any **other** unconditional write to `env` fails the test instead of being
    skipped: a shape this parser cannot read must never pass as "no new names".
    So does, even under a condition, a removal (`del env[...]`) or a write whose key
    is not a string literal, and any statement kind the walker does not model
    (`match`, `try/except*`, a nested `def` / `class`) that touches `env`.
    """
    module = _module(path)
    node = next((n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == function), None)
    if node is None:
        pytest.fail(f"{path.name} 里没有函数 {function}——运行期环境契约被挪走，CLI 侧必须跟着改")
    keys: list[str] = []

    def unreadable(stmt: ast.stmt) -> None:
        pytest.fail(
            f"{path.name}:{stmt.lineno} 的 {function} 以本测试读不懂的方式写 env：{ast.unparse(stmt)[:100]}"
            "——确认注入契约是否变了，并让 _always_set_keys 跟上（不要让它静默跳过）"
        )

    def conditional(stmts: list[ast.stmt]) -> None:
        """Names set here are not contract — but removals and dynamic keys still fail."""
        for sub in (n for stmt in stmts for n in ast.walk(stmt)):
            if isinstance(sub, ast.Delete) and any(
                isinstance(t, ast.Subscript) and _is_env(t.value) for t in sub.targets
            ):
                unreadable(sub)
            if (
                isinstance(sub, ast.Assign)
                and any(isinstance(t, ast.Subscript) and _is_env(t.value) for t in sub.targets)
                and not all(
                    isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str)
                    for t in sub.targets
                    if isinstance(t, ast.Subscript) and _is_env(t.value)
                )
            ):
                unreadable(sub)

    def walk(stmts: list[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk(stmt.body)
            elif isinstance(stmt, ast.Try):
                walk(stmt.body + stmt.orelse + stmt.finalbody)
                conditional([h for handler in stmt.handlers for h in handler.body])
            elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
                conditional([stmt])
            elif isinstance(stmt, ast.Return):
                if stmt.value is not None and not _is_env(stmt.value) and _mentions_env(stmt.value):
                    unreadable(stmt)  # e.g. `return {**env, "NEW": ...}`
            elif isinstance(stmt, ast.Delete) and _mentions_env(stmt):
                unreadable(stmt)
            elif not isinstance(stmt, (ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign)):
                if _mentions_env(stmt):
                    unreadable(stmt)  # match / try-except* / nested def or class
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if not (isinstance(call.func, ast.Attribute) and _is_env(call.func.value)):
                    continue  # not a write to env
                if call.func.attr in _ENV_READS:
                    continue
                literal = call.args[0] if len(call.args) == 1 else None
                if (
                    call.func.attr == "update"
                    and not call.keywords
                    and isinstance(literal, ast.Dict)
                    and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in literal.keys)
                ):
                    keys.extend(k.value for k in literal.keys)
                else:
                    unreadable(stmt)
            elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)) and _mentions_env(stmt.target):
                unreadable(stmt)
            elif isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1:
                    if any(_is_env(t) for t in stmt.targets):
                        unreadable(stmt)
                    continue
                target, value = stmt.targets[0], stmt.value
                if isinstance(target, ast.Subscript) and _is_env(target.value):
                    if isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                        keys.append(target.slice.value)
                    else:
                        unreadable(stmt)
                elif _is_env(target):
                    helper = value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else None
                    if helper == "dict" and not keys:
                        continue  # the seed: `env = dict(extra or {})`
                    if helper == "dict" and len(value.args) == 1 and _is_env(value.args[0]) and not value.keywords:
                        continue  # a copy: `env = dict(env)`
                    found = _imported_function(module, helper) if helper and follow else None
                    if found is None:
                        unreadable(stmt)  # includes re-seeding env after names were set
                    keys.extend(_always_set_keys(found[0], found[1], follow=False))

    walk(node.body)
    return keys


def _build_env_keys(path: Path) -> list[str]:
    """The names `build_env` always injects, wherever its body delegates them to."""
    keys = _always_set_keys(path, "build_env", follow=True)
    if not keys:
        pytest.fail(f"{path.name} 的 build_env 读不出任何注入名——运行期环境契约被改写，CLI 侧必须跟着改")
    return keys


def test_whoami_stub_has_exactly_the_fields_the_server_sends() -> None:
    """The mock's payload == `WhoamiResponse`'s fields. No extras, none missing.

    An extra key means the CLI may be reading something the server never sends
    (that is how `service_account.name` kept "working" in tests while login
    printed nothing). A missing key means a field landed that no test covers.
    """
    server_fields = _class_fields(WHOAMI_SCHEMA, "WhoamiResponse")
    payload = json.loads(whoami_ok().content)["data"]

    assert list(WHOAMI_FIELDS) == server_fields
    assert sorted(payload) == sorted(server_fields)


def test_resource_owner_carries_the_one_field_the_cli_reads() -> None:
    # `WhoamiResourceOwner` lost `user_name` in the F049 → beta2 move, which is
    # why `login` prints an id. If a name ever comes back, this goes red and the
    # copy can be improved on purpose rather than by accident.
    assert _class_fields(WHOAMI_SCHEMA, "WhoamiResourceOwner") == ["user_id"]

    payload = json.loads(whoami_ok().content)["data"]
    assert resource_owner_user_id(payload) == payload["resource_owner"]["user_id"]


def test_open_api_error_codes_the_stub_declares_still_exist_with_that_http_status() -> None:
    """Status drift is invisible from inside this suite otherwise.

    `OPEN_API_HTTP_STATUS` decides what the mock puts on the status line, and
    the CLI's fallback ladder keys on exactly that. A code retired server-side,
    or moved from 401 to 403, would leave every test here passing against a
    status the platform stopped sending.
    """
    server = _error_codes(OPEN_API_ERRCODES)
    missing = sorted(code for code in OPEN_API_HTTP_STATUS if code not in server)
    assert not missing, f"服务端已不再定义这些错误码，CLI 桩仍在用：{missing}"

    drifted = {
        code: (declared, server[code]) for code, declared in OPEN_API_HTTP_STATUS.items() if server[code] != declared
    }
    assert not drifted, f"HTTP 状态与服务端不一致（桩值, 服务端值）：{drifted}"


# ---- `bisheng dev` mirrors of app-proxy and runtime-manager (INV-32) ----------


def test_dev_proxy_injects_exactly_app_proxys_ten_headers_in_order() -> None:
    """`devproxy.INJECTED_HEADER_NAMES` == `app_proxy.headers.INJECTED_HEADER_NAMES`.

    The application reads these names; if the hosted proxy adds an eleventh or
    renames one, the local run must follow in the same change, or "works with
    `bisheng dev`, broken hosted" becomes possible again (AC-23).
    """
    assert tuple(devproxy.INJECTED_HEADER_NAMES) == tuple(_module_constant(APP_PROXY_HEADERS, "INJECTED_HEADER_NAMES"))
    assert devproxy.PLATFORM_HEADER_PREFIX == _module_constant(APP_PROXY_HEADERS, "PLATFORM_HEADER_PREFIX")


def test_dev_proxy_strips_the_same_equivalence_class_and_hop_by_hop_set() -> None:
    assert devproxy.DROPPED_HEADERS == frozenset(_module_constant(APP_PROXY_HEADERS, "DROPPED_HEADERS"))
    assert devproxy.PLATFORM_SESSION_COOKIE == _module_constant(APP_PROXY_CONFIG, "ACCESS_TOKEN_COOKIE")


def _app_proxy_functions(*names: str) -> dict[str, Any]:
    """Compile named top-level functions of app-proxy's ``routing.py`` without importing the package.

    ``routing.py`` imports app-proxy's RPC client, which this environment does
    not have. The two path functions only need ``urlsplit``, so they are lifted
    out of the parsed module and compiled on their own.
    """
    from urllib.parse import urlsplit

    module = _module(APP_PROXY_ROUTING)
    nodes = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name in names]
    missing = set(names) - {node.name for node in nodes}
    assert not missing, f"app-proxy routing.py 里找不到 {missing}"
    namespace: dict[str, Any] = {"urlsplit": urlsplit}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP_PROXY_ROUTING), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("location", "upstream"),
    [
        ("/login", ""),
        ("/docs/?a=1#top", ""),
        ("/", ""),
        ("/apps/survey/login", ""),
        ("/apps/survey", ""),
        ("/apps/other/", ""),
        ("next", ""),
        ("//cdn.example.com/x", ""),
        ("https://example.com/login", "http://127.0.0.1:51234"),
        ("http://127.0.0.1:51234/login?next=%2F", "http://127.0.0.1:51234"),
    ],
)
def test_dev_proxy_restores_the_prefix_on_redirects_exactly_like_app_proxy(location: str, upstream: str) -> None:
    """A redirect the app sends must land in the same place locally and hosted.

    The local copy exists because this package cannot import app-proxy; this
    test is what keeps the copy honest.
    """
    hosted = _app_proxy_functions("entry_prefix_for", "restore_entry_prefix")["restore_entry_prefix"]
    assert devproxy.restore_entry_prefix(location, "survey", "/apps", upstream) == hosted(
        location, "survey", "/apps", upstream
    )


def test_dev_proxy_uses_app_proxys_entry_prefix() -> None:
    assert devproxy.ENTRY_PREFIX == _module_constant(APP_PROXY_CONFIG, "DEFAULT_ENTRY_PREFIX")


def test_dev_env_names_equal_the_runtime_managers_build_env() -> None:
    """`devdb.PLATFORM_ENV_NAMES` == the keys `lifecycle.build_env` injects.

    The contract list lives in F054 `contracts-runtime-manager.md` §5 and is
    implemented once, in `build_env`; this is the check that the CLI never grows
    a second definition of it (design §6.2: "不得在 CLI 侧另抄一份定义").
    """
    # Sorted, not ordered: since 4a91ec61da the keys are split between
    # `platform_environment` and a trailing assignment in `build_env`, so source
    # order carries no meaning. A missing, extra, or duplicated name still fails.
    assert sorted(devdb.PLATFORM_ENV_NAMES) == sorted(_build_env_keys(RUNTIME_LIFECYCLE))
    assert tuple(devdb.RESERVED_ENV_PREFIXES) == tuple(_module_constant(RUNTIME_LIFECYCLE, "RESERVED_ENV_PREFIXES"))


def test_model_face_env_names_come_from_the_injection_contract() -> None:
    """`devdb.MODEL_FACE_ENV_NAMES` ⊆ what §5 of the contract declares.

    These three are not in `build_env` — hosted, F055 T056 fills them from the
    application's own runtime credential — so there is no source file to read
    them off. The contract document is what both sides were written against, and
    a name renamed there without the CLI following is exactly the drift that
    makes "works with `bisheng dev`, broken hosted" possible (AC-27 / AC-49).
    """
    if not RUNTIME_ENV_CONTRACT.is_file():
        pytest.fail(f"找不到注入环境变量契约 {RUNTIME_ENV_CONTRACT}；若文档被移动，请改这里的路径。")
    section = RUNTIME_ENV_CONTRACT.read_text(encoding="utf-8").split("## 5. ", 1)
    assert len(section) == 2, "契约文档的第 5 节（注入应用的环境变量）不见了"
    body = section[1].split("\n## ", 1)[0]
    for name in devdb.MODEL_FACE_ENV_NAMES:
        assert f"`{name}`" in body, f"{name} 不在契约 §5 里——CLI 不得自造注入名"
    # And the document still says `dev` is the one that injects them locally; if
    # that sentence goes, this mirror has lost its reason to exist.
    assert "`dev` 期由 F053 同名注入" in body


def test_the_three_model_names_are_not_a_second_copy_of_the_build_env_list() -> None:
    """The two tuples are disjoint, so neither can quietly absorb the other."""
    assert not set(devdb.MODEL_FACE_ENV_NAMES) & set(devdb.PLATFORM_ENV_NAMES)
    assert not set(devdb.MODEL_FACE_ENV_NAMES) & set(_build_env_keys(RUNTIME_LIFECYCLE))


def test_dev_framework_exports_match_the_hosted_entrypoint() -> None:
    # The entrypoint script exports the framework spellings of the base path;
    # `dev` sets the same names so an app needs no per-environment branch.
    if not RUNTIME_ENTRYPOINT.is_file():
        pytest.fail(f"找不到托管入口脚本 {RUNTIME_ENTRYPOINT}；若模板被移动，请改这里的路径。")
    script = RUNTIME_ENTRYPOINT.read_text(encoding="utf-8")
    exported = set(re.findall(r"^export ([A-Z_]+)=", script, re.MULTILINE))
    assert set(devdb.FRAMEWORK_ENV_NAMES) <= exported
    # And the same resolution order for the start command.
    assert (
        script.index("BISHENG_APP_START") < script.index("Procfile") < script.index("main.py") < script.index("app.py")
    )
