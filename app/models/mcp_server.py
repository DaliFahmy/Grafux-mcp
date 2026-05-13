"""
app/models/mcp_server.py — MCP server registry and connection models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.models.mcp_tool import MCPTool


class ServerType(str, PyEnum):
    LOCAL_PLUGIN = "local_plugin"
    REMOTE_SSE = "remote_sse"
    REMOTE_HTTP = "remote_http"
    COMPOSIO = "composio"
    E2B = "e2b"
    DEVICE = "device"


class ServerStatus(str, PyEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    CONNECTING = "connecting"
    ERROR = "error"


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    server_type: Mapped[ServerType] = mapped_column(
        Enum(ServerType, name="server_type_enum"), nullable=False
    )
    transport: Mapped[str] = mapped_column(String(50), nullable=False, default="http")
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[ServerStatus] = mapped_column(
        Enum(ServerStatus, name="server_status_enum"),
        nullable=False,
        default=ServerStatus.INACTIVE,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    tools: Mapped[list[MCPTool]] = relationship(
        "MCPTool", back_populates="server", cascade="all, delete-orphan"
    )
    connections: Mapped[list[MCPConnection]] = relationship(
        "MCPConnection", back_populates="server", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_mcp_servers_org_slug", "org_id", "slug", unique=True),
    )


class MCPConnection(Base):
    __tablename__ = "mcp_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mcp_servers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="disconnected")
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    server: Mapped[MCPServer] = relationship("MCPServer", back_populates="connections")
