"""R2 — max_concurrent_invocations is actually enforced."""

from __future__ import annotations

import asyncio
import uuid

import app.services.execution_service as es_mod
from app.services.execution_service import ExecutionService
from tests._fakes import FakeSession


def test_semaphore_uses_configured_limit(monkeypatch):
    monkeypatch.setattr(es_mod.settings, "max_concurrent_invocations", 7)
    svc = ExecutionService()
    assert svc._get_semaphore()._value == 7


async def test_cap_serializes_concurrent_invocations(monkeypatch):
    monkeypatch.setattr(es_mod.settings, "max_concurrent_invocations", 1)
    svc = ExecutionService()

    state = {"current": 0, "max": 0}

    async def fake_route(**kwargs):
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.02)
        state["current"] -= 1
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(es_mod.execution_router, "route", fake_route)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(es_mod.event_bus, "publish", _noop)

    async def _one():
        return await svc.invoke(
            tool_name="t",
            arguments={},
            org_id=str(uuid.uuid4()),
            db=FakeSession(),
            redis=None,
        )

    await asyncio.gather(*[_one() for _ in range(4)])

    assert state["max"] == 1  # cap of 1 means no two ran the backend at once
