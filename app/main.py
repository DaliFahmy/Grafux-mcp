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
from fastapi import FastAPI, Request
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

    # 1. Verify Redis connectivity (falls back to in-process cache if unreachable)
    from app.cache.redis_client import init_redis
    logger.info("Redis %s", await init_redis())

    # 2. Load local tool plugins in background thread
    from app.core.runtime.local_runner import start_plugin_loader
    start_plugin_loader()

    # 3. Sync tools from S3 (if configured)
    if settings.aws_access_key_id:
        logger.info("Starting S3 tool sync...")
        from app.core.runtime.local_runner import s3_syncer
        from app.core.runtime.threadpool import run_in_threadpool
        try:
            result = await asyncio.wait_for(
                run_in_threadpool(s3_syncer.sync_all),
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
        except Exception as exc:
            logger.debug("Error disconnecting server %s on shutdown: %s", server_id, exc)

    from app.core.http_client import aclose_http_client
    await aclose_http_client()

    from app.core.runtime.threadpool import shutdown_threadpool
    shutdown_threadpool()

    from app.cache.redis_client import close_redis_pool
    await close_redis_pool()

    logger.info("Grafux-mcp shutdown complete")


async def _periodic_s3_sync() -> None:
    """Re-sync tools from S3 at a fixed interval."""
    await asyncio.sleep(60)  # Brief delay so startup sync doesn't overlap
    from app.core.runtime.local_runner import s3_syncer
    from app.core.runtime.threadpool import run_in_threadpool
    while True:
        try:
            await asyncio.sleep(settings.s3_sync_interval)
            await run_in_threadpool(s3_syncer.sync_all)
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

    # ── CORS-on-500 safety net ────────────────────────────────────────────────
    # Starlette's CORSMiddleware does NOT add headers to responses produced by an
    # unhandled exception, so a 500 reaches the browser as an opaque CORS error
    # ("No 'Access-Control-Allow-Origin' header") that masks the real failure.
    # Re-apply the CORS header on any unhandled error so the actual cause is visible.
    def _cors_headers(request: Request) -> dict:
        origin = request.headers.get("origin")
        allowed = settings.allowed_origins
        if origin and ("*" in allowed or origin in allowed):
            allow_origin = origin
        elif "*" in allowed:
            allow_origin = "*"
        else:
            allow_origin = allowed[0] if allowed else "*"
        return {
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Credentials": "false" if "*" in allowed else "true",
            "Vary": "Origin",
        }

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        logger.error("Unhandled error on %s: %s", request.url.path, exc)
        return JSONResponse(
            status_code=500,
            content={"isError": True, "content": [{"type": "text", "text": f"Server error: {exc}"}]},
            headers=_cors_headers(request),
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

    # Legacy pre-v1 endpoints for the Qt/WASM clients (/health, /api/tools, ...)
    from app.api.legacy import router as legacy_router
    app.include_router(legacy_router)

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
