"""
app/core/lifecycle/server_manager.py — MCP server connection lifecycle manager.

Manages connect/disconnect state transitions and health state in Redis.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys, TTL_HEALTH
from app.cache.redis_client import cache_set
from app.core.protocol.mcp_client import MCPClientWrapper
from app.core.protocol.negotiation import cache_capabilities
from app.models.mcp_server import MCPConnection, MCPServer, ServerStatus, ServerType

logger = logging.getLogger(__name__)

# In-memory map of server_id → MCPClientWrapper (live connections)
_active_connections: dict[str, MCPClientWrapper] = {}


class ServerManager:
    """Handles the full connect → active → disconnect lifecycle of MCP servers."""

    async def connect(
        self,
        server: MCPServer,
        db: AsyncSession,
        redis: Any,
    ) -> dict[str, Any]:
        """
        Connect to an MCP server, discover its capabilities, update DB status,
        and write health state to Redis.
        """
        server_id = str(server.id)
        org_id = str(server.org_id)

        if server_id in _active_connections:
            return {"status": "already_connected", "server_id": server_id}

        # Update status → connecting
        await db.execute(
            update(MCPServer)
            .where(MCPServer.id == server.id)
            .values(status=ServerStatus.CONNECTING)
        )
        await db.commit()

        if server.server_type == ServerType.LOCAL_PLUGIN:
            # Local plugins don't require a network connection
            await self._record_connected(server, db, redis, capabilities={})
            return {"status": "connected", "server_id": server_id, "type": "local_plugin"}

        if not server.endpoint_url:
            await self._record_error(server, db, redis, error="No endpoint URL configured")
            raise ValueError(f"Server {server_id} has no endpoint_url")

        try:
            transport = "sse" if server.server_type == ServerType.REMOTE_SSE else "http"
            client = MCPClientWrapper(endpoint_url=server.endpoint_url, transport=transport)
            capabilities = await client.connect()
            _active_connections[server_id] = client

            await cache_capabilities(server_id, capabilities, redis=redis)
            await self._record_connected(server, db, redis, capabilities=capabilities)

            return {"status": "connected", "server_id": server_id, "capabilities": capabilities}

        except Exception as exc:
            logger.error("Failed to connect server %s: %s", server_id, exc)
            await self._record_error(server, db, redis, error=str(exc))
            raise

    async def disconnect(
        self,
        server: MCPServer,
        db: AsyncSession,
        redis: Any,
    ) -> None:
        server_id = str(server.id)
        client = _active_connections.pop(server_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

        await db.execute(
            update(MCPServer)
            .where(MCPServer.id == server.id)
            .values(status=ServerStatus.INACTIVE)
        )

        # Mark latest connection as disconnected
        result = await db.execute(
            select(MCPConnection)
            .where(
                MCPConnection.server_id == server.id,
                MCPConnection.status == "connected",
            )
            .order_by(MCPConnection.connected_at.desc())
            .limit(1)
        )
        conn = result.scalar_one_or_none()
        if conn:
            conn.status = "disconnected"
            conn.disconnected_at = datetime.now(timezone.utc)

        await db.commit()
        await self._write_health(str(server.org_id), server_id, "disconnected", redis=redis)

    def get_client(self, server_id: str) -> MCPClientWrapper | None:
        return _active_connections.get(server_id)

    def active_server_ids(self) -> list[str]:
        return list(_active_connections.keys())

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _record_connected(
        self,
        server: MCPServer,
        db: AsyncSession,
        redis: Any,
        capabilities: dict,
    ) -> None:
        now = datetime.now(timezone.utc)
        await db.execute(
            update(MCPServer).where(MCPServer.id == server.id).values(status=ServerStatus.ACTIVE)
        )
        db.add(
            MCPConnection(
                server_id=server.id,
                org_id=server.org_id,
                status="connected",
                connected_at=now,
                metadata_={"capabilities": capabilities},
            )
        )
        await db.commit()
        await self._write_health(
            str(server.org_id), str(server.id), "connected", redis=redis
        )

    async def _record_error(
        self, server: MCPServer, db: AsyncSession, redis: Any, error: str
    ) -> None:
        await db.execute(
            update(MCPServer).where(MCPServer.id == server.id).values(status=ServerStatus.ERROR)
        )
        await db.commit()
        await self._write_health(str(server.org_id), str(server.id), "error", redis=redis, error=error)

    async def _write_health(
        self, org_id: str, server_id: str, status: str, redis: Any, error: str | None = None
    ) -> None:
        key = MCPKeys.server_health(org_id, server_id)
        payload = {
            "server_id": server_id,
            "status": status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if error:
            payload["error"] = error
        await cache_set(key, payload, ttl=TTL_HEALTH, redis=redis)


server_manager = ServerManager()
