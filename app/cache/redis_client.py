"""
app/cache/redis_client.py — Async Redis client singleton and helpers.

Redis is treated as an optional dependency. When a real server is reachable it
is used for caching and pub/sub across workers. When it is not (typical for
single-instance local dev, e.g. on Windows without a Redis service), the client
transparently falls back to an in-process stand-in so the rest of the service
keeps working instead of raising on every cache call. Call init_redis() once at
startup to decide which mode is active.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.config import settings

logger = logging.getLogger(__name__)


_redis_pool: Redis | None = None
_fallback: "_InMemoryRedis | None" = None
_use_fallback: bool = False


# ── In-process fallback (single-instance only) ────────────────────────────────

class _InMemoryPubSub:
    """asyncio.Queue-backed pub/sub mirroring the subset of redis.PubSub we use."""

    def __init__(self, hub: dict[str, set[asyncio.Queue]]) -> None:
        self._hub = hub
        self._queue: asyncio.Queue = asyncio.Queue()
        self._channels: set[str] = set()

    async def subscribe(self, channel: str) -> None:
        self._channels.add(channel)
        self._hub.setdefault(channel, set()).add(self._queue)

    async def unsubscribe(self, channel: str | None = None) -> None:
        for ch in ([channel] if channel else list(self._channels)):
            self._channels.discard(ch)
            subs = self._hub.get(ch)
            if subs is not None:
                subs.discard(self._queue)
                if not subs:
                    self._hub.pop(ch, None)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float | None = None
    ) -> dict | None:
        try:
            if timeout is None:
                data = await self._queue.get()
            else:
                data = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return {"type": "message", "data": data}

    async def aclose(self) -> None:
        await self.unsubscribe()

    # redis<5 compatibility alias
    close = aclose


class _InMemoryRedis:
    """Minimal single-process stand-in for Redis.

    Covers exactly the surface Grafux-mcp uses — get/set/setex/delete/scan_iter
    and pub/sub. State lives only in this process, so it is fine for single-
    instance local dev but never a substitute for real Redis across workers.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, float | None]] = {}
        self._channels: dict[str, set[asyncio.Queue]] = {}

    async def ping(self) -> bool:
        return True

    def _expired(self, key: str) -> bool:
        item = self._store.get(key)
        if item is None:
            return True
        _, expiry = item
        if expiry is not None and expiry <= time.monotonic():
            self._store.pop(key, None)
            return True
        return False

    async def get(self, key: str) -> str | None:
        if self._expired(key):
            return None
        return self._store[key][0]

    async def set(self, key: str, value: str) -> bool:
        self._store[key] = (value, None)
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self._store[key] = (value, time.monotonic() + float(ttl))
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._store.pop(key, None) is not None:
                removed += 1
        return removed

    async def scan_iter(self, match: str = "*", count: int | None = None) -> AsyncGenerator[str, None]:
        for key in list(self._store.keys()):
            if self._expired(key):
                continue
            if fnmatch.fnmatch(key, match):
                yield key

    async def publish(self, channel: str, message: str) -> int:
        subs = self._channels.get(channel)
        if not subs:
            return 0
        for q in list(subs):
            q.put_nowait(message)
        return len(subs)

    def pubsub(self) -> _InMemoryPubSub:
        return _InMemoryPubSub(self._channels)

    async def aclose(self) -> None:
        self._store.clear()
        self._channels.clear()


# ── Pool management ───────────────────────────────────────────────────────────

def get_redis_pool() -> Redis:
    """Return the shared client — the real pool, or the in-process fallback once
    init_redis() has determined Redis is unreachable."""
    global _redis_pool, _fallback
    if _use_fallback:
        if _fallback is None:
            _fallback = _InMemoryRedis()
        return _fallback  # type: ignore[return-value]
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=50,
        )
    return _redis_pool


async def init_redis() -> str:
    """Probe Redis once at startup. On failure, switch to the in-process
    fallback so cache/pub-sub calls degrade gracefully instead of raising.

    Returns a short status string for the caller to log.
    """
    global _use_fallback
    try:
        redis = get_redis_pool()
        await redis.ping()
        return f"connected ({settings.redis_url})"
    except Exception as exc:
        _use_fallback = True
        await close_redis_pool()
        return f"unavailable ({exc}) — using in-process fallback (single-instance only)"


def using_fallback() -> bool:
    return _use_fallback


async def close_redis_pool() -> None:
    global _redis_pool
    if _redis_pool is not None:
        try:
            await _redis_pool.aclose()
        except Exception:
            pass
        _redis_pool = None


async def get_redis() -> Redis:
    """FastAPI dependency — returns the shared Redis pool (or fallback)."""
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
