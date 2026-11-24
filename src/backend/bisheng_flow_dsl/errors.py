"""The one exception the declaration rules raise.

The rules are shared by the platform backend and the ``bisheng`` CLI, so the error
says *what* went wrong and *whose* fault it is, never what to type next: the CLI
turns it into its own ``CliError`` (exit code + next step), the platform into
copilot ``issues``. Keep presentation out of here.
"""

from __future__ import annotations

from typing import Any, Literal

#: ``invalid`` — the declaration (or input handed to the rules) is wrong; fix the input.
#: ``defect`` — data that should be platform-shaped is not; report it, do not act on it.
Kind = Literal["invalid", "defect"]


class DslError(Exception):
    """A failure of the declaration rules.

    ``reason`` is a stable machine code callers branch on (``compile_issues``,
    ``platform_shape``, ``online_shape``, ``local_shape``); ``details`` carries the
    structured payload (for ``compile_issues``: ``{"issues": [...]}``).
    """

    def __init__(self, message: str, *, kind: Kind, reason: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind: Kind = kind
        self.reason = reason
        self.details = details
