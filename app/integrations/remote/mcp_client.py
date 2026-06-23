"""
app/integrations/remote/mcp_client.py — Generic remote MCP client.

Supports SSE and HTTP transports for any external MCP server.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RemoteMCPClient:
    """
    Thin wrapper around the shared remote connection pool for use in the execution
    router. Connections are reused per endpoint (lock-guarded) rather than rebuilt on
    every call — see ``app/core/protocol/connection_pool.py``.
    """

    def __init__(self, endpoint_url: str, transport: str = "sse") -> None:
        self._endpoint_url = endpoint_url
        self._transport = transport

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from app.core.protocol.connection_pool import remote_connection_pool

        return await remote_connection_pool.call_tool(
            self._endpoint_url, self._transport, tool_name, arguments
        )

    async def list_tools(self) -> list[dict[str, Any]]:
        from app.core.protocol.connection_pool import remote_connection_pool

        return await remote_connection_pool.list_tools(self._endpoint_url, self._transport)
