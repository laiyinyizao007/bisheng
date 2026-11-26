"""Workflow declaration language shared by the platform backend and the ``bisheng`` CLI (F007 design decision 1).

One copy of the compile / validate / diff / decompile rules. The backend imports it
directly (like ``bisheng_langchain``); the CLI wheel bundles it via hatch force-include.
Standard library and PyYAML only: never import ``bisheng``, ``bisheng_cli`` or ``bisheng_langchain`` here.
"""

PACKAGE = "bisheng_flow_dsl"
