"""
app/schemas/mcp.py — Shared MCP protocol and session schemas.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MCPSessionCreate(BaseModel):
    project_id: uuid.UUID | None = None
    server_ids: list[uuid.UUID] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class MCPSessionResponse(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID | None
    user_id: str | None
    server_ids: list[uuid.UUID]
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class DiscoverRequest(BaseModel):
    server_id: uuid.UUID | None = None
    org_id: uuid.UUID | None = None


class CapabilityInfo(BaseModel):
    server_id: str
    tools: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)


class HealthStatus(BaseModel):
    service: str = "grafux-mcp"
    version: str = "2.0.0"
    status: str = "healthy"
    environment: str
    tools_loaded: int
    redis_connected: bool
    db_connected: bool
    active_connections: int
    active_invocations: int
