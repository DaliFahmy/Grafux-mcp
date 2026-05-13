"""
app/services/session_service.py — MCP session management.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.keys import MCPKeys, TTL_SESSION
from app.cache.redis_client import cache_delete, cache_set
from app.models.session import MCPSession


class SessionService:
    async def create(
        self,
        org_id: uuid.UUID,
        project_id: uuid.UUID | None,
        user_id: str | None,
        server_ids: list[uuid.UUID],
        ttl_seconds: int,
        db: AsyncSession,
        redis: Any,
    ) -> MCPSession:
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        session = MCPSession(
            org_id=org_id,
            project_id=project_id,
            user_id=user_id,
            server_ids=[str(sid) for sid in server_ids],
            expires_at=expires,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        await cache_set(
            MCPKeys.session(str(session.id)),
            {"session_id": str(session.id), "org_id": str(org_id), "user_id": user_id},
            ttl=ttl_seconds,
            redis=redis,
        )
        return session

    async def get(
        self, session_id: uuid.UUID, org_id: uuid.UUID, db: AsyncSession
    ) -> MCPSession | None:
        result = await db.execute(
            select(MCPSession).where(
                MCPSession.id == session_id,
                MCPSession.org_id == org_id,
            )
        )
        return result.scalar_one_or_none()

    async def end(
        self, session: MCPSession, db: AsyncSession, redis: Any
    ) -> None:
        await db.delete(session)
        await db.commit()
        await cache_delete(MCPKeys.session(str(session.id)), redis=redis)


session_service = SessionService()
