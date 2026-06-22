"""
app/core/runtime/router.py — Execution router: dispatches tool calls to the
correct runner based on server type.

Dispatch is table-driven (``server_type → handler``) rather than an if/elif chain,
and the stateless backend clients (devices, E2B) are constructed once and reused.
The remote/Composio wrappers are cheap to build because their HTTP now goes through
the shared pooled client (see ``app/core/http_client.py``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.core.exceptions import RoutingError
from app.models.mcp_server import ServerType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _RouteCtx:
    """Everything a runner might need, shaped once per invocation."""

    tool_name: str
    arguments: dict[str, Any]
    invocation_id: str
    org_id: str
    project_id: str | None
    username: str | None
    project: str | None
    category: str | None
    server_config: dict[str, Any]
    endpoint_url: str | None
    code: str | None
    server_type: str
    timeout: float


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

    def __init__(self) -> None:
        # server_type → bound handler. REMOTE_SSE/REMOTE_HTTP share one runner.
        self._handlers: dict[str, Callable[[_RouteCtx], Awaitable[dict[str, Any]]]] = {
            ServerType.LOCAL_PLUGIN: self._run_local,
            ServerType.REMOTE_SSE: self._run_remote,
            ServerType.REMOTE_HTTP: self._run_remote,
            ServerType.COMPOSIO: self._run_composio,
            ServerType.E2B: self._run_e2b,
            ServerType.DEVICE: self._run_device,
        }
        # Stateless clients — built lazily once and reused across invocations.
        self._devices_client: Any = None
        self._e2b_executor: Any = None

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
        handler = self._handlers.get(server_type)
        if handler is None:
            raise RoutingError(f"Unknown server type: {server_type}")

        ctx = _RouteCtx(
            tool_name=tool_name,
            arguments=arguments,
            invocation_id=invocation_id,
            org_id=org_id,
            project_id=project_id,
            username=username,
            project=project,
            category=category,
            server_config=server_config or {},
            endpoint_url=endpoint_url,
            code=code,
            server_type=server_type,
            timeout=timeout,
        )
        return await handler(ctx)

    # ── Runners ───────────────────────────────────────────────────────────────

    async def _run_local(self, ctx: _RouteCtx) -> dict[str, Any]:
        from app.core.runtime.local_runner import call_tool_direct
        from app.core.runtime.threadpool import run_in_threadpool

        return await asyncio.wait_for(
            run_in_threadpool(
                call_tool_direct,
                ctx.tool_name,
                ctx.arguments,
                ctx.username,
                ctx.project,
                ctx.category,
            ),
            timeout=ctx.timeout,
        )

    async def _run_remote(self, ctx: _RouteCtx) -> dict[str, Any]:
        from app.integrations.remote.mcp_client import RemoteMCPClient

        if not ctx.endpoint_url:
            raise RoutingError("Remote MCP server requires endpoint_url")

        transport = "sse" if ctx.server_type == ServerType.REMOTE_SSE else "http"
        client = RemoteMCPClient(endpoint_url=ctx.endpoint_url, transport=transport)
        return await asyncio.wait_for(
            client.call_tool(ctx.tool_name, ctx.arguments), timeout=ctx.timeout
        )

    async def _run_composio(self, ctx: _RouteCtx) -> dict[str, Any]:
        from app.integrations.composio.client import ComposioMCPClient

        client = ComposioMCPClient(config=ctx.server_config)
        return await asyncio.wait_for(
            client.call_tool(ctx.tool_name, ctx.arguments), timeout=ctx.timeout
        )

    async def _run_e2b(self, ctx: _RouteCtx) -> dict[str, Any]:
        if self._e2b_executor is None:
            from app.integrations.e2b.executor import E2BExecutor

            self._e2b_executor = E2BExecutor()
        return await asyncio.wait_for(
            self._e2b_executor.execute(
                code=ctx.code or ctx.arguments.get("code", ""),
                arguments=ctx.arguments,
                org_id=ctx.org_id,
                invocation_id=ctx.invocation_id,
            ),
            timeout=ctx.timeout,
        )

    async def _run_device(self, ctx: _RouteCtx) -> dict[str, Any]:
        if self._devices_client is None:
            from app.integrations.devices.client import DevicesClient

            self._devices_client = DevicesClient()
        return await asyncio.wait_for(
            self._devices_client.call_tool(
                ctx.tool_name, ctx.arguments, config=ctx.server_config
            ),
            timeout=ctx.timeout,
        )


execution_router = ExecutionRouter()
