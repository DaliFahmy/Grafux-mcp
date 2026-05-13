"""
app/api/v1/sessions.py — MCP session lifecycle endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys, TTL_SESSION
from app.cache.redis_client import cache_delete, cache_set, get_redis
from app.db.database import get_db
from app.models.session import MCPSession
from app.schemas.mcp import MCPSessionCreate, MCPSessionResponse
from app.security.auth import AuthContext, get_auth_context
from app.services.session_service import session_service

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=MCPSessionResponse, status_code=201)
async def create_session(
    body: MCPSessionCreate,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Create a new MCP session scoped to an org/project."""
    session = await session_service.create(
        org_id=uuid.UUID(auth.org_id),
        project_id=body.project_id or (uuid.UUID(auth.project_id) if auth.project_id else None),
        user_id=auth.user_id,
        server_ids=body.server_ids,
        ttl_seconds=body.ttl_seconds,
        db=db,
        redis=redis,
    )
    return MCPSessionResponse.model_validate(session)


@router.get("/{session_id}", response_model=MCPSessionResponse)
async def get_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    session = await session_service.get(session_id, uuid.UUID(auth.org_id), db)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return MCPSessionResponse.model_validate(session)


@router.delete("/{session_id}", status_code=204)
async def end_session(
    session_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    session = await session_service.get(session_id, uuid.UUID(auth.org_id), db)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session_service.end(session, db, redis)
