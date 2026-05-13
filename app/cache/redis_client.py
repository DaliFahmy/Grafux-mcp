"""
app/cache/redis_client.py — Async Redis client singleton and helpers.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.config import settings


_redis_pool: Redis | None = None


def get_redis_pool() -> Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
    return _redis_pool


async def close_redis_pool() -> None:
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None


async def get_redis() -> Redis:
    """FastAPI dependency — returns the shared Redis pool."""
    return get_redis_pool()


# ── Typed helpers ─────────────────────────────────────────────────────────────

async def cache_set(
    key: str,
    value: Any,
    ttl: int | None = None,
    redis: Redis | None = None,
) -> None:
    r = redis or get_redis_pool()
    serialized = json.dumps(value) if not isinstance(value, str) else value
    if ttl:
        await r.setex(key, ttl, serialized)
    else:
        await r.set(key, serialized)


async def cache_get(
    key: str,
    redis: Redis | None = None,
) -> Any | None:
    r = redis or get_redis_pool()
    raw = await r.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


async def cache_delete(key: str, redis: Redis | None = None) -> None:
    r = redis or get_redis_pool()
    await r.delete(key)


async def cache_delete_pattern(pattern: str, redis: Redis | None = None) -> int:
    """Delete all keys matching a pattern. Returns the number of keys deleted."""
    r = redis or get_redis_pool()
    deleted = 0
    async for key in r.scan_iter(pattern):
        await r.delete(key)
        deleted += 1
    return deleted


@asynccontextmanager
async def pubsub_context(redis: Redis | None = None) -> AsyncGenerator[PubSub, None]:
    r = redis or get_redis_pool()
    ps = r.pubsub()
    try:
        yield ps
    finally:
        await ps.aclose()
