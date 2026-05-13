"""
app/core/protocol/mcp_client.py — MCP SDK client wrapper for remote MCP servers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPClientWrapper:
    """
    Wraps the MCP SDK ClientSession for connecting to remote MCP servers.

    Supports SSE and HTTP transports. Handles tool listing and invocation.
    """

    def __init__(self, endpoint_url: str, transport: str = "sse") -> None:
        self.endpoint_url = endpoint_url
        self.transport = transport
        self._session: Any = None

    async def connect(self) -> dict[str, Any]:
        """Connect to the MCP server and return its capabilities."""
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.streamable_http import streamablehttp_client

            if self.transport == "sse":
                transport_ctx = sse_client(self.endpoint_url)
            else:
                transport_ctx = streamablehttp_client(self.endpoint_url)

            read, write = await transport_ctx.__aenter__()
            session = ClientSession(read, write)
            await session.__aenter__()
            result = await session.initialize()
            self._session = (session, transport_ctx)
            return {
                "name": result.serverInfo.name,
                "version": result.serverInfo.version,
                "capabilities": result.capabilities.model_dump() if result.capabilities else {},
            }
        except ImportError:
            logger.warning("MCP SDK not installed — using HTTP fallback for %s", self.endpoint_url)
            return await self._http_probe()
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to MCP server {self.endpoint_url}: {exc}") from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        """Discover all tools from the connected MCP server."""
        try:
            if self._session:
                session, _ = self._session
                result = await session.list_tools()
                return [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "input_schema": t.inputSchema if isinstance(t.inputSchema, dict) else {},
                    }
                    for t in result.tools
                ]
        except Exception as exc:
            logger.warning("list_tools via SDK failed: %s, falling back to HTTP", exc)

        return await self._http_list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke a tool on the remote MCP server."""
        try:
            if self._session:
                session, _ = self._session
                result = await session.call_tool(name, arguments)
                return {
                    "content": [
                        {"type": c.type, "text": getattr(c, "text", str(c))}
                        for c in result.content
                    ],
                    "isError": result.isError if hasattr(result, "isError") else False,
                }
        except Exception as exc:
            logger.warning("call_tool via SDK failed: %s, falling back to HTTP", exc)

        return await self._http_call_tool(name, arguments)

    async def disconnect(self) -> None:
        if self._session:
            try:
                session, transport_ctx = self._session
                await session.__aexit__(None, None, None)
                await transport_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._session = None

    # ── HTTP fallback (when MCP SDK not available or server uses REST) ────────

    async def _http_probe(self) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(f"{self.endpoint_url}/")
                if resp.status_code < 300:
                    data = resp.json()
                    return {"name": data.get("service", "unknown"), "version": "?", "capabilities": {}}
            except Exception:
                pass
        return {"name": "unknown", "version": "?", "capabilities": {}}

    async def _http_list_tools(self) -> list[dict[str, Any]]:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.endpoint_url}/api/tools")
            resp.raise_for_status()
            data = resp.json()
            return data.get("tools", [])

    async def _http_call_tool(self, name: str, arguments: dict) -> dict[str, Any]:
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.endpoint_url}/api/tools/{name}",
                json={"arguments": arguments},
            )
            resp.raise_for_status()
            return resp.json()
