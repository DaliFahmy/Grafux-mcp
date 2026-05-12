#!/usr/bin/env python3
"""
MCP Server — HTTP/WebSocket Entry Point

Wraps the stdio-based MCP core and exposes tools via REST API and WebSocket
for browser-based clients (WebAssembly Qt application).

Module layout
─────────────
  config.py            env setup, shared utilities (_get_project_root, etc.)
  registry.py          plugin loader, tool/resource/prompt registries
  executor.py          tool dispatch engine (_call_tool_direct)
  bridge.py            MCPBridge class + singleton
  block_downloader.py  BlockDownloader class + singleton
  routes.py            all FastAPI route handlers (REST + WebSocket)
  mcp_server.py        ← you are here: app, lifecycle, main()
"""

import asyncio
import sys
import traceback

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Install with: pip install fastapi uvicorn websockets")
    sys.exit(1)

from config import SYNC_INTERVAL_SECONDS
from bridge import bridge
from block_downloader import block_downloader
from routes import router

# ── FastAPI application ───────────────────────────────────────────────────────

app = FastAPI(title="MCP Web Bridge", version="1.0.0")

# CORS: allow_origins=["*"] and allow_credentials=True cannot be combined —
# browsers (including WASM clients) reject credentialed requests when the
# Access-Control-Allow-Origin header is a wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


# ── Background tasks ──────────────────────────────────────────────────────────

async def periodic_sync_task():
    """Re-download tools from S3 at a fixed interval (skipped when interval=0)."""
    await asyncio.sleep(60)  # brief delay to avoid overlapping with startup sync
    while True:
        try:
            if SYNC_INTERVAL_SECONDS > 0:
                await asyncio.sleep(SYNC_INTERVAL_SECONDS)
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, block_downloader.sync_all)
                if result.get("success"):
                    downloaded = result.get('files_synced', 0)
                    if downloaded or result.get('files_deleted', 0):
                        print(
                            f"[INFO] Periodic sync: {downloaded} downloaded, "
                            f"{result.get('files_skipped', 0)} unchanged, "
                            f"{result.get('tools_loaded', 0)} tools loaded",
                            file=sys.stderr,
                        )
                else:
                    print(f"[ERROR] Periodic sync failed: {result.get('error')}",
                          file=sys.stderr)
            else:
                await asyncio.sleep(86400)
        except Exception as e:
            print(f"[ERROR] Periodic sync task error: {e}")
            traceback.print_exc()
            await asyncio.sleep(300)


# ── Lifecycle hooks ───────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    """Sync tools from S3 at startup.

    Runs in a thread executor so the event loop stays alive and Render's health
    check can succeed even while the sync is in progress.  Capped at 120 s to
    avoid blocking the health check indefinitely.
    """
    print("[INFO] Server startup — syncing tools from S3...")
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, block_downloader.sync_all),
            timeout=120.0,
        )
        if result.get("success"):
            print(
                f"[INFO] Startup sync complete: "
                f"{result.get('files_synced', 0)} files synced, "
                f"{result.get('tools_loaded', 0)} tools loaded"
            )
        else:
            print(f"[WARN] Startup sync failed: {result.get('error', 'unknown error')}")
    except asyncio.TimeoutError:
        print("[WARN] Startup S3 sync timed out after 120 s — continuing with local tools")
    except Exception as e:
        print(f"[ERROR] Startup sync error: {e}")
        traceback.print_exc()

    if SYNC_INTERVAL_SECONDS > 0:
        print(f"[INFO] Starting periodic sync task (interval: {SYNC_INTERVAL_SECONDS}s)")
        asyncio.create_task(periodic_sync_task())
    else:
        print("[INFO] Periodic sync disabled (S3_SYNC_INTERVAL=0)")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on server shutdown."""
    bridge.shutdown()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="MCP Web Bridge Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    args = parser.parse_args()

    print(f"[INFO] Starting MCP web bridge on http://{args.host}:{args.port}")
    print(f"[INFO] WebSocket endpoint: ws://{args.host}:{args.port}/ws")
    print(f"[INFO] API docs: http://{args.host}:{args.port}/docs")

    uvicorn.run("mcp_server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
