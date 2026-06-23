"""app/core/protocol/connection_pool.py — Pooled remote MCP connections.

Remote tool invocations used to open a brand-new connection for every call:
``MCPClientWrapper()`` → ``connect()`` (full SSE/streamable-HTTP handshake +
``ClientSession.initialize()``) → ``call_tool()`` → ``disconnect()``. That cost a
fresh handshake (often 100–500 ms) on every single invocation. This pool keeps one
live wrapper per ``(endpoint_url, transport)`` and reuses it.

Concurrency-safety (why per-connection locks):
    The ``mcp`` SDK transports (``sse_client`` / ``streamablehttp_client``) and
    ``ClientSession`` are built on anyio task-scoped cancel scopes. A single
    ``ClientSession`` must never be driven concurrently (two in-flight round-trips
    interleave reads/writes on the shared stream and corrupt the protocol), and its
    ``__aenter__``/``__aexit__`` must run on one event loop. So each pooled entry is
    guarded by an ``asyncio.Lock``: the session is *reused* across invocations but
    never used *concurrently*. Calls to the same endpoint serialize on that lock;
    calls to different endpoints run fully in parallel. Entries are also keyed by the
    running loop (mirroring ``app/core/http_client.py``) and rebuilt if the loop
    changes, keeping all cancel scopes on a single consistent loop.

Relationship to ``ServerManager._active_connections``:
    That map holds sessions owned by the explicit connect/disconnect registry API and
    poked by the health monitor; it has no per-use lock. Invocations must NOT reuse
    those sessions — this pool is the *sole* owner of invocation-time sessions. The
    two are deliberately separate (different lifecycles).

Kill-switch:
    If SDK cross-task ``__aexit__`` ever proves flaky in staging, the conservative
    fallback is to keep SDK sessions ephemeral (the old behaviour) and rely on the
    HTTP-fallback path, which already reuses the pooled ``httpx`` client
    (``get_http_client()``) and is unconditionally concurrency-safe.

The pool returns exactly what ``MCPClientWrapper.call_tool`` / ``list_tools`` return
today (SDK envelope or HTTP-fallback JSON) — no contract change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.config import settings
from app.core.protocol.mcp_client import MCPClientWrapper

logger = logging.getLogger(__name__)

# Errors that indicate the live session is stale/broken and a single transparent
# reconnect is worth attempting. RuntimeError covers the SDK's "cancel scope in a
# different task" failure; the rest are transport-level breakages.
_RECONNECTABLE: tuple[type[BaseException], ...] = (
    ConnectionError,
    BrokenPipeError,
    OSError,
    RuntimeError,
)


@dataclass
class _Entry:
    """One pooled wrapper plus the lock that serializes all use of it."""

    wrapper: MCPClientWrapper
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    loop: asyncio.AbstractEventLoop | None = None
    connected: bool = False
    last_used: float = field(default_factory=time.monotonic)


class RemoteConnectionPool:
    """Per-endpoint pool of live ``MCPClientWrapper`` connections."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        # Guards mutation of the _entries dict itself (create / evict).
        self._registry_lock = asyncio.Lock()

    # ── Acquisition ───────────────────────────────────────────────────────────

    async def _get_entry(self, endpoint_url: str, transport: str) -> _Entry:
        key = (endpoint_url, transport)
        async with self._registry_lock:
            self._prune_closed_loops()
            entry = self._entries.get(key)
            if entry is None:
                await self._evict_to_capacity()
                entry = _Entry(
                    wrapper=MCPClientWrapper(endpoint_url=endpoint_url, transport=transport)
                )
                self._entries[key] = entry
            return entry

    @contextlib.asynccontextmanager
    async def acquire(self, endpoint_url: str, transport: str) -> AsyncIterator[MCPClientWrapper]:
        """Yield a connected wrapper, holding its lock for the whole round-trip."""
        loop = asyncio.get_running_loop()
        entry = await self._get_entry(endpoint_url, transport)
        async with entry.lock:
            # A wrapper connected on a different loop can't be reused safely.
            if entry.connected and entry.loop is not loop:
                await self._hard_close(entry)
            if not entry.connected:
                await entry.wrapper.connect()
                entry.connected = True
                entry.loop = loop
            entry.last_used = time.monotonic()
            yield entry.wrapper

    # ── Public operations ──────────────────────────────────────────────────────

    async def call_tool(
        self, endpoint_url: str, transport: str, name: str, arguments: dict
    ) -> dict:
        try:
            async with self.acquire(endpoint_url, transport) as wrapper:
                return await wrapper.call_tool(name, arguments)
        except _RECONNECTABLE as exc:
            logger.warning(
                "Pooled MCP call to %s failed (%s) — reconnecting once", endpoint_url, exc
            )
            await self._mark_broken(endpoint_url, transport)
            async with self.acquire(endpoint_url, transport) as wrapper:
                return await wrapper.call_tool(name, arguments)

    async def list_tools(self, endpoint_url: str, transport: str) -> list[dict]:
        try:
            async with self.acquire(endpoint_url, transport) as wrapper:
                return await wrapper.list_tools()
        except _RECONNECTABLE as exc:
            logger.warning(
                "Pooled MCP list_tools on %s failed (%s) — reconnecting once",
                endpoint_url,
                exc,
            )
            await self._mark_broken(endpoint_url, transport)
            async with self.acquire(endpoint_url, transport) as wrapper:
                return await wrapper.list_tools()

    # ── Maintenance ────────────────────────────────────────────────────────────

    async def evict_idle(self, max_idle_seconds: float) -> None:
        """Disconnect and drop entries unused for longer than ``max_idle_seconds``."""
        now = time.monotonic()
        async with self._registry_lock:
            stale = [
                (key, entry)
                for key, entry in self._entries.items()
                if now - entry.last_used > max_idle_seconds and not entry.lock.locked()
            ]
            for key, entry in stale:
                await self._hard_close(entry)
                self._entries.pop(key, None)

    async def aclose_all(self) -> None:
        """Disconnect every pooled connection (lifespan shutdown)."""
        async with self._registry_lock:
            for entry in list(self._entries.values()):
                await self._hard_close(entry)
            self._entries.clear()

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _mark_broken(self, endpoint_url: str, transport: str) -> None:
        entry = self._entries.get((endpoint_url, transport))
        if entry is not None:
            async with entry.lock:
                await self._hard_close(entry)

    async def _hard_close(self, entry: _Entry) -> None:
        """Best-effort disconnect; never raises."""
        if entry.connected:
            try:
                await entry.wrapper.disconnect()
            except Exception as exc:
                logger.debug("Error disconnecting pooled MCP wrapper: %s", exc)
        entry.connected = False

    def _prune_closed_loops(self) -> None:
        for key, entry in list(self._entries.items()):
            if entry.loop is not None and entry.loop.is_closed():
                self._entries.pop(key, None)

    async def _evict_to_capacity(self) -> None:
        """Disconnect and drop the least-recently-used unlocked entry if full."""
        if len(self._entries) < settings.remote_pool_max_size:
            return
        candidates = [
            (key, entry) for key, entry in self._entries.items() if not entry.lock.locked()
        ]
        if not candidates:
            return
        key, entry = min(candidates, key=lambda kv: kv[1].last_used)
        await self._hard_close(entry)
        self._entries.pop(key, None)


remote_connection_pool = RemoteConnectionPool()
