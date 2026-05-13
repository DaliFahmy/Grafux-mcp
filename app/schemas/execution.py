"""
app/schemas/execution.py — Pydantic v2 schemas for tool execution.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ExecuteRequest(BaseModel):
    tool_name: str = Field(..., min_length=1)
    tool_id: uuid.UUID | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    project_id: uuid.UUID | None = None
    # Legacy v1 fields for backward compatibility
    username: str | None = None
    project: str | None = None
    category: str | None = None
    output_port_paths: list[str] = Field(default_factory=list)
    timeout: float = Field(default=120.0, ge=1.0, le=600.0)


class AsyncExecuteRequest(ExecuteRequest):
    """Same as ExecuteRequest but always returns an invocation_id immediately."""


class InvocationResponse(BaseModel):
    id: uuid.UUID
    tool_name: str
    server_type: str
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class AsyncInvocationStarted(BaseModel):
    invocation_id: uuid.UUID
    status: str = "pending"


class ToolContent(BaseModel):
    type: str
    text: str | None = None


class ToolCallResult(BaseModel):
    content: list[ToolContent]
    isError: bool = False
    diagnostics: dict[str, Any] | None = None
    output_files: list[dict[str, Any]] | None = None
