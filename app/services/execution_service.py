"""
app/services/execution_service.py — Async invocation lifecycle engine.

Handles:
- Invocation creation and persistence
- Routing to the correct executor backend
- Retry and timeout logic
- Cancellation
- Streaming events via Redis pub/sub
- Audit logging
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys
from app.cache.redis_client import cache_set
from app.config import settings
from app.core.runtime.cancellation import cancellation_registry
from app.core.runtime.router import execution_router
from app.core.streaming.event_bus import event_bus
from app.models.invocation import InvocationStatus, MCPInvocation, ToolExecutionLog
from app.models.mcp_server import MCPServer, ServerType
from app.models.mcp_tool import MCPTool

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


class ExecutionService:
    """
    Orchestrates the full lifecycle of a tool invocation.

    Sync path: await invoke() — blocks until the tool returns.
    Async path: await invoke_async() — persists invocation and fires off
    a background task; caller polls GET /execute/{id} or streams /ws.
    """

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        org_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
        session_id: uuid.UUID | None = None,
        tool_id: uuid.UUID | None = None,
        username: str | None = None,
        project: str | None = None,
        category: str | None = None,
        timeout: float | None = None,
        db: AsyncSession,
        redis: Any,
    ) -> dict[str, Any]:
        """Execute a tool synchronously and return the result."""
        invocation = await self._create_invocation(
            tool_name=tool_name,
            arguments=arguments,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            tool_id=tool_id,
            db=db,
        )
        invocation_id = str(invocation.id)

        cancel_event = cancellation_registry.register(invocation_id)

        try:
            result = await self._run(
                invocation=invocation,
                arguments=arguments,
                username=username,
                project=project,
                category=category,
                timeout=timeout or settings.default_tool_timeout,
                db=db,
                redis=redis,
                cancel_event=cancel_event,
            )
            return result
        finally:
            cancellation_registry.unregister(invocation_id)

    async def invoke_async(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        org_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
        session_id: uuid.UUID | None = None,
        tool_id: uuid.UUID | None = None,
        username: str | None = None,
        project: str | None = None,
        category: str | None = None,
        timeout: float | None = None,
        db: AsyncSession,
        redis: Any,
    ) -> uuid.UUID:
        """
        Start a tool execution in the background and return the invocation_id.
        The caller can poll GET /execute/{id} or subscribe via WebSocket.
        """
        invocation = await self._create_invocation(
            tool_name=tool_name,
            arguments=arguments,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            tool_id=tool_id,
            db=db,
        )

        # Fire and forget — a new DB session is opened inside the task
        asyncio.create_task(
            self._run_background(
                invocation_id=invocation.id,
                tool_name=tool_name,
                arguments=arguments,
                org_id=org_id,
                username=username,
                project=project,
                category=category,
                timeout=timeout or settings.default_tool_timeout,
                redis=redis,
            ),
            name=f"invocation-{invocation.id}",
        )
        return invocation.id

    def cancel(self, invocation_id: str) -> bool:
        return cancellation_registry.cancel(invocation_id)

    async def get_invocation(
        self, invocation_id: uuid.UUID, org_id: str, db: AsyncSession
    ) -> MCPInvocation | None:
        result = await db.execute(
            select(MCPInvocation).where(
                MCPInvocation.id == invocation_id,
                MCPInvocation.org_id == uuid.UUID(org_id),
            )
        )
        return result.scalar_one_or_none()

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _create_invocation(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        org_id: str,
        project_id: str | None,
        user_id: str | None,
        session_id: uuid.UUID | None,
        tool_id: uuid.UUID | None,
        db: AsyncSession,
    ) -> MCPInvocation:
        # Determine server type from tool_id if provided
        server_type = "local_plugin"
        if tool_id:
            result = await db.execute(
                select(MCPTool).where(MCPTool.id == tool_id)
            )
            tool = result.scalar_one_or_none()
            if tool:
                srv_result = await db.execute(
                    select(MCPServer).where(MCPServer.id == tool.server_id)
                )
                srv = srv_result.scalar_one_or_none()
                if srv:
                    server_type = srv.server_type

        invocation = MCPInvocation(
            org_id=uuid.UUID(org_id),
            project_id=uuid.UUID(project_id) if project_id else None,
            user_id=user_id,
            session_id=session_id,
            tool_id=tool_id,
            tool_name=tool_name,
            server_type=server_type,
            status=InvocationStatus.PENDING,
            input=arguments,
        )
        db.add(invocation)
        await db.commit()
        await db.refresh(invocation)
        return invocation

    async def _run(
        self,
        *,
        invocation: MCPInvocation,
        arguments: dict[str, Any],
        username: str | None,
        project: str | None,
        category: str | None,
        timeout: float,
        db: AsyncSession,
        redis: Any,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        invocation_id = str(invocation.id)

        # Mark running
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == invocation.id)
            .values(status=InvocationStatus.RUNNING, started_at=datetime.now(timezone.utc))
        )
        await db.commit()
        await event_bus.publish_log(invocation_id, "info", f"Starting tool: {invocation.tool_name}")

        # Resolve routing information
        server_type, endpoint_url, server_config = await self._resolve_routing(
            invocation, db
        )

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            if cancel_event.is_set():
                await self._mark_cancelled(invocation, db)
                return {"content": [{"type": "text", "text": "Invocation cancelled"}], "isError": True}

            try:
                if attempt > 0:
                    await event_bus.publish_log(
                        invocation_id, "warn", f"Retry {attempt}/{MAX_RETRIES}"
                    )

                result = await execution_router.route(
                    server_type=server_type,
                    tool_name=invocation.tool_name,
                    arguments=arguments,
                    invocation_id=invocation_id,
                    org_id=str(invocation.org_id),
                    project_id=str(invocation.project_id) if invocation.project_id else None,
                    username=username,
                    project=project,
                    category=category,
                    server_config=server_config,
                    endpoint_url=endpoint_url,
                    timeout=timeout,
                )

                await self._mark_done(invocation, result, db)
                await event_bus.publish_result(invocation_id, result)

                # Audit log
                from app.services.audit_service import audit_service
                await audit_service.log(invocation_id, "info", "Tool completed successfully", redis=redis)

                return result

            except asyncio.TimeoutError:
                last_error = TimeoutError(f"Tool '{invocation.tool_name}' timed out after {timeout}s")
                await event_bus.publish_log(invocation_id, "error", str(last_error))
                if attempt >= MAX_RETRIES:
                    break
                await asyncio.sleep(2 ** attempt)

            except asyncio.CancelledError:
                await self._mark_cancelled(invocation, db)
                return {"content": [{"type": "text", "text": "Invocation cancelled"}], "isError": True}

            except Exception as exc:
                last_error = exc
                await event_bus.publish_log(invocation_id, "error", str(exc))
                if attempt >= MAX_RETRIES:
                    break
                await asyncio.sleep(2 ** attempt)

        # All retries exhausted
        error_msg = str(last_error) if last_error else "Unknown error"
        await self._mark_failed(invocation, error_msg, db)
        error_result = {
            "content": [{"type": "text", "text": error_msg}],
            "isError": True,
        }
        await event_bus.publish_error(invocation_id, error_msg)
        return error_result

    async def _run_background(
        self,
        *,
        invocation_id: uuid.UUID,
        tool_name: str,
        arguments: dict[str, Any],
        org_id: str,
        username: str | None,
        project: str | None,
        category: str | None,
        timeout: float,
        redis: Any,
    ) -> None:
        """Background task wrapper — opens its own DB session."""
        from app.db.database import AsyncSessionLocal

        cancel_event = cancellation_registry.register(str(invocation_id))
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(MCPInvocation).where(MCPInvocation.id == invocation_id)
                )
                invocation = result.scalar_one_or_none()
                if not invocation:
                    return
                await self._run(
                    invocation=invocation,
                    arguments=arguments,
                    username=username,
                    project=project,
                    category=category,
                    timeout=timeout,
                    db=db,
                    redis=redis,
                    cancel_event=cancel_event,
                )
        except Exception as exc:
            logger.error("Background invocation %s failed: %s", invocation_id, exc)
        finally:
            cancellation_registry.unregister(str(invocation_id))

    async def _resolve_routing(
        self, invocation: MCPInvocation, db: AsyncSession
    ) -> tuple[str, str | None, dict | None]:
        """Return (server_type, endpoint_url, server_config) for this invocation."""
        if not invocation.tool_id:
            return ServerType.LOCAL_PLUGIN, None, None

        result = await db.execute(
            select(MCPTool).where(MCPTool.id == invocation.tool_id)
        )
        tool = result.scalar_one_or_none()
        if not tool:
            return ServerType.LOCAL_PLUGIN, None, None

        srv_result = await db.execute(
            select(MCPServer).where(MCPServer.id == tool.server_id)
        )
        server = srv_result.scalar_one_or_none()
        if not server:
            return ServerType.LOCAL_PLUGIN, None, None

        return server.server_type, server.endpoint_url, server.config

    async def _mark_done(
        self, invocation: MCPInvocation, result: dict, db: AsyncSession
    ) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == invocation.id)
            .values(
                status=InvocationStatus.DONE,
                output=result,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    async def _mark_failed(
        self, invocation: MCPInvocation, error: str, db: AsyncSession
    ) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == invocation.id)
            .values(
                status=InvocationStatus.FAILED,
                error_message=error,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    async def _mark_cancelled(
        self, invocation: MCPInvocation, db: AsyncSession
    ) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == invocation.id)
            .values(
                status=InvocationStatus.CANCELLED,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


execution_service = ExecutionService()
