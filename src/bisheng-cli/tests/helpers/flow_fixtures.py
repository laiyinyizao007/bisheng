"""Shared inputs for the ``bisheng flow`` local-module tests.

``fixtures/flow/node_templates.json`` is a frozen copy of the backend's
``NODE_TEMPLATES`` (``GET /api/v2/flow/node_templates``). It is a **test fixture
only** — the CLI never ships or falls back to a template copy (design decision
2, AC-05); at runtime templates come from the platform or the command fails.
The backend suite pins this file to the live constant
(``test/workflow/test_node_templates.py::test_cli_fixture_matches_backend_templates``),
so the two cannot drift silently.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

_TEMPLATES = json.loads((Path(__file__).parent.parent / "fixtures" / "flow" / "node_templates.json").read_text("utf-8"))


def templates() -> list[dict[str, Any]]:
    return copy.deepcopy(_TEMPLATES)


def tool_child(name: str, tool_key: str, params: list[str], *, child_id: int = 7) -> dict[str, Any]:
    """One child as ``GET /api/v2/tools`` returns it, ``node_template`` included."""
    return {
        "id": child_id,
        "name": name,
        "tool_key": tool_key,
        "desc": f"{name} 描述",
        "api_params": [{"name": p, "description": "", "required": True} for p in params],
        "node_template": {
            "id": child_id,
            "tool_key": tool_key,
            "type": "tool",
            "name": name,
            "description": f"{name} 描述",
            "group_params": [
                {
                    "name": "工具参数",
                    "params": [
                        {
                            "key": p,
                            "label": p,
                            "type": "var_textarea",
                            "test": "input",
                            "desc": "",
                            "required": True,
                            "value": "",
                        }
                        for p in params
                    ],
                },
                {
                    "name": "输出",
                    "params": [{"global": "key", "key": "output", "label": "output", "type": "var", "value": ""}],
                },
            ],
        },
    }


def tools() -> list[dict[str, Any]]:
    return [
        {
            "id": 3,
            "name": "invest",
            "children": [
                tool_child("submit_candidate", "key_submit", ["name", "source"], child_id=7),
                tool_child("get_feedback", "key_feedback", [], child_id=8),
            ],
        }
    ]


def find_param(node: dict[str, Any], key: str) -> dict[str, Any]:
    for group in node["data"]["group_params"]:
        for param in group["params"]:
            if param["key"] == key:
                return param
    raise KeyError(key)
