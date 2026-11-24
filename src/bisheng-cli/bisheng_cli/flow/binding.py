"""Where a declaration's workflow lives, and what this machine last pushed to it.

Two files under ``.bisheng/`` next to the declaration, with opposite rules.

**``.bisheng/flow.json`` — the binding (committed).** Shaped like
``.bisheng/app.json`` (``project.py``): per normalised platform address, per
declaration file name, the workflow id. A team that shares the repository shares
the binding, so ``flow diff`` / ``push`` / ``run`` reach the same workflow
without anyone passing ``--flow-id``. It holds identifiers only — never a
definition, never anything about a credential.

It sits next to the declaration rather than in the current directory: the
declaration path is always explicit on the command line, the working directory
is not, and a binding that moves with ``cd`` binds the wrong workflow.

**``.bisheng/cache/last_push.json`` — the last push (this checkout, not committed).**
The definition last pushed from this checkout to each workflow. ``push`` uses
it as the common ancestor when it decides what "only the platform has" (AC-26):
a value that differs online only because the YAML moved on since the last push
is not somebody's web edit. The record is never trusted on its own: it counts
only while it is *semantically identical* to what the platform serves now, so a
stale or damaged record can only make push ask again. A record deliberately
written to equal the online content does skip the question — which grants
nothing beyond ``--allow-drop-online-only``, open to anyone who can write the
project anyway; this is a guard against surprise, not against the local
user. Without the record — a fresh
clone, another machine — every online-only value is treated as an edit. The
cache directory ignores itself in git, like the template cache beside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from bisheng_cli.credentials import normalise_base_url
from bisheng_cli.errors import EXIT_LOCAL_INVALID, EXIT_USAGE, CliError

STATE_DIR = ".bisheng"
FLOW_REF_NAME = "flow.json"
FLOW_REF_VERSION = 1
CACHE_DIR = "cache"
TEMPLATE_CACHE_NAME = "node_templates.json"
LAST_PUSH_NAME = "last_push.json"
_LAST_PUSH_FORMAT = 1
#: A last-push file larger than this is ignored rather than parsed.
_LAST_PUSH_MAX_BYTES = 64 * 1024 * 1024


def state_dir(spec_path: str | Path) -> Path:
    return Path(spec_path).resolve().parent / STATE_DIR


def cache_dir(spec_path: str | Path) -> Path:
    return state_dir(spec_path) / CACHE_DIR


def template_cache_path(spec_path: str | Path) -> Path:
    return cache_dir(spec_path) / TEMPLATE_CACHE_NAME


# ---- .bisheng/flow.json -----------------------------------------------------------


def _ref_path(spec_path: str | Path) -> Path:
    return state_dir(spec_path) / FLOW_REF_NAME


def _read_store(spec_path: str | Path) -> dict[str, Any]:
    path = _ref_path(spec_path)
    if not path.is_file():
        return {"version": FLOW_REF_VERSION, "flows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CliError(
            f"{path} 无法解析：{exc.__class__.__name__}",
            exit_code=EXIT_LOCAL_INVALID,
            next_step=f"修正或删除 {path} 后，用 --flow-id 显式指定工作流重跑（成功后会重新写入绑定）。",
        ) from None
    if not isinstance(data, dict) or not isinstance(data.get("flows"), dict):
        return {"version": FLOW_REF_VERSION, "flows": {}}
    return data


def read_binding(spec_path: str | Path, base_url: str) -> dict[str, Any] | None:
    per_platform = _read_store(spec_path)["flows"].get(normalise_base_url(base_url))
    entry = per_platform.get(Path(spec_path).name) if isinstance(per_platform, dict) else None
    return entry if isinstance(entry, dict) and isinstance(entry.get("flow_id"), str) else None


def save_binding(spec_path: str | Path, base_url: str, flow_id: str, *, name: str | None = None) -> None:
    store = _read_store(spec_path)
    key = normalise_base_url(base_url)
    per_platform = store["flows"].get(key)
    if not isinstance(per_platform, dict):
        per_platform = store["flows"][key] = {}
    entry = per_platform.get(Path(spec_path).name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.update(flow_id=flow_id, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if name is not None:
        entry["name"] = name
    per_platform[Path(spec_path).name] = entry
    store["version"] = FLOW_REF_VERSION
    path = _ref_path(spec_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Committed file: a torn write would break every flow command until fixed by hand.
    _replace(path, json.dumps(store, ensure_ascii=False, indent=2) + "\n")


def resolve_flow_id(spec_path: str | Path, base_url: str, explicit: str | None) -> tuple[str, bool]:
    """``(flow_id, came_from_the_command_line)``. Explicit wins; then the binding; else a usage error."""
    if explicit:
        return explicit, True
    entry = read_binding(spec_path, base_url)
    if entry:
        return entry["flow_id"], False
    raise CliError(
        "无法确定目标工作流",
        exit_code=EXIT_USAGE,
        next_step=(
            f"用 --flow-id 指定工作流 id；还没有工作流就用 bisheng flow push {Path(spec_path).name} --create 新建。"
            f"成功一次后 id 会记在 {STATE_DIR}/{FLOW_REF_NAME}，之后可省略。"
        ),
    )


# ---- .bisheng/cache/last_push.json -------------------------------------------------


def _push_key(base_url: str, flow_id: str) -> str:
    digest = hashlib.sha256(normalise_base_url(base_url).encode("utf-8")).hexdigest()
    return f"{digest}:{flow_id}"


def _last_push_path(spec_path: str | Path) -> Path:
    return cache_dir(spec_path) / LAST_PUSH_NAME


def _read_last_push_store(spec_path: str | Path) -> dict[str, Any]:
    path = _last_push_path(spec_path)
    try:
        if path.stat().st_size > _LAST_PUSH_MAX_BYTES:
            return {}
        store = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(store, dict) or store.get("format") != _LAST_PUSH_FORMAT:
        return {}
    entries = store.get("entries")
    return entries if isinstance(entries, dict) else {}


def read_last_push(spec_path: str | Path, base_url: str, flow_id: str) -> dict[str, Any] | None:
    entry = _read_last_push_store(spec_path).get(_push_key(base_url, flow_id))
    definition = entry.get("definition") if isinstance(entry, dict) else None
    if isinstance(definition, dict) and isinstance(definition.get("nodes"), list):
        return definition
    return None


def save_last_push(
    spec_path: str | Path, base_url: str, flow_id: str, definition: dict[str, Any], *, version_id: int | None
) -> bool:
    """Record a push. Best effort: a record that cannot be written only means more confirmations later."""
    entries = _read_last_push_store(spec_path)
    entries[_push_key(base_url, flow_id)] = {
        "version_id": version_id,
        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "definition": definition,
    }
    directory = cache_dir(spec_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", "utf-8")
        _replace(
            _last_push_path(spec_path),
            json.dumps({"format": _LAST_PUSH_FORMAT, "entries": entries}, ensure_ascii=False),
        )
        return True
    except OSError:
        return False


def _replace(path: Path, text: str) -> None:
    """Write ``text`` to a unique sibling, then ``os.replace`` it in: readers see the old file or the new one."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
