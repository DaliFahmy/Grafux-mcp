"""
app/models/invocation.py — Tool execution invocation and log records.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class InvocationStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class MCPInvocation(Base):
    __tablename__ = "mcp_invocations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_sessions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tool_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Human-readable tool reference (may be set even if tool_id is null for local tools)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    server_type: Mapped[str] = mapped_column(String(100), nullable=False, default="local_plugin")

    status: Mapped[InvocationStatus] = mapped_column(
        Enum(InvocationStatus, name="invocation_status_enum"),
        nullable=False,
        default=InvocationStatus.PENDING,
        index=True,
    )
    input: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    logs: Mapped[list[ToolExecutionLog]] = relationship(
        "ToolExecutionLog", back_populates="invocation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_mcp_invocations_org_status", "org_id", "status"),
        Index("ix_mcp_invocations_started_at", "started_at"),
    )


class ToolExecutionLog(Base):
    __tablename__ = "tool_execution_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invocation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_invocations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    invocation: Mapped[MCPInvocation] = relationship("MCPInvocation", back_populates="logs")
