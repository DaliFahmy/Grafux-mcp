"""R3 — retries are transient-only (deterministic errors fail fast)."""

from __future__ import annotations

import uuid

import app.services.execution_service as es_mod
from app.services.execution_service import execution_service
from tests._fakes import FakeSession


async def _invoke_with_route(monkeypatch, route_impl):
    """Run a sync invoke whose backend `route` is driven by route_impl(attempt)."""
    monkeypatch.setattr(es_mod, "_backoff_seconds", lambda attempt: 0.0)

    calls = {"n": 0}

    async def fake_route(**kwargs):
        calls["n"] += 1
        return await route_impl(calls["n"])

    monkeypatch.setattr(es_mod.execution_router, "route", fake_route)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(es_mod.event_bus, "publish", _noop)

    result = await execution_service.invoke(
        tool_name="t",
        arguments={},
        org_id=str(uuid.uuid4()),
        db=FakeSession(),
        redis=None,
    )
    return result, calls["n"]


async def test_non_transient_error_is_not_retried(monkeypatch):
    async def route_impl(attempt):
        raise ValueError("unknown tool")

    result, attempts = await _invoke_with_route(monkeypatch, route_impl)
    assert result["isError"] is True
    assert attempts == 1  # failed fast, no retries


async def test_transient_error_is_retried_to_exhaustion(monkeypatch):
    async def route_impl(attempt):
        raise ConnectionError("connection reset")

    result, attempts = await _invoke_with_route(monkeypatch, route_impl)
    assert result["isError"] is True
    assert attempts == 3  # initial + MAX_RETRIES(2)


async def test_transient_error_then_success(monkeypatch):
    async def route_impl(attempt):
        if attempt == 1:
            raise ConnectionError("connection reset")
        return {"content": [{"type": "text", "text": "ok"}]}

    result, attempts = await _invoke_with_route(monkeypatch, route_impl)
    assert "isError" not in result
    assert attempts == 2
