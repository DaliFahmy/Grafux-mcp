"""
app/core/registry/server_registry.py — MCP server registry operations.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import MCPServer, ServerStatus


async def get_server(
    server_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> MCPServer | None:
    result = await db.execute(
        select(MCPServer).where(
            MCPServer.id == server_id,
            MCPServer.org_id == org_id,
        )
    )
    return result.scalar_one_or_none()


async def list_servers(
    org_id: uuid.UUID,
    db: AsyncSession,
    status: ServerStatus | None = None,
) -> list[MCPServer]:
    query = select(MCPServer).where(MCPServer.org_id == org_id)
    if status:
        query = query.where(MCPServer.status == status)
    result = await db.execute(query.order_by(MCPServer.created_at.desc()))
    return list(result.scalars().all())


async def create_server(
    org_id: uuid.UUID,
    data: dict[str, Any],
    db: AsyncSession,
) -> MCPServer:
    server = MCPServer(org_id=org_id, **data)
    db.add(server)
    await db.commit()
    await db.refresh(server)
    return server


async def delete_server(
    server: MCPServer,
    db: AsyncSession,
) -> None:
    await db.delete(server)
    await db.commit()
