"""Declaration → YAML text, the one spelling the platform and the CLI both write.

Readable on purpose (it is what a person or a model edits next): Chinese stays
Chinese, multi-line prompts are ``|`` block literals, keys keep the declaration's
order (``name`` → ``nodes`` → ``edges``) instead of being sorted. ``yaml.safe_load``
reads it back to the same value.
"""

from __future__ import annotations

from typing import Any

import yaml


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # PyYAML itself falls back to a quoted style when a block literal cannot hold the
    # text (e.g. trailing spaces), so asking for "|" never produces unreadable output.
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _str)


def dump(declaration: dict[str, Any]) -> str:
    return yaml.dump(
        declaration,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1 << 30,
    )
