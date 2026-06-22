"""A2 — the event bus round-trips events and stops on the final one."""

from __future__ import annotations

import asyncio

from app.core.streaming.event_bus import event_bus


async def test_publish_subscribe_roundtrip_and_final_stops():
    invocation_id = "test-invocation-roundtrip"
    received: list[dict] = []

    async def consume():
        async for ev in event_bus.subscribe(invocation_id, timeout=5.0):
            received.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let subscribe() register on the channel

    await event_bus.publish_log(invocation_id, "info", "working")
    await event_bus.publish_result(
        invocation_id, {"content": [{"type": "text", "text": "done"}]}
    )

    # The final result event must end the generator without hitting the timeout.
    await asyncio.wait_for(task, timeout=5.0)

    assert received[0]["type"] == "log"
    assert received[-1]["type"] == "result"
    assert received[-1]["final"] is True


async def test_subscribe_times_out_without_events():
    received: list[dict] = []

    async for ev in event_bus.subscribe("nobody-publishes-here", timeout=0.2):
        received.append(ev)

    assert received == []
