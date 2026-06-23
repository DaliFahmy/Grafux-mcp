"""Remote MCP connection pool — reuse, serialization, reconnect, eviction.

Fully offline: a FakeWrapper stands in for ``MCPClientWrapper`` so no network or
``mcp`` SDK is needed. Each test uses its own ``RemoteConnectionPool`` instance to
avoid cross-test state in the module singleton.
"""

from __future__ import annotations

import asyncio

import app.core.protocol.connection_pool as cp_mod
from app.core.protocol.connection_pool import RemoteConnectionPool


class FakeWrapper:
    """Records connect/call/disconnect and tracks in-flight concurrency."""

    # Shared across instances so tests can observe global overlap.
    created: list[FakeWrapper] = []
    inflight = 0
    max_inflight = 0
    # Class-level defaults so monkeypatch.setattr(FakeWrapper, ...) affects every
    # instance (the pool builds the instances itself, so we can't set them per-call).
    delay = 0.0
    fail_calls = 0

    def __init__(self, endpoint_url: str, transport: str = "sse") -> None:
        self.endpoint_url = endpoint_url
        self.transport = transport
        self.connect_count = 0
        self.disconnect_count = 0
        self.call_count = 0
        self._session = "session"
        FakeWrapper.created.append(self)

    async def connect(self) -> dict:
        self.connect_count += 1
        return {"name": "fake"}

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self.call_count += 1
        if self.fail_calls > 0:
            self.fail_calls -= 1
            raise ConnectionError("simulated broken session")
        FakeWrapper.inflight += 1
        FakeWrapper.max_inflight = max(FakeWrapper.max_inflight, FakeWrapper.inflight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return {"content": [{"type": "text", "text": "ok"}]}
        finally:
            FakeWrapper.inflight -= 1

    async def list_tools(self) -> list[dict]:
        return []

    async def disconnect(self) -> None:
        self.disconnect_count += 1


def _reset_fakewrapper() -> None:
    FakeWrapper.created = []
    FakeWrapper.inflight = 0
    FakeWrapper.max_inflight = 0


def _patch(monkeypatch):
    _reset_fakewrapper()
    monkeypatch.setattr(cp_mod, "MCPClientWrapper", FakeWrapper)


async def test_connection_is_reused_across_calls(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    for _ in range(3):
        result = await pool.call_tool("https://x.test", "sse", "t", {})
        assert result["content"][0]["text"] == "ok"

    assert len(FakeWrapper.created) == 1  # one wrapper, not three
    w = FakeWrapper.created[0]
    assert w.connect_count == 1  # connected once, reused
    assert w.call_count == 3
    assert w.disconnect_count == 0


async def test_same_endpoint_calls_are_serialized(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    # Make the call block so overlap would be observable if it were allowed.
    monkeypatch.setattr(FakeWrapper, "delay", 0.02)

    async def one():
        return await pool.call_tool("https://x.test", "sse", "t", {})

    await asyncio.gather(*[one() for _ in range(4)])

    assert FakeWrapper.max_inflight == 1  # the per-connection lock serializes use


async def test_different_endpoints_run_in_parallel(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()
    monkeypatch.setattr(FakeWrapper, "delay", 0.02)

    async def call(url):
        return await pool.call_tool(url, "sse", "t", {})

    await asyncio.gather(call("https://a.test"), call("https://b.test"))

    assert len(FakeWrapper.created) == 2
    assert FakeWrapper.max_inflight == 2  # distinct endpoints aren't serialized


async def test_broken_session_reconnects_once(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    # First call raises a reconnectable error; the pool should disconnect, reconnect,
    # and retry once transparently.
    orig_init = FakeWrapper.__init__

    def init_with_fail(self, endpoint_url, transport="sse"):
        orig_init(self, endpoint_url, transport)
        if len(FakeWrapper.created) == 1:
            self.fail_calls = 1

    monkeypatch.setattr(FakeWrapper, "__init__", init_with_fail)

    result = await pool.call_tool("https://x.test", "sse", "t", {})
    assert result["content"][0]["text"] == "ok"

    w = FakeWrapper.created[0]
    assert w.connect_count == 2  # initial + one reconnect
    assert w.disconnect_count >= 1
    assert w.call_count == 2  # failed once, then succeeded


async def test_idle_eviction_disconnects(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    await pool.call_tool("https://x.test", "sse", "t", {})
    w = FakeWrapper.created[0]

    # Negative idle threshold => every entry is considered stale.
    await pool.evict_idle(-1.0)

    assert w.disconnect_count == 1
    # A subsequent call must build a fresh connection.
    await pool.call_tool("https://x.test", "sse", "t", {})
    assert len(FakeWrapper.created) == 2


async def test_aclose_all_disconnects_everything(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    await pool.call_tool("https://a.test", "sse", "t", {})
    await pool.call_tool("https://b.test", "http", "t", {})

    await pool.aclose_all()

    assert all(w.disconnect_count == 1 for w in FakeWrapper.created)


async def test_capacity_evicts_lru(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(cp_mod.settings, "remote_pool_max_size", 2)
    pool = RemoteConnectionPool()

    for url in ("https://a.test", "https://b.test", "https://c.test"):
        await pool.call_tool(url, "sse", "t", {})

    # Pool never exceeds the configured cap.
    assert len(pool._entries) <= 2


def test_loop_rebind_creates_new_connection(monkeypatch):
    _patch(monkeypatch)
    pool = RemoteConnectionPool()

    async def call():
        return await pool.call_tool("https://x.test", "sse", "t", {})

    asyncio.run(call())  # loop #1 — connects
    asyncio.run(call())  # loop #2 — old loop closed, entry pruned, reconnects

    assert len(FakeWrapper.created) == 2
