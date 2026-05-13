"""
app/schemas/registry.py — Pydantic v2 schemas for MCP server and tool registry.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ServerTypeEnum(str, Enum):
    LOCAL_PLUGIN = "local_plugin"
    REMOTE_SSE = "remote_sse"
    REMOTE_HTTP = "remote_http"
    COMPOSIO = "composio"
    E2B = "e2b"
    DEVICE = "device"


class ServerStatusEnum(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CONNECTING = "connecting"
    ERROR = "error"


# ── Requests ──────────────────────────────────────────────────────────────────

class RegisterServerRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-z0-9-_]+$")
    server_type: ServerTypeEnum
    transport: str = Field(default="http", max_length=50)
    endpoint_url: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class UpdateServerRequest(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    config: dict[str, Any] | None = None
    status: ServerStatusEnum | None = None


# ── Responses ─────────────────────────────────────────────────────────────────

class ServerResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    server_type: ServerTypeEnum
    transport: str
    endpoint_url: str | None
    config: dict[str, Any]
    status: ServerStatusEnum
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ToolResponse(BaseModel):
    id: uuid.UUID
    server_id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID | None
    name: str
    description: str
    input_schema: dict[str, Any]
    tool_type: str
    category: str | None
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ServerHealthResponse(BaseModel):
    server_id: str
    status: str
    latency_ms: float | None = None
    checked_at: datetime | None = None
    error: str | None = None
