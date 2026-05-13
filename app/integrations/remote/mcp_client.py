"""
app/integrations/remote/mcp_client.py — Generic remote MCP client.

Supports SSE and HTTP transports for any external MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.protocol.mcp_client import MCPClientWrapper

logger = logging.getLogger(__name__)


class RemoteMCPClient:
    """
    Thin wrapper around MCPClientWrapper for use in the execution router.
    Creates a fresh connection per call (stateless per invocation).
    """

    def __init__(self, endpoint_url: str, transport: str = "sse") -> None:
        self._endpoint_url = endpoint_url
        self._transport = transport

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        client = MCPClientWrapper(
            endpoint_url=self._endpoint_url,
            transport=self._transport,
        )
        try:
            await client.connect()
            result = await client.call_tool(tool_name, arguments)
            return result
        finally:
            await client.disconnect()

    async def list_tools(self) -> list[dict[str, Any]]:
        client = MCPClientWrapper(
            endpoint_url=self._endpoint_url,
            transport=self._transport,
        )
        try:
            await client.connect()
            return await client.list_tools()
        finally:
            await client.disconnect()
