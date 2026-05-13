"""
app/dependencies.py — FastAPI dependency injection providers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Depends
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import get_redis_pool
from app.db.database import get_db as _get_db
from app.security.auth import AuthContext, get_auth_context


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async for session in _get_db():
        yield session


async def get_redis() -> Redis:
    return get_redis_pool()


async def get_current_user(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    return auth
