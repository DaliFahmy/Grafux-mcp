"""
app/core/lifecycle/health_monitor.py — Background health polling for active MCP servers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.cache.keys import MCPKeys, TTL_HEALTH
from app.cache.redis_client import cache_set, get_redis_pool

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 30  # seconds


class HealthMonitor:
    """
    Background task that periodically pings all connected MCP servers
    and writes their health state to Redis.
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="health-monitor")
            logger.info("HealthMonitor started (interval=%ds)", _POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._check_all()
            except Exception as exc:
                logger.error("HealthMonitor error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _check_all(self) -> None:
        from app.core.lifecycle.server_manager import _active_connections

        redis = get_redis_pool()
        for server_id, client in list(_active_connections.items()):
            t0 = time.monotonic()
            status = "unknown"
            error: str | None = None
            try:
                # Lightweight probe: list tools to verify connectivity
                await asyncio.wait_for(client.list_tools(), timeout=10.0)
                latency_ms = (time.monotonic() - t0) * 1000
                status = "healthy"
            except asyncio.TimeoutError:
                latency_ms = 10_000
                status = "timeout"
                error = "Health check timed out"
            except Exception as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                status = "error"
                error = str(exc)

            # Write to Redis (we don't have org_id here; use a generic key)
            key = f"mcp:health:{server_id}"
            payload: dict[str, Any] = {
                "server_id": server_id,
                "status": status,
                "latency_ms": round(latency_ms, 1),
            }
            if error:
                payload["error"] = error
            await cache_set(key, payload, ttl=TTL_HEALTH * 3, redis=redis)

            if status != "healthy":
                logger.warning("Server %s health=%s error=%s", server_id, status, error)


health_monitor = HealthMonitor()
