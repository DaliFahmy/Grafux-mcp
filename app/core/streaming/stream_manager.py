"""
app/core/streaming/stream_manager.py — WebSocket connection manager.

Manages active WebSocket connections and fan-out of invocation events
from Redis pub/sub to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class StreamManager:
    """
    Tracks active WebSocket connections grouped by org_id.

    Supports per-invocation streaming subscriptions so that any worker
    in a horizontally-scaled deployment can push events to any connected client
    via Redis pub/sub → StreamManager fan-out.
    """

    def __init__(self) -> None:
        # org_id → set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        # invocation_id → set of WebSocket connections waiting for that stream
        self._invocation_waiters: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, org_id: str) -> None:
        async with self._lock:
            self._connections[org_id].add(websocket)
        logger.debug("WS connected org=%s total=%d", org_id, len(self._connections[org_id]))

    async def disconnect(self, websocket: WebSocket, org_id: str) -> None:
        async with self._lock:
            self._connections[org_id].discard(websocket)
            if not self._connections[org_id]:
                del self._connections[org_id]
            # Remove from all invocation waiters
            for waiters in self._invocation_waiters.values():
                waiters.discard(websocket)
        logger.debug("WS disconnected org=%s", org_id)

    async def subscribe_invocation(
        self, websocket: WebSocket, invocation_id: str
    ) -> None:
        async with self._lock:
            self._invocation_waiters[invocation_id].add(websocket)

    async def unsubscribe_invocation(
        self, websocket: WebSocket, invocation_id: str
    ) -> None:
        async with self._lock:
            self._invocation_waiters[invocation_id].discard(websocket)

    async def send_to_invocation_waiters(
        self, invocation_id: str, message: dict[str, Any]
    ) -> None:
        async with self._lock:
            waiters = set(self._invocation_waiters.get(invocation_id, set()))

        payload = json.dumps(message)
        dead: list[WebSocket] = []

        for ws in waiters:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._invocation_waiters[invocation_id].discard(ws)

    async def broadcast_to_org(
        self, org_id: str, message: dict[str, Any]
    ) -> None:
        async with self._lock:
            connections = set(self._connections.get(org_id, set()))

        payload = json.dumps(message)
        dead: list[WebSocket] = []

        for ws in connections:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections[org_id].discard(ws)

    def connection_count(self) -> int:
        return sum(len(v) for v in self._connections.values())


stream_manager = StreamManager()
