# ruff: noqa: RUF001 -- Chinese issue copy with full-width punctuation
"""Credentials on the canvas never reach the model (F007 AC-25).

A canvas can carry secrets in node parameters — the Agent node's SQL connection
password today, a header or key on whatever node comes next. The model needs none of
them to edit the workflow, and the model service is outside the platform. So:

- ``redact(declaration)``: every credential-like value (by key name, at any depth,
  including ``{key: Authorization, value: ...}`` pairs) is replaced by ``REDACTED``
  before the declaration is dumped into the prompt;
- ``restore(spec, base)``: where the answer still says ``REDACTED`` at the same node
  and path, the original value is put back; ``REDACTED`` anywhere else (copied to a
  new node, moved to another key) is an issue fed back to the model — it can never
  land on the canvas as a literal;
- ``mask(value)``: the same rule for what the diff preview shows.

The rule is by name, not by template, so it also covers nodes added later. A false
positive only hides a value from the model; the value itself is restored untouched.
"""

from __future__ import annotations

import copy
import re
from typing import Any

REDACTED = "<已隐藏>"
_MISSING = object()
#: Matched anywhere in the key once case and separators are dropped (``accessToken``,
#: ``x-api-key``, ``password_input``, ``pwd123``). ``tokens`` (as in ``max_tokens``) is a
#: count, not a credential.
_SENSITIVE = re.compile(
    r"password|passwd|pwd|secret|token(?!s)|apikey|accesskey|privatekey|credential|authorization|authheader|cookie"
)
_SEPARATORS = re.compile(r"[\s_\-.]+")


def is_sensitive(key: Any) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE.search(_SEPARATORS.sub("", key).lower()))


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _scrub(value: Any, replacement: Any) -> tuple[Any, int]:
    """``(copy with credential values replaced, how many)``."""
    if isinstance(value, dict):
        out, count = {}, 0
        pair_key = value.get("key")
        for key, item in value.items():
            secret = is_sensitive(key) or (key == "value" and is_sensitive(pair_key))
            if secret and not _blank(item) and not isinstance(item, bool):  # a structure under such a key goes whole
                out[key] = replacement
                count += 1
            else:
                out[key], found = _scrub(item, replacement)
                count += found
        return out, count
    if isinstance(value, list):
        out_list, count = [], 0
        for item in value:
            scrubbed, found = _scrub(item, replacement)
            out_list.append(scrubbed)
            count += found
        return out_list, count
    return value, 0


def redact(declaration: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """The declaration with node parameter credentials replaced by ``REDACTED``."""
    out = copy.deepcopy(declaration)
    total = 0
    for node in out.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("params"), dict):
            node["params"], found = _scrub(node["params"], REDACTED)
            total += found
    return out, total


def mask(value: Any) -> Any:
    """For display: credential values become ``REDACTED`` (a bare credential value is masked by its key upstream)."""
    return _scrub(value, REDACTED)[0]


def restore(spec: dict[str, Any], base: dict[str, Any]) -> list[str]:
    """Put the originals back where the answer kept ``REDACTED`` in place (in ``spec``, in place); issues otherwise."""
    originals = {n.get("id"): n.get("params") for n in base.get("nodes") or [] if isinstance(n, dict)}
    issues: list[str] = []
    for node in spec.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        params = node.get("params")
        if isinstance(params, dict):
            _restore_in(params, originals.get(node_id), node_id, [], issues)
        for key, value in node.items():
            if key != "params" and _contains_redacted(value):
                issues.append(f"节点 {node_id} 的 {key} 里出现了隐藏值 {REDACTED}：隐藏值只能原样留在原来的位置")
    return issues


def _restore_in(container: Any, original: Any, node_id: Any, path: list[Any], issues: list[str]) -> None:
    items = container.items() if isinstance(container, dict) else enumerate(container)
    for key, value in list(items):
        here = _original_at(original, key)
        if (
            key == "value"
            and isinstance(container, dict)
            and "key" in container
            and (not isinstance(original, dict) or original.get("key") != container.get("key"))
        ):
            here = _MISSING  # a {key, value} pair moved or renamed: its secret does not follow
        if isinstance(value, str) and value != REDACTED and REDACTED in value:
            # Rewritten around the marker ("Bearer <已隐藏>"): landing it would replace the
            # real value with this text, and the masked preview could not show the loss.
            where = ".".join(str(p) for p in [*path, key])
            issues.append(f"节点 {node_id} 的 {where} 改写了隐藏值 {REDACTED}：隐藏值只能整体原样保留，不能拼接或改写")
        elif value == REDACTED:
            if here is _MISSING or here == REDACTED:
                where = ".".join(str(p) for p in [*path, key])
                issues.append(
                    f"节点 {node_id} 的 {where} 是隐藏值 {REDACTED}：它只能留在原节点原位置；"
                    "新节点或新位置请留空，由用户自己填写"
                )
            else:
                container[key] = copy.deepcopy(here)
        elif isinstance(value, dict | list):
            _restore_in(value, None if here is _MISSING else here, node_id, [*path, key], issues)


def _original_at(original: Any, key: Any) -> Any:
    if isinstance(original, dict) and key in original:
        return original[key]
    if isinstance(original, list) and isinstance(key, int) and 0 <= key < len(original):
        return original[key]
    return _MISSING


def _contains_redacted(value: Any) -> bool:
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, dict):
        return any(_contains_redacted(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(v) for v in value)
    return False
