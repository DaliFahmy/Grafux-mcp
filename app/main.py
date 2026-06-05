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
    async def legacy_call_tool(
        tool_name: str,
        request: Request,
        username: str = None,
        project: str = None,
    ):
        from app.core.runtime.local_runner import call_tool_direct
        from app.core.runtime.output_files import infer_output_port_paths, read_output_files

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        output_port_paths = body.pop("__output_port_paths__", None) or body.pop("output_port_paths", None)
        if isinstance(output_port_paths, list):
            output_port_paths = [p for p in output_port_paths if isinstance(p, str)]
        else:
            output_port_paths = []

        args = body
        # username/project come from query params; also accept from body for backward compat
        if not username:
            username = args.pop("username", None)
        else:
            args.pop("username", None)
        if not project:
            project = args.pop("project", None)
        else:
            args.pop("project", None)
        category = args.pop("category", None)
        arguments = args.pop("arguments", args)
        if not category and isinstance(arguments, dict):
            from app.core.runtime.local_runner import infer_category_from_arguments
            category = infer_category_from_arguments(arguments)

        loop = asyncio.get_event_loop()

        def _invoke() -> dict:
            return call_tool_direct(tool_name, arguments, username, project, category)

        def _attach_output_files(result: dict) -> dict:
            if not isinstance(result, dict):
                return result
            paths_to_read = list(output_port_paths)
            if not paths_to_read and isinstance(arguments, dict):
                paths_to_read = infer_output_port_paths(arguments)
            if not paths_to_read:
                return result
            output_files = read_output_files(paths_to_read)
            if not output_files and isinstance(arguments, dict):
                inferred = infer_output_port_paths(arguments)
                if inferred and inferred != paths_to_read:
                    output_files = read_output_files(inferred)
            if output_files:
                result["output_files"] = output_files
            return result

        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _invoke),
                timeout=120.0,
            )
            return _attach_output_files(result)
        except ValueError as exc:
            if "Unknown tool" not in str(exc) or not username or not project:
                raise
            from app.core.runtime.local_runner import reload_plugins, s3_syncer

            logger.info(
                "Unknown tool %s — syncing from Supabase (user=%s project=%s category=%s)",
                tool_name, username, project, category,
            )
            sb_result = await loop.run_in_executor(
                None,
                lambda: s3_syncer.sync_tool_from_supabase(
                    username, project, tool_name, category
                ),
            )
            if sb_result.get("files_synced", 0) == 0 and sb_result.get("tools_loaded", 0) == 0:
                await loop.run_in_executor(None, reload_plugins)
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, _invoke),
                    timeout=120.0,
                )
                return _attach_output_files(result)
            except ValueError:
                raise exc from None
        except asyncio.TimeoutError:
            logger.error("Tool %s timed out (user=%s project=%s)", tool_name, username, project)
            return JSONResponse(status_code=504, content={"isError": True, "content": [{"type": "text", "text": f"Tool '{tool_name}' timed out after 120 s"}]})
        except Exception as exc:
            logger.error("Tool %s failed: %s", tool_name, exc)
            return JSONResponse(
                status_code=200,
                content={"isError": True, "content": [{"type": "text", "text": f"Tool error: {exc}"}]},
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

        # Primary: pull from Supabase (where the Qt client actually uploads files)
        sb_result = await loop.run_in_executor(
            None, lambda: s3_syncer.sync_tool_from_supabase(username, project, tool_name, category)
        )

        # Secondary: also try S3 (for any files that may live there)
        s3_result = await loop.run_in_executor(
            None, lambda: s3_syncer.sync_tool(username, project, tool_name, category, user_id)
        )

        total_synced = sb_result.get("files_synced", 0) + s3_result.get("files_synced", 0)
        total_tools  = max(sb_result.get("tools_loaded", 0), s3_result.get("tools_loaded", 0))
        success      = bool(total_synced) or sb_result.get("success", False) or s3_result.get("success", False)

        return {
            "success": success,
            "files_synced": total_synced,
            "tools_loaded": total_tools,
            "supabase": sb_result,
            "s3": s3_result,
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
