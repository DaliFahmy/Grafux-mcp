"""
app/core/streaming/event_bus.py — Redis pub/sub event bus for invocation streaming.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from app.cache.keys import MCPKeys
from app.cache.redis_client import get_redis_pool

logger = logging.getLogger(__name__)


class EventBus:
    """
    Thin wrapper around Redis pub/sub for broadcasting invocation progress events.

    Producers call publish() to emit events.
    Consumers iterate subscribe() to receive them until the stream ends.
    """

    async def publish(
        self,
        invocation_id: str,
        event_type: str,
        data: Any,
        *,
        is_final: bool = False,
    ) -> None:
        """Publish an event to an invocation's channel."""
        channel = MCPKeys.invocation_events(invocation_id)
        payload = json.dumps(
            {"type": event_type, "data": data, "final": is_final}
        )
        try:
            redis = get_redis_pool()
            await redis.publish(channel, payload)
        except Exception as exc:
            logger.warning("EventBus.publish failed for %s: %s", invocation_id, exc)

    async def publish_log(self, invocation_id: str, level: str, message: str) -> None:
        await self.publish(invocation_id, "log", {"level": level, "message": message})

    async def publish_result(self, invocation_id: str, result: Any) -> None:
        await self.publish(invocation_id, "result", result, is_final=True)

    async def publish_error(self, invocation_id: str, error: str) -> None:
        await self.publish(invocation_id, "error", {"error": error}, is_final=True)

    async def subscribe(
        self,
        invocation_id: str,
        timeout: float = 300.0,
    ) -> AsyncGenerator[dict, None]:
        """
        Async generator that yields events for the given invocation.

        Stops when a final event is received or when timeout expires.
        """
        channel = MCPKeys.invocation_events(invocation_id)
        redis = get_redis_pool()
        pubsub = redis.pubsub()

        loop = asyncio.get_event_loop()
        try:
            await pubsub.subscribe(channel)
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning("EventBus subscription timed out for %s", invocation_id)
                    break

                # Block on the socket for the next message (capped so we re-check the
                # deadline periodically). Returns None on timeout — no busy-wait, no
                # artificial latency floor.
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=min(remaining, 5.0),
                )
                if message is None or message.get("type") != "message":
                    continue

                try:
                    event = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                yield event

                if event.get("final"):
                    break

        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception as exc:
                logger.debug("EventBus cleanup error for %s: %s", invocation_id, exc)


event_bus = EventBus()
