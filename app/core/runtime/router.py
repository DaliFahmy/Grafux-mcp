"""
app/core/runtime/router.py — Execution router: dispatches tool calls to the
correct runner based on server type.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.models.mcp_server import ServerType

logger = logging.getLogger(__name__)


class ExecutionRouter:
    """
    Routes a tool invocation to the appropriate execution backend:

      local_plugin  → LocalRunner (in-process Python)
      remote_sse    → RemoteMCPClient
      remote_http   → RemoteMCPClient
      composio      → ComposioClient
      e2b           → E2BSandboxExecutor
      device        → DevicesClient
    """

    async def route(
        self,
        *,
        server_type: str,
        tool_name: str,
        arguments: dict[str, Any],
        invocation_id: str,
        org_id: str,
        project_id: str | None = None,
        username: str | None = None,
        project: str | None = None,
        category: str | None = None,
        # Remote/Composio/Device require the server config
        server_config: dict[str, Any] | None = None,
        endpoint_url: str | None = None,
        # E2B requires optional code string
        code: str | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        if server_type == ServerType.LOCAL_PLUGIN:
            return await self._run_local(
                tool_name, arguments, username, project, category, timeout
            )

        if server_type in (ServerType.REMOTE_SSE, ServerType.REMOTE_HTTP):
            return await self._run_remote(
                tool_name, arguments, endpoint_url, server_type, timeout
            )

        if server_type == ServerType.COMPOSIO:
            return await self._run_composio(
                tool_name, arguments, server_config or {}, timeout
            )

        if server_type == ServerType.E2B:
            return await self._run_e2b(
                tool_name, arguments, code, org_id, invocation_id, timeout
            )

        if server_type == ServerType.DEVICE:
            return await self._run_device(
                tool_name, arguments, server_config or {}, timeout
            )

        raise ValueError(f"Unknown server type: {server_type}")

    # ── Runners ───────────────────────────────────────────────────────────────

    async def _run_local(
        self,
        tool_name: str,
        arguments: dict,
        username: str | None,
        project: str | None,
        category: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        from app.core.runtime.local_runner import call_tool_direct

        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_tool_direct(tool_name, arguments, username, project, category),
            ),
            timeout=timeout,
        )
        return result

    async def _run_remote(
        self,
        tool_name: str,
        arguments: dict,
        endpoint_url: str | None,
        server_type: str,
        timeout: float,
    ) -> dict[str, Any]:
        from app.integrations.remote.mcp_client import RemoteMCPClient

        if not endpoint_url:
            raise ValueError("Remote MCP server requires endpoint_url")

        transport = "sse" if server_type == ServerType.REMOTE_SSE else "http"
        client = RemoteMCPClient(endpoint_url=endpoint_url, transport=transport)
        return await asyncio.wait_for(
            client.call_tool(tool_name, arguments), timeout=timeout
        )

    async def _run_composio(
        self,
        tool_name: str,
        arguments: dict,
        server_config: dict,
        timeout: float,
    ) -> dict[str, Any]:
        from app.integrations.composio.client import ComposioMCPClient

        client = ComposioMCPClient(config=server_config)
        return await asyncio.wait_for(
            client.call_tool(tool_name, arguments), timeout=timeout
        )

    async def _run_e2b(
        self,
        tool_name: str,
        arguments: dict,
        code: str | None,
        org_id: str,
        invocation_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        from app.integrations.e2b.executor import E2BExecutor

        executor = E2BExecutor()
        return await asyncio.wait_for(
            executor.execute(
                code=code or arguments.get("code", ""),
                arguments=arguments,
                org_id=org_id,
                invocation_id=invocation_id,
            ),
            timeout=timeout,
        )

    async def _run_device(
        self,
        tool_name: str,
        arguments: dict,
        server_config: dict,
        timeout: float,
    ) -> dict[str, Any]:
        from app.integrations.devices.client import DevicesClient

        client = DevicesClient()
        return await asyncio.wait_for(
            client.call_tool(tool_name, arguments, config=server_config), timeout=timeout
        )


execution_router = ExecutionRouter()
