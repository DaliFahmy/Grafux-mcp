"""app/core/http_client.py — Shared, connection-pooled ``httpx.AsyncClient``.

Creating an ``httpx.AsyncClient`` per request throws away the TCP/TLS connection
pool, so every outbound call (remote MCP servers, Composio, devices, backend auth)
pays a fresh handshake. This module hands out a reused client instead.

An ``httpx.AsyncClient`` is bound to the event loop it was created on. The server
runs a single long-lived loop, so it gets exactly one client reused across every
request. To stay correct under any other caller (tests using ``asyncio.run`` per
case, background task loops), the client is keyed by the *running loop*: one client
per loop, reused for that loop's lifetime, transparently recreated for the next.

Mirrors the sibling service's module (see memory ``orchestrator-perf-refactor``).

Usage::

    from app.core.http_client import get_http_client

    resp = await get_http_client().post(url, json=payload, timeout=30.0)

Per-call ``timeout=`` overrides the client default, so callers keep control of
individual request budgets. The server closes its client in the FastAPI lifespan
shutdown (see ``app/main.py``).
"""

from __future__ import annotations

import asyncio

import httpx

from app.config import settings

# Maps an event loop (by id) to the client created on it. The loop is kept in the
# value so we can detect identity reuse and prune entries for closed loops.
_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.http_default_timeout,
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive_connections,
        ),
    )


def _prune_closed() -> None:
    """Drop entries whose loop has been closed."""
    for key, (loop, _client) in list(_clients.items()):
        if loop.is_closed():
            _clients.pop(key, None)


def get_http_client() -> httpx.AsyncClient:
    """Return a pooled async HTTP client bound to the currently running loop."""
    loop = asyncio.get_running_loop()
    entry = _clients.get(id(loop))
    if entry is not None and entry[0] is loop and not entry[1].is_closed:
        return entry[1]
    _prune_closed()
    client = _new_client()
    _clients[id(loop)] = (loop, client)
    return client


async def aclose_http_client() -> None:
    """Close the current loop's client and release its pooled connections.

    Idempotent. Safe to call from the FastAPI lifespan shutdown.
    """
    loop = asyncio.get_running_loop()
    entry = _clients.pop(id(loop), None)
    if entry is not None and not entry[1].is_closed:
        await entry[1].aclose()
