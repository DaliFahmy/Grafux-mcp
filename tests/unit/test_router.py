"""A4 — table-driven dispatch, typed errors, and reused stateless clients."""

from __future__ import annotations

import pytest

from app.core.exceptions import RoutingError
from app.core.runtime.router import ExecutionRouter
from app.models.mcp_server import ServerType


async def test_unknown_server_type_raises_routing_error():
    router = ExecutionRouter()
    with pytest.raises(RoutingError):
        await router.route(
            server_type="nonsense",
            tool_name="x",
            arguments={},
            invocation_id="i",
            org_id="o",
        )


async def test_remote_without_endpoint_raises_routing_error():
    router = ExecutionRouter()
    with pytest.raises(RoutingError):
        await router.route(
            server_type=ServerType.REMOTE_SSE,
            tool_name="x",
            arguments={},
            invocation_id="i",
            org_id="o",
            endpoint_url=None,
        )


async def test_device_client_constructed_once_and_reused(monkeypatch):
    constructed = {"count": 0}

    class FakeDevicesClient:
        def __init__(self) -> None:
            constructed["count"] += 1

        async def call_tool(self, tool_name, arguments, config=None):
            return {"content": [{"type": "text", "text": "ok"}], "isError": False}

    monkeypatch.setattr(
        "app.integrations.devices.client.DevicesClient", FakeDevicesClient
    )

    router = ExecutionRouter()
    for _ in range(3):
        await router.route(
            server_type=ServerType.DEVICE,
            tool_name="t",
            arguments={},
            invocation_id="i",
            org_id="o",
            server_config={},
        )

    assert constructed["count"] == 1  # cached across invocations
