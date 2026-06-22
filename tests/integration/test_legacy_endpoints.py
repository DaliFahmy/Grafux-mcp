"""B2 — legacy endpoints still respond after being moved to app/api/legacy.py."""

from __future__ import annotations

import httpx
from httpx import ASGITransport

from app.core.runtime import plugin_loader
from app.main import app


async def test_legacy_health_and_tools_list():
    plugin_loader._plugins_loaded.set()

    @plugin_loader.register_tool("legacy_echo_test", "echo", {"type": "object"})
    def _echo(args):
        return {"content": [{"type": "text", "text": "ok"}]}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "healthy"

        tools = await client.get("/api/tools")
        assert tools.status_code == 200
        names = [t["name"] for t in tools.json()["tools"]]
        assert "legacy_echo_test" in names
