"""
app/api/v1/discovery.py — Tool discovery and listing endpoints.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys, TTL_TOOLS
from app.cache.redis_client import cache_get, cache_set, get_redis
from app.core.runtime import local_runner as _lr
from app.db.database import get_db
from app.models.mcp_tool import MCPTool, OrgMCPTool
from app.schemas.registry import ToolResponse
from app.security.auth import AuthContext, get_auth_context

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tools", tags=["discovery"])


@router.get("", response_model=list[ToolResponse | dict])
async def list_tools(
    project_id: uuid.UUID | None = Query(default=None),
    category: str | None = Query(default=None),
    include_local: bool = Query(default=True),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    List all tools available to the requesting org/project.

    Returns a merged list of:
    - Registered remote/hosted tools from the DB (org-scoped)
    - Local plugin tools loaded from data/ (backward compat, always included)
    """
    cache_key = (
        MCPKeys.project_tools(auth.org_id, str(project_id))
        if project_id
        else MCPKeys.org_tools(auth.org_id)
    )

    cached = await cache_get(cache_key, redis=redis)
    if cached:
        tools_data = cached if isinstance(cached, list) else json.loads(cached)
    else:
        # DB tools (registered remote/hosted)
        query = select(MCPTool).where(MCPTool.org_id == uuid.UUID(auth.org_id))
        if project_id:
            query = query.where(
                (MCPTool.project_id == project_id) | (MCPTool.project_id.is_(None))
            )
        if category:
            query = query.where(MCPTool.category == category)

        result = await db.execute(query)
        db_tools = result.scalars().all()

        tools_data = [
            {
                "id": str(t.id),
                "server_id": str(t.server_id),
                "org_id": str(t.org_id),
                "project_id": str(t.project_id) if t.project_id else None,
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "tool_type": t.tool_type,
                "category": t.category,
                "status": t.status,
                "source": "registered",
            }
            for t in db_tools
        ]

        await cache_set(cache_key, tools_data, ttl=TTL_TOOLS, redis=redis)

    # Always merge local plugin tools
    if include_local:
        local = _lr.list_local_tools()
        if category:
            local = [t for t in local if t.get("category") == category]
        tools_data = tools_data + local  # type: ignore[operator]

    return tools_data


@router.get("/{tool_id}")
async def get_tool(
    tool_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Get details for a specific registered tool."""
    result = await db.execute(
        select(MCPTool).where(
            MCPTool.id == tool_id,
            MCPTool.org_id == uuid.UUID(auth.org_id),
        )
    )
    tool = result.scalar_one_or_none()
    if not tool:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tool not found")
    return ToolResponse.model_validate(tool)


@router.get("/{tool_id}/schema")
async def get_tool_schema(
    tool_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Return only the input JSON schema for a tool."""
    result = await db.execute(
        select(MCPTool.input_schema).where(
            MCPTool.id == tool_id,
            MCPTool.org_id == uuid.UUID(auth.org_id),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Tool not found")
    return {"input_schema": row}


@router.post("/discover")
async def trigger_discovery(
    server_id: uuid.UUID | None = None,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Trigger fresh tool discovery from MCP servers.

    If server_id is provided, discovers only from that server.
    Otherwise re-discovers from all active servers for the org.
    """
    from app.services.discovery_service import discovery_service

    count = await discovery_service.discover(
        org_id=uuid.UUID(auth.org_id),
        server_id=server_id,
        db=db,
        redis=redis,
    )
    return {"discovered": count, "org_id": auth.org_id}
