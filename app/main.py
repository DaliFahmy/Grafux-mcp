"""
app/main.py — FastAPI application factory, lifespan, and router registration.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings

# ── Logging setup ─────────────────────────────────────────────────────────────

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.dev.ConsoleRenderer() if not settings.is_production else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    stream=sys.stderr,
)

logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Grafux-mcp starting (env=%s)", settings.environment)

    # ── Startup ───────────────────────────────────────────────────────────────

    # 1. Verify Redis connectivity
    from app.cache.redis_client import get_redis_pool
    try:
        redis = get_redis_pool()
        await redis.ping()
        logger.info("Redis connected: %s", settings.redis_url)
    except Exception as exc:
        logger.warning("Redis not available at startup: %s", exc)

    # 2. Load local tool plugins in background thread
    from app.core.runtime.local_runner import start_plugin_loader
    start_plugin_loader()

    # 3. Sync tools from S3 (if configured)
    if settings.aws_access_key_id:
        logger.info("Starting S3 tool sync...")
        from app.core.runtime.local_runner import s3_syncer
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        try:
            result = await _asyncio.wait_for(
                loop.run_in_executor(None, s3_syncer.sync_all),
                timeout=120.0,
            )
            if result.get("success"):
                logger.info(
                    "S3 sync complete: %d synced, %d tools loaded",
                    result.get("files_synced", 0),
                    result.get("tools_loaded", 0),
                )
        except asyncio.TimeoutError:
            logger.warning("S3 sync timed out — continuing with local tools")
        except Exception as exc:
            logger.warning("S3 sync failed: %s", exc)

        # Start periodic S3 sync task
        if settings.s3_sync_interval > 0:
            asyncio.create_task(_periodic_s3_sync(), name="periodic-s3-sync")

    # 4. Start health monitor
    from app.core.lifecycle.health_monitor import health_monitor
    health_monitor.start()

    logger.info("Grafux-mcp startup complete")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────

    logger.info("Grafux-mcp shutting down...")

    from app.core.lifecycle.health_monitor import health_monitor
    await health_monitor.stop()

    from app.core.lifecycle.server_manager import _active_connections
    for server_id, client in list(_active_connections.items()):
        try:
            await client.disconnect()
        except Exception:
            pass

    from app.cache.redis_client import close_redis_pool
    await close_redis_pool()

    logger.info("Grafux-mcp shutdown complete")


async def _periodic_s3_sync() -> None:
    """Re-sync tools from S3 at a fixed interval."""
    await asyncio.sleep(60)  # Brief delay so startup sync doesn't overlap
    from app.core.runtime.local_runner import s3_syncer
    loop = asyncio.get_event_loop()
    while True:
        try:
            await asyncio.sleep(settings.s3_sync_interval)
            await loop.run_in_executor(None, s3_syncer.sync_all)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Periodic S3 sync error: %s", exc)
            await asyncio.sleep(300)


# ── Application factory ───────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Grafux-mcp",
        description="MCP Gateway, Registry, and Distributed Tool Execution Runtime",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    origins = settings.allowed_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Rate limiting ─────────────────────────────────────────────────────────
    try:
        from slowapi import Limiter, _rate_limit_exceeded_handler
        from slowapi.errors import RateLimitExceeded
        from slowapi.util import get_remote_address

        limiter = Limiter(key_func=get_remote_address)
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    except ImportError:
        logger.warning("slowapi not installed — rate limiting disabled")

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.api.v1.health import router as health_router
    from app.api.v1.registry import router as registry_router
    from app.api.v1.discovery import router as discovery_router
    from app.api.v1.execution import router as execution_router
    from app.api.v1.sessions import router as sessions_router
    from app.api.v1.streaming import router as streaming_router
    from app.api.ws.gateway import router as ws_router

    v1_prefix = "/api/v1"
    app.include_router(health_router, prefix=v1_prefix)
    app.include_router(registry_router, prefix=v1_prefix)
    app.include_router(discovery_router, prefix=v1_prefix)
    app.include_router(execution_router, prefix=v1_prefix)
    app.include_router(sessions_router, prefix=v1_prefix)
    app.include_router(streaming_router, prefix=v1_prefix)
    app.include_router(ws_router)  # /ws at root (backward compat)

    # ── Root endpoint (backward compat with v1 /  response) ──────────────────
    @app.get("/")
    async def root():
        return {
            "service": "Grafux-mcp",
            "version": "2.0.0",
            "status": "running",
            "docs": "/docs",
            "endpoints": {
                "health": "/api/v1/health",
                "tools": "/api/v1/tools",
                "execute": "/api/v1/execute",
                "registry": "/api/v1/registry/servers",
                "sessions": "/api/v1/sessions",
                "websocket": "/ws",
            },
        }

    # Legacy v1 endpoints for backward compat (redirect to new paths)
    @app.get("/health")
    async def legacy_health():
        from app.core.runtime import local_runner as _lr
        return {
            "status": "healthy",
            "initialized": _lr._plugins_loaded.is_set(),
            "tools_count": len(_lr.TOOLS),
        }

    @app.get("/api/tools")
    async def legacy_list_tools():
        from app.core.runtime.local_runner import list_local_tools
        return {"tools": list_local_tools()}

    @app.post("/api/tools/{tool_name}")
    async def legacy_call_tool(tool_name: str, body: dict = None):
        from app.core.runtime.local_runner import call_tool_direct
        args = body or {}
        username = args.pop("username", None)
        project = args.pop("project", None)
        category = args.pop("category", None)
        arguments = args.pop("arguments", args)
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_tool_direct(tool_name, arguments, username, project, category),
            ),
            timeout=120.0,
        )

    @app.post("/reload-tools")
    async def legacy_reload_tools():
        from app.core.runtime.local_runner import reload_plugins
        loop = asyncio.get_event_loop()
        counts = await loop.run_in_executor(None, reload_plugins)
        return {"success": True, "counts": counts}

    @app.post("/sync-tools-from-s3")
    async def legacy_sync_s3(username: str = None, project: str = None):
        from app.core.runtime.local_runner import s3_syncer
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: s3_syncer.sync_all(username, project)
        )

    @app.post("/sync-tool")
    async def legacy_sync_tool(
        username: str, project: str, tool_name: str,
        category: str = None, user_id: str = None,
    ):
        from app.core.runtime.local_runner import s3_syncer
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: s3_syncer.sync_tool(username, project, tool_name, category, user_id)
        )

    return app


app = create_app()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Grafux-mcp Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level=settings.log_level.lower(),
    )
