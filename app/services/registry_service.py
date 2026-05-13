"""
app/services/registry_service.py — High-level MCP registry orchestration.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lifecycle.server_manager import server_manager
from app.core.registry.server_registry import create_server, get_server
from app.models.mcp_server import MCPServer


async def register_and_connect(
    org_id: uuid.UUID,
    data: dict[str, Any],
    db: AsyncSession,
    redis: Any,
) -> tuple[MCPServer, dict[str, Any]]:
    """Register a new MCP server and immediately attempt to connect it."""
    server = await create_server(org_id=org_id, data=data, db=db)
    try:
        connect_result = await server_manager.connect(server, db, redis)
    except Exception as exc:
        connect_result = {"status": "error", "error": str(exc)}
    return server, connect_result
