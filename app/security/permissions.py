"""
app/security/permissions.py — Org and project-scoped tool access checks.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_tool import MCPTool, OrgMCPTool, ProjectMCPTool
from app.security.auth import AuthContext

logger = logging.getLogger(__name__)


async def assert_tool_accessible(
    tool_id: uuid.UUID,
    auth: AuthContext,
    db: AsyncSession,
) -> MCPTool:
    """
    Verify that a tool is accessible for the given org (and optional project).

    Raises 404 if the tool doesn't exist in the org, or 403 if it's disabled.
    """
    result = await db.execute(
        select(MCPTool).where(
            MCPTool.id == tool_id,
            MCPTool.org_id == uuid.UUID(auth.org_id),
        )
    )
    tool = result.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")

    # Check org-level enablement
    org_assignment = await db.execute(
        select(OrgMCPTool).where(
            OrgMCPTool.tool_id == tool_id,
            OrgMCPTool.org_id == uuid.UUID(auth.org_id),
        )
    )
    org_row = org_assignment.scalar_one_or_none()
    if org_row and not org_row.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tool disabled for org")

    # Check project-level enablement when a project is in context
    if auth.project_id:
        proj_assignment = await db.execute(
            select(ProjectMCPTool).where(
                ProjectMCPTool.tool_id == tool_id,
                ProjectMCPTool.project_id == uuid.UUID(auth.project_id),
            )
        )
        proj_row = proj_assignment.scalar_one_or_none()
        if proj_row and not proj_row.enabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Tool disabled for project"
            )

    return tool


async def assert_server_owned_by_org(
    server_id: uuid.UUID,
    auth: AuthContext,
    db: AsyncSession,
) -> None:
    """Verify that an MCP server belongs to the requesting org."""
    from app.models.mcp_server import MCPServer

    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == server_id,
            MCPServer.org_id == uuid.UUID(auth.org_id),
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")


def require_role(allowed_roles: list[str]):
    """Dependency factory — raises 403 if the user's role is not in allowed_roles."""

    def _check(auth: AuthContext) -> AuthContext:
        if auth.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{auth.role}' not permitted. Required: {allowed_roles}",
            )
        return auth

    return _check
