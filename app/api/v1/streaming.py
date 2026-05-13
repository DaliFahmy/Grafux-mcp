"""
app/api/v1/streaming.py — SSE streaming endpoints for invocation logs.
"""

from __future__ import annotations

import asyncio
import json
import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.streaming.event_bus import event_bus
from app.db.database import get_db
from app.security.auth import AuthContext, get_auth_context
from app.services.execution_service import execution_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stream", tags=["streaming"])


@router.get("/{invocation_id}/events")
async def stream_invocation_events(
    invocation_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE endpoint — streams live execution events for an invocation.

    Connect to this endpoint to receive real-time progress logs,
    stdout/stderr from sandboxes, and the final result.
    """
    inv = await execution_service.get_invocation(invocation_id, auth.org_id, db)
    if not inv:
        raise HTTPException(status_code=404, detail="Invocation not found")

    async def event_generator():
        yield f"data: {json.dumps({'type': 'connected', 'invocation_id': str(invocation_id)})}\n\n"
        try:
            async for event in event_bus.subscribe(str(invocation_id), timeout=300.0):
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("final"):
                    break
        except asyncio.CancelledError:
            pass
        yield f"data: {json.dumps({'type': 'closed'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
