"""Load a workflow declaration (``flow.yaml``) — data in, never code (AC-10).

The declaration goes through a ``yaml.SafeLoader`` subclass. ``SafeLoader`` already
refuses ``!!python/object`` and friends; the subclass adds exactly one
constructor, ``!include``, which reads a text file and returns its content. It
does not evaluate, look up classes or import modules — and the included text is
not parsed as YAML either, so a prompt file containing YAML or ``{{#…#}}`` stays
literal. (``yaml.unsafe_load`` / a bare ``yaml.load`` would make reading a
declaration equivalent to running it; that is the thing this module exists to
rule out.)

``!include`` paths resolve against the **declaration's directory**, not the CWD
— otherwise the same project breaks when run from another directory. Paths that
leave the project are not blocked: it is the developer's own workflow reading
the developer's own files, and pretending this is a sandbox would be false
comfort. The boundary is *read, don't execute*.

Local quick checks only (AC-11): the file parses safely, its top level is a
mapping, the required top-level keys exist, and the two list-shaped keys are
lists. No defaults are filled and nothing about node types, wiring or
references is decided here — that is the compiler's and validator's job, and
ultimately the platform's.

This module is the single place the DSL's key names are defined (design §6.5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bisheng_cli.errors import EXIT_LOCAL_INVALID, CliError

#: Top-level keys a declaration must carry. ``edges`` is deliberately absent:
#: a missing wiring is reported by the validator per node (rule #5), which says
#: *which* node is unconnected rather than "edges missing".
REQUIRED_TOP_LEVEL: tuple[str, ...] = ("name", "nodes")
#: Top-level keys that, when present, must be YAML sequences.
LIST_KEYS: tuple[str, ...] = ("nodes", "edges")

INCLUDE_TAG = "!include"


class _IncludeError(Exception):
    """Raised from inside the YAML constructor; turned into a ``CliError`` by ``load_spec``."""

    def __init__(self, reference: str, resolved: Path, reason: str) -> None:
        super().__init__(reason)
        self.reference = reference
        self.resolved = resolved
        self.reason = reason


class _SpecLoader(yaml.SafeLoader):
    """``SafeLoader`` + ``!include``. ``spec_dir`` is set per load (see ``_loader_for``)."""

    spec_dir: Path = Path(".")


def _construct_include(loader: _SpecLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(None, None, f"{INCLUDE_TAG} 只接受一个相对路径字符串", node.start_mark)
    reference = loader.construct_scalar(node)
    resolved = (loader.spec_dir / reference).resolve()
    # Included text is compiled into the definition and sent to the platform on
    # push, so it may only come from the declaration's own directory tree: a
    # declaration taken from a PR must not be able to upload ``~/.ssh/id_rsa``.
    if not resolved.is_relative_to(loader.spec_dir.resolve()):
        raise _IncludeError(reference, resolved, "路径超出了声明文件所在目录（被引入的内容会随 push 发往平台）")
    try:
        return resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise _IncludeError(reference, resolved, "文件不存在")
    except IsADirectoryError:
        raise _IncludeError(reference, resolved, "目标是目录，不是文件")
    except UnicodeDecodeError:
        raise _IncludeError(reference, resolved, "不是 UTF-8 文本")
    except OSError as exc:
        raise _IncludeError(reference, resolved, f"无法读取（{exc.__class__.__name__}）")


_SpecLoader.add_constructor(INCLUDE_TAG, _construct_include)


def _loader_for(spec_dir: Path) -> type[_SpecLoader]:
    # ``yaml.load`` instantiates the Loader class itself, so the per-file base
    # directory travels as a class attribute on a throwaway subclass.
    return type("_BoundSpecLoader", (_SpecLoader,), {"spec_dir": spec_dir})


def load_spec(path: str | Path) -> dict[str, Any]:
    """Read and quick-check a declaration. Returns the mapping exactly as written."""
    spec_path = Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CliError(
            f"声明文件不存在：{spec_path}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="检查路径；声明文件通常是项目根下的 flow.yaml。",
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(
            f"无法读取声明文件 {spec_path}（{exc.__class__.__name__}）",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="确认它是 UTF-8 编码的文本文件。",
        )

    try:
        data = yaml.load(text, Loader=_loader_for(spec_path.resolve().parent))
    except _IncludeError as exc:
        raise CliError(
            f"{INCLUDE_TAG} {exc.reference} 无法展开：{exc.reason}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="路径相对于声明文件所在目录解析；检查文件名与位置。",
            details={"reference": exc.reference, "resolved_path": str(exc.resolved)},
        )
    except yaml.YAMLError as exc:
        raise CliError(
            f"声明文件无法安全解析：{_first_line(exc)}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step=f"声明文件只能包含普通 YAML 数据与 {INCLUDE_TAG}；可执行对象标签（如 !!python/...）一律拒绝。",
            details={"file": str(spec_path)},
        )

    if not isinstance(data, dict):
        raise CliError(
            f"声明文件顶层必须是键值映射，实际是 {_kind(data)}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="顶层写 name: / nodes: / edges: 这样的键值对。",
            details={"file": str(spec_path)},
        )

    missing = [key for key in REQUIRED_TOP_LEVEL if key not in data]
    if missing:
        raise CliError(
            f"声明文件缺少必填顶层字段：{', '.join(missing)}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="补齐后重试。",
            details={"file": str(spec_path), "missing": missing},
        )

    not_lists = [key for key in LIST_KEYS if key in data and not isinstance(data[key], list)]
    if not_lists:
        raise CliError(
            f"顶层字段必须是列表：{', '.join(not_lists)}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step="用 YAML 列表（每项以 - 开头）书写。",
            details={"file": str(spec_path), "not_lists": not_lists},
        )
    return data


def _kind(value: Any) -> str:
    if value is None:
        return "空文档"
    if isinstance(value, list):
        return "列表"
    return type(value).__name__


def _first_line(exc: yaml.YAMLError) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
