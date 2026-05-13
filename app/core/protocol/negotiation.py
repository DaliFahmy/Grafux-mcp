"""
app/core/protocol/negotiation.py — MCP capability negotiation.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.cache.keys import MCPKeys, TTL_CAPABILITIES
from app.cache.redis_client import cache_get, cache_set

logger = logging.getLogger(__name__)


async def cache_capabilities(
    server_id: str,
    capabilities: dict[str, Any],
    redis: Any = None,
) -> None:
    key = MCPKeys.server_capabilities(server_id)
    await cache_set(key, capabilities, ttl=TTL_CAPABILITIES, redis=redis)


async def get_cached_capabilities(
    server_id: str,
    redis: Any = None,
) -> dict[str, Any] | None:
    key = MCPKeys.server_capabilities(server_id)
    return await cache_get(key, redis=redis)


def merge_capabilities(
    server_caps: dict[str, Any],
    local_caps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge server-reported capabilities with locally known capabilities."""
    merged = dict(server_caps)
    if local_caps:
        for k, v in local_caps.items():
            if k not in merged:
                merged[k] = v
    return merged
