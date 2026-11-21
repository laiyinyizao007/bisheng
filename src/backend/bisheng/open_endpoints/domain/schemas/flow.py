"""Stable request contracts for the workflow-as-code write face (F005).

The CLI pushes a workflow definition; the request bodies here are the wire
contract it compiles against. They are deliberately **narrower** than the v1
schemas they mirror:

* no ``user_id`` / ``flow_id`` — AC-42: an open-API write never designates
  whose name it acts in; the identity comes from the credential's resource
  owner only.
* no ``flow_type`` — this face writes **workflow** versions, full stop. The
  value is fixed by the service call, not by the request.
* ``extra="forbid"`` — a silently ignored unknown field is a silently ignored
  typo, and the CLI compiles against exactly these fields.

The ``data`` graph check is **delegated** to the v1 models' own validators (one
rule set, no copy). It must run here, at the request boundary: the endpoint
later builds ``FlowCreate`` / ``FlowVersionCreate`` in its body, and a pydantic
``ValidationError`` raised there is not a ``RequestValidationError`` — it would
surface as a 500 instead of a request error.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bisheng.database.models.flow import FlowCreate
from bisheng.database.models.flow_version import FlowVersionBase


class OpenFlowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Workflow name, unique per owner")
    description: str | None = Field(default=None, description="Workflow description")
    data: dict | None = Field(default=None, description="Workflow graph data (nodes/edges/viewport)")
    logo: str | None = Field(default=None, description="Workflow logo")

    @field_validator("data")
    @classmethod
    def _graph_has_nodes_and_edges(cls, v):
        return FlowCreate.validate_json(v)


class OpenFlowVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, description="Version name")
    description: str | None = Field(default=None, description="Version description")
    data: dict | None = Field(default=None, description="Workflow graph data (nodes/edges/viewport)")
    original_version_id: int | None = Field(default=None, description="Source version this one was copied from")

    @field_validator("data")
    @classmethod
    def _graph_has_nodes_and_edges(cls, v):
        return FlowVersionBase.validate_json(v)


class OpenToolSchemaParseRequest(BaseModel):
    """Parse an OpenAPI document the caller already holds.

    No ``download_url`` (v1 has one): fetching an arbitrary URL on behalf of a
    machine key is an SSRF surface (no scheme / private-range / timeout guard in
    the shared fetch helper), and the CLI reads a local file anyway.
    """

    model_config = ConfigDict(extra="forbid")

    file_content: str = Field(min_length=1, description="Raw OpenAPI document content")


class OpenToolCreate(BaseModel):
    """One tool (an API operation) inside a registration request.

    No ``type`` / ``tool_key`` ownership fields beyond what the draft carries:
    the DAO assigns ``type`` and derives ``tool_key``; ``user_id`` /
    ``tenant_id`` / ``is_preset`` / ``is_delete`` are stamped by the service.
    """

    model_config = ConfigDict(extra="forbid")

    # Column widths: ``name`` String(125), ``logo`` String(512) — over-length is a request error, not a DB 500.
    name: str = Field(min_length=1, max_length=125, description="Operation name (operationId)")
    desc: str | None = Field(default=None, description="Operation description")
    logo: str | None = Field(default=None, max_length=512, description="Tool logo")
    tool_key: str | None = Field(default=None, description="Draft tool key; the server re-derives it on insert")
    api_params: list[dict] | None = Field(default=None, description="Operation parameters")
    extra: str | dict | None = Field(default=None, description="Operation extras (method, path, ...)")


class OpenToolTypeCreate(BaseModel):
    """Register a tool type with its tools — the v2 write body.

    Narrower than v1's ``GptsToolsTypeRead`` body: no ``id`` / ``user_id`` /
    ``tenant_id`` / ``is_shared`` / ``is_delete`` / ``extra``. v1's service
    overwrites some of those but not ``is_shared`` (tenant-wide sharing root)
    nor the children's ``tenant_id``; a new face does not inherit that gap.

    Optional fields default to ``None`` and the endpoint dumps with
    ``exclude_unset`` — the v1 model's own defaults apply, none are copied here.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Tool type name, unique per owner")
    description: str | None = Field(default=None, description="Tool type description")
    logo: str | None = Field(default=None, description="Tool type logo")
    server_host: str | None = Field(default=None, description="Base URL the tools call")
    auth_method: int | None = Field(default=None, description="Authentication method")
    api_key: str | None = Field(
        default=None, max_length=1000, description="API key used when auth_method requires one (v1 cap)"
    )
    auth_type: str | None = Field(default=None, description="Authentication type")
    # Only custom OpenAPI tools (``ToolPresetType.API`` = 0) on this face: preset (1) is refused anyway, and
    # MCP (2) makes the server connect to a caller-chosen address — out of scope for workflow-as-code.
    is_preset: Literal[0] | None = Field(default=None, description="Tool type kind: 0 = custom OpenAPI tool")
    openapi_schema: str | None = Field(default=None, description="OpenAPI / MCP schema the tools were parsed from")
    parameter_name: str | None = Field(default=None, description="Custom auth header/query parameter name")
    api_location: str | None = Field(default=None, description="Where the custom parameter goes: header or query")
    children: list[OpenToolCreate] = Field(default_factory=list, description="Tools under this type")
