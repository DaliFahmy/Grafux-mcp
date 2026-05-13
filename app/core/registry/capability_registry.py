"""
app/core/registry/capability_registry.py — Capability discovery and caching.
"""

from __future__ import annotations

import logging
from typing import Any

from app.cache.keys import MCPKeys, TTL_CAPABILITIES
from app.cache.redis_client import cache_get, cache_set

logger = logging.getLogger(__name__)


async def store_capabilities(
    server_id: str,
    tools: list[str],
    resources: list[str] | None = None,
    prompts: list[str] | None = None,
    redis: Any = None,
) -> None:
    payload = {
        "server_id": server_id,
        "tools": tools,
        "resources": resources or [],
        "prompts": prompts or [],
    }
    await cache_set(MCPKeys.server_capabilities(server_id), payload, ttl=TTL_CAPABILITIES, redis=redis)


async def get_capabilities(server_id: str, redis: Any = None) -> dict[str, Any] | None:
    return await cache_get(MCPKeys.server_capabilities(server_id), redis=redis)


async def invalidate_capabilities(server_id: str, redis: Any = None) -> None:
    from app.cache.redis_client import cache_delete
    await cache_delete(MCPKeys.server_capabilities(server_id), redis=redis)
