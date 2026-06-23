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
import random
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import settings
from app.core.errors import tool_error
from app.core.exceptions import RemoteCallError
from app.core.runtime.cancellation import cancellation_registry
from app.core.runtime.router import execution_router
from app.core.streaming.event_bus import event_bus
from app.models.invocation import InvocationStatus, MCPInvocation
from app.models.mcp_server import ServerType
from app.models.mcp_tool import MCPTool
from app.shared.factories import utcnow

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

# Only retry failures that are plausibly transient. Retrying a deterministic
# error (bad tool, validation, auth, routing) just wastes time and — worse — can
# run a non-idempotent tool two or three times.
_TRANSIENT_ERRORS = (TimeoutError, ConnectionError, OSError, RemoteCallError)


def _is_transient(exc: BaseException) -> bool:
    return isinstance(exc, _TRANSIENT_ERRORS)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter to avoid a retry thundering herd."""
    return (2**attempt) + random.uniform(0, 0.5)


@dataclass(frozen=True)
class RoutingInfo:
    """Where and how to run an invocation — resolved once, then threaded through.

    Holds only plain values (no ORM objects), so it is safe to pass into a
    background task running on a different DB session.
    """

    server_type: str
    endpoint_url: str | None = None
    server_config: dict | None = None


@dataclass(frozen=True)
class _InvocationRef:
    """The few invocation fields ``_run`` actually needs, as plain values.

    Carrying these instead of the ORM row lets the background task skip re-SELECTing
    the invocation it just created (every status write is an ``update()...where(id==)``,
    so no live ORM object is required). Safe across DB sessions and event loops.
    """

    id: uuid.UUID
    tool_name: str
    org_id: str
    project_id: str | None = None


class ExecutionService:
    """
    Orchestrates the full lifecycle of a tool invocation.

    Sync path: await invoke() — blocks until the tool returns.
    Async path: await invoke_async() — persists invocation and fires off
    a background task; caller polls GET /execute/{id} or streams /ws.
    """

    def __init__(self) -> None:
        # Gate concurrent tool executions so an overload queues instead of
        # exhausting DB connections / the thread pool / the event loop. Created
        # lazily on first use so it binds to the running loop (and so a config
        # override applied before startup is honoured).
        self._semaphore: asyncio.Semaphore | None = None
        # Strong references to in-flight background invocation tasks, so they are
        # not garbage-collected mid-run and their failures are always observed.
        self._background_tasks: set[asyncio.Task] = set()

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(settings.max_concurrent_invocations)
        return self._semaphore

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
        routing = await self._resolve_tool_routing(tool_id, db)
        invocation = await self._create_invocation(
            tool_name=tool_name,
            arguments=arguments,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            tool_id=tool_id,
            server_type=routing.server_type,
            db=db,
        )
        invocation_id = str(invocation.id)
        ref = _InvocationRef(
            id=invocation.id,
            tool_name=tool_name,
            org_id=org_id,
            project_id=project_id,
        )

        cancel_event = cancellation_registry.register(invocation_id)

        try:
            return await self._run(
                ref=ref,
                arguments=arguments,
                routing=routing,
                username=username,
                project=project,
                category=category,
                timeout=timeout or settings.default_tool_timeout,
                db=db,
                redis=redis,
                cancel_event=cancel_event,
            )
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
        routing = await self._resolve_tool_routing(tool_id, db)
        invocation = await self._create_invocation(
            tool_name=tool_name,
            arguments=arguments,
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            tool_id=tool_id,
            server_type=routing.server_type,
            db=db,
        )

        # A new DB session is opened inside the task. Routing and the invocation
        # identity are already resolved to plain values, so the task never re-queries
        # for either. The task is tracked so it is not GC'd mid-run and so its
        # failures are always logged.
        ref = _InvocationRef(
            id=invocation.id,
            tool_name=tool_name,
            org_id=org_id,
            project_id=project_id,
        )
        task = asyncio.create_task(
            self._run_background(
                ref=ref,
                arguments=arguments,
                routing=routing,
                username=username,
                project=project,
                category=category,
                timeout=timeout or settings.default_tool_timeout,
                redis=redis,
            ),
            name=f"invocation-{invocation.id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_done)
        return invocation.id

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Background invocation task %s crashed: %s", task.get_name(), exc, exc_info=exc
            )

    async def shutdown(self) -> None:
        """Cancel and drain any in-flight background invocations (lifespan shutdown)."""
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

    async def _resolve_tool_routing(
        self, tool_id: uuid.UUID | None, db: AsyncSession
    ) -> RoutingInfo:
        """Resolve (server_type, endpoint_url, config) for a tool in a single query.

        Uses a joined eager load so the tool and its server come back together,
        replacing the previous two-query-twice pattern. Falls back to local-plugin
        routing when there is no tool_id or the row is missing.
        """
        if not tool_id:
            return RoutingInfo(ServerType.LOCAL_PLUGIN)

        result = await db.execute(
            select(MCPTool).options(joinedload(MCPTool.server)).where(MCPTool.id == tool_id)
        )
        tool = result.scalar_one_or_none()
        if tool is None or tool.server is None:
            return RoutingInfo(ServerType.LOCAL_PLUGIN)

        server = tool.server
        return RoutingInfo(server.server_type, server.endpoint_url, server.config)

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
        server_type: str,
        db: AsyncSession,
    ) -> MCPInvocation:
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
        ref: _InvocationRef,
        arguments: dict[str, Any],
        routing: RoutingInfo,
        username: str | None,
        project: str | None,
        category: str | None,
        timeout: float,
        db: AsyncSession,
        redis: Any,
        cancel_event: asyncio.Event,
    ) -> dict[str, Any]:
        invocation_id = str(ref.id)

        # Gate concurrent executions. Under normal load this acquires immediately;
        # under overload the invocation waits here (back-pressure) instead of piling
        # on. The invocation is only marked RUNNING once it actually holds a slot.
        async with self._get_semaphore():
            await db.execute(
                update(MCPInvocation)
                .where(MCPInvocation.id == ref.id)
                .values(status=InvocationStatus.RUNNING, started_at=utcnow())
            )
            await db.commit()
            await event_bus.publish_log(invocation_id, "info", f"Starting tool: {ref.tool_name}")

            last_error: Exception | None = None
            for attempt in range(MAX_RETRIES + 1):
                if cancel_event.is_set():
                    await self._mark_cancelled(ref, db)
                    return tool_error("Invocation cancelled")

                try:
                    if attempt > 0:
                        await event_bus.publish_log(
                            invocation_id, "warn", f"Retry {attempt}/{MAX_RETRIES}"
                        )

                    result = await execution_router.route(
                        server_type=routing.server_type,
                        tool_name=ref.tool_name,
                        arguments=arguments,
                        invocation_id=invocation_id,
                        org_id=ref.org_id,
                        project_id=ref.project_id,
                        username=username,
                        project=project,
                        category=category,
                        server_config=routing.server_config,
                        endpoint_url=routing.endpoint_url,
                        timeout=timeout,
                    )

                    await self._mark_done(ref, result, db)
                    await event_bus.publish_result(invocation_id, result)

                    from app.services.audit_service import audit_service

                    await audit_service.log(
                        invocation_id, "info", "Tool completed successfully", redis=redis
                    )

                    return result

                except asyncio.CancelledError:
                    await self._mark_cancelled(ref, db)
                    return tool_error("Invocation cancelled")

                except TimeoutError:
                    last_error = TimeoutError(f"Tool '{ref.tool_name}' timed out after {timeout}s")
                    await event_bus.publish_log(invocation_id, "error", str(last_error))
                    if attempt >= MAX_RETRIES:
                        break
                    await asyncio.sleep(_backoff_seconds(attempt))

                except Exception as exc:
                    last_error = exc
                    await event_bus.publish_log(invocation_id, "error", str(exc))
                    # Fail fast on deterministic errors; only retry transient ones.
                    if attempt >= MAX_RETRIES or not _is_transient(exc):
                        break
                    await asyncio.sleep(_backoff_seconds(attempt))

            # All retries exhausted (or a non-transient error broke the loop).
            error_msg = str(last_error) if last_error else "Unknown error"
            await self._mark_failed(ref, error_msg, db)
            await event_bus.publish_error(invocation_id, error_msg)
            return tool_error(error_msg)

    async def _run_background(
        self,
        *,
        ref: _InvocationRef,
        arguments: dict[str, Any],
        routing: RoutingInfo,
        username: str | None,
        project: str | None,
        category: str | None,
        timeout: float,
        redis: Any,
    ) -> None:
        """Background task wrapper — opens its own DB session.

        The invocation was just created and committed by ``invoke_async``; its needed
        fields are carried in ``ref``, so there is no re-SELECT here. Status writes are
        ``update()...where(id==)`` and simply affect zero rows if the row is gone.
        """
        from app.db.database import AsyncSessionLocal

        cancel_event = cancellation_registry.register(str(ref.id))
        try:
            async with AsyncSessionLocal() as db:
                await self._run(
                    ref=ref,
                    arguments=arguments,
                    routing=routing,
                    username=username,
                    project=project,
                    category=category,
                    timeout=timeout,
                    db=db,
                    redis=redis,
                    cancel_event=cancel_event,
                )
        except Exception as exc:
            logger.error("Background invocation %s failed: %s", ref.id, exc)
        finally:
            cancellation_registry.unregister(str(ref.id))

    async def _mark_done(self, ref: _InvocationRef, result: dict, db: AsyncSession) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == ref.id)
            .values(
                status=InvocationStatus.DONE,
                output=result,
                completed_at=utcnow(),
            )
        )
        await db.commit()

    async def _mark_failed(self, ref: _InvocationRef, error: str, db: AsyncSession) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == ref.id)
            .values(
                status=InvocationStatus.FAILED,
                error_message=error,
                completed_at=utcnow(),
            )
        )
        await db.commit()

    async def _mark_cancelled(self, ref: _InvocationRef, db: AsyncSession) -> None:
        await db.execute(
            update(MCPInvocation)
            .where(MCPInvocation.id == ref.id)
            .values(
                status=InvocationStatus.CANCELLED,
                completed_at=utcnow(),
            )
        )
        await db.commit()


execution_service = ExecutionService()
