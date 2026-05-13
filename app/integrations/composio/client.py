"""
app/integrations/composio/client.py — Composio MCP client.

Treats Composio as a remote MCP server (SSE or HTTP transport).
Each connected Composio integration is stored as an mcp_servers row
with server_type=composio.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


class ComposioMCPClient:
    """
    Client for calling Composio-hosted MCP tools.

    Composio exposes tools via its own MCP endpoint; we connect to it
    using the standard MCP client wrapper (SSE transport).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._api_key = (
            self._config.get("api_key")
            or settings.composio_api_key
        )

    def _get_endpoint(self, tool_name: str) -> str:
        base = self._config.get("endpoint_url", "https://mcp.composio.dev")
        return f"{base}/{tool_name}"

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available Composio actions via the MCP protocol."""
        endpoint = self._config.get("endpoint_url", "https://mcp.composio.dev")
        try:
            from app.core.protocol.mcp_client import MCPClientWrapper
            client = MCPClientWrapper(
                endpoint_url=endpoint,
                transport=self._config.get("transport", "sse"),
            )
            await client.connect()
            tools = await client.list_tools()
            await client.disconnect()
            return tools
        except Exception as exc:
            logger.warning("Composio list_tools failed (SDK path): %s — trying REST", exc)
            return await self._rest_list_tools()

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Invoke a Composio action."""
        endpoint = self._config.get("endpoint_url", "https://mcp.composio.dev")
        try:
            from app.core.protocol.mcp_client import MCPClientWrapper
            client = MCPClientWrapper(
                endpoint_url=endpoint,
                transport=self._config.get("transport", "sse"),
            )
            await client.connect()
            result = await client.call_tool(tool_name, arguments)
            await client.disconnect()
            return result
        except Exception as exc:
            logger.warning("Composio call_tool failed (SDK path): %s — trying REST", exc)
            return await self._rest_call_tool(tool_name, arguments)

    # ── REST fallback (Composio native API) ───────────────────────────────────

    async def _rest_list_tools(self) -> list[dict[str, Any]]:
        import httpx
        if not self._api_key:
            raise ValueError("Composio API key not configured")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://backend.composio.dev/api/v2/actions",
                headers={"x-api-key": self._api_key},
                params={"limit": 100},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
        return [
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "input_schema": item.get("parameters", {}),
            }
            for item in items
        ]

    async def _rest_call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        import httpx
        if not self._api_key:
            raise ValueError("Composio API key not configured")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"https://backend.composio.dev/api/v2/actions/{tool_name}/execute",
                headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
                json={"input": arguments},
            )
            resp.raise_for_status()
            data = resp.json()
        output = data.get("response") or data.get("output") or str(data)
        return {
            "content": [{"type": "text", "text": str(output)}],
            "isError": data.get("error") is not None,
        }
