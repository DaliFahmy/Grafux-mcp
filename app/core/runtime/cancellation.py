"""
app/core/runtime/cancellation.py — Invocation cancellation registry.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class CancellationRegistry:
    """
    Tracks active invocations and provides cancellation tokens.

    Each running invocation registers an asyncio.Event. Callers can
    check the event or await it to detect cancellation.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def register(self, invocation_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self._events[invocation_id] = event
        return event

    def cancel(self, invocation_id: str) -> bool:
        event = self._events.get(invocation_id)
        if event and not event.is_set():
            event.set()
            logger.info("Invocation %s cancelled", invocation_id)
            return True
        return False

    def unregister(self, invocation_id: str) -> None:
        self._events.pop(invocation_id, None)

    def is_cancelled(self, invocation_id: str) -> bool:
        event = self._events.get(invocation_id)
        return event is not None and event.is_set()

    def active_count(self) -> int:
        return len(self._events)


cancellation_registry = CancellationRegistry()
