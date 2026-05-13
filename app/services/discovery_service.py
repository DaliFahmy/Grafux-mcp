"""
app/services/discovery_service.py — Multi-source tool discovery.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys
from app.cache.redis_client import cache_delete
from app.core.lifecycle.server_manager import server_manager
from app.core.registry.capability_registry import store_capabilities
from app.core.registry.tool_registry import upsert_tools
from app.models.mcp_server import MCPServer, ServerStatus, ServerType

logger = logging.getLogger(__name__)


class DiscoveryService:
    """
    Orchestrates tool discovery from all registered MCP servers for an org.

    Connects to each active server, lists its tools, persists them to the DB,
    and invalidates the org's tool cache so the next list_tools call picks them up.
    """

    async def discover(
        self,
        org_id: uuid.UUID,
        server_id: uuid.UUID | None,
        db: AsyncSession,
        redis: Any,
    ) -> int:
        """
        Discover tools from one or all active servers.

        Returns the total count of tools upserted.
        """
        if server_id:
            result = await db.execute(
                select(MCPServer).where(
                    MCPServer.id == server_id,
                    MCPServer.org_id == org_id,
                )
            )
            servers = [s for s in [result.scalar_one_or_none()] if s is not None]
        else:
            result = await db.execute(
                select(MCPServer).where(
                    MCPServer.org_id == org_id,
                    MCPServer.status == ServerStatus.ACTIVE,
                )
            )
            servers = list(result.scalars().all())

        total = 0
        for srv in servers:
            try:
                count = await self._discover_server(srv, db, redis)
                total += count
            except Exception as exc:
                logger.error("Discovery failed for server %s: %s", srv.id, exc)

        # Invalidate org tool cache
        await cache_delete(MCPKeys.org_tools(str(org_id)), redis=redis)
        return total

    async def _discover_server(
        self, server: MCPServer, db: AsyncSession, redis: Any
    ) -> int:
        server_id = str(server.id)

        # Local plugins don't use the MCP protocol — they're discovered at startup
        if server.server_type == ServerType.LOCAL_PLUGIN:
            from app.core.runtime.local_runner import list_local_tools
            tools = list_local_tools()
            await store_capabilities(
                server_id, [t["name"] for t in tools], redis=redis
            )
            return len(tools)

        # For remote/hosted servers, use the live client connection
        client = server_manager.get_client(server_id)
        if not client:
            logger.warning("Server %s has no active connection — skipping discovery", server_id)
            return 0

        raw_tools = await client.list_tools()
        count = await upsert_tools(server.id, server.org_id, raw_tools, db)
        await store_capabilities(server_id, [t["name"] for t in raw_tools], redis=redis)
        logger.info("Discovered %d tools from server %s", count, server_id)
        return count


discovery_service = DiscoveryService()
