"""
app/api/v1/health.py — Health check and server status endpoints.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import get_redis
from app.config import settings
from app.core.runtime import local_runner as _lr
from app.core.runtime.cancellation import cancellation_registry
from app.core.streaming.stream_manager import stream_manager
from app.db.database import get_db
from app.schemas.mcp import HealthStatus

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthStatus)
async def health_check(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> HealthStatus:
    """Overall service health — used by Render's health check."""
    redis_ok = False
    db_ok = False

    try:
        await redis.ping()
        redis_ok = True
    except Exception:
        pass

    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return HealthStatus(
        status="healthy" if (redis_ok and db_ok) else "degraded",
        environment=settings.environment,
        tools_loaded=len(_lr.TOOLS),
        redis_connected=redis_ok,
        db_connected=db_ok,
        active_connections=stream_manager.connection_count(),
        active_invocations=cancellation_registry.active_count(),
    )


@router.get("/liveness")
async def liveness() -> JSONResponse:
    """Kubernetes / Render liveness probe — returns 200 if process is alive."""
    return JSONResponse({"status": "alive"})


@router.get("/readiness")
async def readiness(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
) -> JSONResponse:
    """Readiness probe — returns 200 only when DB and Redis are reachable."""
    try:
        await redis.ping()
        await db.execute(text("SELECT 1"))
        return JSONResponse({"status": "ready"})
    except Exception as exc:
        logger.warning("Readiness check failed: %s", exc)
        return JSONResponse({"status": "not_ready", "error": str(exc)}, status_code=503)
