"""
app/api/v1/execution.py — Tool execution endpoints (sync + async + cancel + logs).
"""

from __future__ import annotations

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import get_redis
from app.db.database import get_db
from app.schemas.execution import (
    AsyncExecuteRequest,
    AsyncInvocationStarted,
    ExecuteRequest,
    InvocationResponse,
)
from app.security.auth import AuthContext, get_auth_context
from app.services.execution_service import execution_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/execute", tags=["execution"])


@router.post("", response_model=dict)
async def execute_tool(
    body: ExecuteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Execute a tool synchronously.

    Blocks until the tool completes (up to timeout seconds).
    Returns the tool result directly.
    """
    result = await execution_service.invoke(
        tool_name=body.tool_name,
        arguments=body.arguments,
        org_id=auth.org_id,
        project_id=str(body.project_id) if body.project_id else auth.project_id,
        user_id=auth.user_id,
        tool_id=body.tool_id,
        username=body.username,
        project=body.project,
        category=body.category,
        timeout=body.timeout,
        db=db,
        redis=redis,
    )
    return result


@router.post("/async", response_model=AsyncInvocationStarted, status_code=status.HTTP_202_ACCEPTED)
async def execute_tool_async(
    body: AsyncExecuteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Start a tool execution asynchronously.

    Returns an invocation_id immediately. Subscribe to /ws or poll
    GET /execute/{id} to get the result.
    """
    invocation_id = await execution_service.invoke_async(
        tool_name=body.tool_name,
        arguments=body.arguments,
        org_id=auth.org_id,
        project_id=str(body.project_id) if body.project_id else auth.project_id,
        user_id=auth.user_id,
        tool_id=body.tool_id,
        username=body.username,
        project=body.project,
        category=body.category,
        timeout=body.timeout,
        db=db,
        redis=redis,
    )
    return AsyncInvocationStarted(invocation_id=invocation_id)


@router.get("/{invocation_id}", response_model=InvocationResponse)
async def get_invocation(
    invocation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Poll for the status and result of an async invocation."""
    inv = await execution_service.get_invocation(invocation_id, auth.org_id, db)
    if not inv:
        raise HTTPException(status_code=404, detail="Invocation not found")
    return InvocationResponse.model_validate(inv)


@router.delete("/{invocation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_invocation(
    invocation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a running invocation."""
    inv = await execution_service.get_invocation(invocation_id, auth.org_id, db)
    if not inv:
        raise HTTPException(status_code=404, detail="Invocation not found")
    cancelled = execution_service.cancel(str(invocation_id))
    if not cancelled:
        raise HTTPException(status_code=409, detail="Invocation is not running or already completed")
