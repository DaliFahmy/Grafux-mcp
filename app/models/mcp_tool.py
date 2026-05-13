"""
app/models/mcp_tool.py — Tool registry, permissions, and org/project assignments.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base

if TYPE_CHECKING:
    from app.models.mcp_server import MCPServer


class ToolStatus(str, PyEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DEPRECATED = "deprecated"


class MCPTool(Base):
    __tablename__ = "mcp_tools"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    tool_type: Mapped[str] = mapped_column(String(100), nullable=False, default="generic")
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[ToolStatus] = mapped_column(
        Enum(ToolStatus, name="tool_status_enum"),
        nullable=False,
        default=ToolStatus.AVAILABLE,
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

    server: Mapped[MCPServer] = relationship("MCPServer", back_populates="tools")
    permissions: Mapped[list[MCPToolPermission]] = relationship(
        "MCPToolPermission", back_populates="tool", cascade="all, delete-orphan"
    )
    org_assignments: Mapped[list[OrgMCPTool]] = relationship(
        "OrgMCPTool", back_populates="tool", cascade="all, delete-orphan"
    )
    project_assignments: Mapped[list[ProjectMCPTool]] = relationship(
        "ProjectMCPTool", back_populates="tool", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_mcp_tools_org_server_name", "org_id", "server_id", "name", unique=True),
    )


class MCPToolPermission(Base):
    __tablename__ = "mcp_tool_permissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    allowed_roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    tool: Mapped[MCPTool] = relationship("MCPTool", back_populates="permissions")


class OrgMCPTool(Base):
    """Org-level tool enablement and configuration."""

    __tablename__ = "organization_mcp_tools"

    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    tool: Mapped[MCPTool] = relationship("MCPTool", back_populates="org_assignments")


class ProjectMCPTool(Base):
    """Project-level tool enablement and configuration."""

    __tablename__ = "project_mcp_tools"

    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_tools.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    tool: Mapped[MCPTool] = relationship("MCPTool", back_populates="project_assignments")
