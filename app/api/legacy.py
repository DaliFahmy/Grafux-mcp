"""app/api/legacy.py — Backward-compatible endpoints for the Qt/WASM clients.

These pre-v1 routes (``/health``, ``/api/tools``, ``/api/tools/{name}``, the
reload/sync endpoints) are still called by shipped desktop/web clients, so their
behaviour is preserved exactly. They were lifted out of ``app/main.py`` to keep the
app factory focused; the only change is that blocking work now runs in the shared
bounded thread pool instead of the default executor.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.runtime.threadpool import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["legacy"])

_TOOL_TIMEOUT = 120.0


@router.get("/health")
async def legacy_health():
    from app.core.runtime import local_runner as _lr
    return {
        "status": "healthy",
        "initialized": _lr._plugins_loaded.is_set(),
        "tools_count": len(_lr.TOOLS),
    }


@router.get("/api/tools")
async def legacy_list_tools():
    from app.core.runtime.local_runner import list_local_tools
    return {"tools": list_local_tools()}


def _normalize_body(body: object) -> dict:
    return body if isinstance(body, dict) else {}


@router.post("/api/tools/{tool_name}")
async def legacy_call_tool(
    tool_name: str,
    request: Request,
    username: str = None,
    project: str = None,
):
    """Invoke a local tool; on an unknown-tool miss, sync it from Supabase and retry."""
    import asyncio

    from app.core.runtime.local_runner import call_tool_direct
    from app.core.runtime.output_files import infer_output_port_paths, read_output_files

    try:
        body = _normalize_body(await request.json())
    except Exception:
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
        result = await asyncio.wait_for(run_in_threadpool(_invoke), timeout=_TOOL_TIMEOUT)
        return _attach_output_files(result)
    except ValueError as exc:
        if "Unknown tool" not in str(exc) or not username or not project:
            # Malformed tool / authorization / namespace error. Return a readable
            # error (CORS-safe via normal middleware) instead of an opaque 500.
            logger.error("Tool %s invalid: %s", tool_name, exc)
            return JSONResponse(
                status_code=200,
                content={"isError": True, "content": [{"type": "text", "text": f"Tool error: {exc}"}]},
            )
        from app.core.runtime.local_runner import reload_plugins, s3_syncer

        logger.info(
            "Unknown tool %s — syncing from Supabase (user=%s project=%s category=%s)",
            tool_name, username, project, category,
        )
        sb_result = await run_in_threadpool(
            s3_syncer.sync_tool_from_supabase, username, project, tool_name, category
        )
        if sb_result.get("files_synced", 0) == 0 and sb_result.get("tools_loaded", 0) == 0:
            await run_in_threadpool(reload_plugins)
        try:
            result = await asyncio.wait_for(run_in_threadpool(_invoke), timeout=_TOOL_TIMEOUT)
            return _attach_output_files(result)
        except ValueError:
            # Still unknown after a sync attempt — surface a clear error, not a 500.
            logger.error("Tool %s still unknown after sync (user=%s project=%s)", tool_name, username, project)
            return JSONResponse(
                status_code=200,
                content={"isError": True, "content": [{"type": "text", "text": f"Tool error: {exc}"}]},
            )
    except asyncio.TimeoutError:
        logger.error("Tool %s timed out (user=%s project=%s)", tool_name, username, project)
        return JSONResponse(status_code=504, content={"isError": True, "content": [{"type": "text", "text": f"Tool '{tool_name}' timed out after 120 s"}]})
    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_name, exc)
        return JSONResponse(
            status_code=200,
            content={"isError": True, "content": [{"type": "text", "text": f"Tool error: {exc}"}]},
        )


@router.post("/reload-tools")
async def legacy_reload_tools():
    from app.core.runtime.local_runner import reload_plugins
    counts = await run_in_threadpool(reload_plugins)
    return {"success": True, "counts": counts}


@router.post("/sync-tools-from-s3")
async def legacy_sync_s3(username: str = None, project: str = None):
    from app.core.runtime.local_runner import s3_syncer
    return await run_in_threadpool(s3_syncer.sync_all, username, project)


@router.post("/sync-tool")
async def legacy_sync_tool(
    username: str, project: str, tool_name: str,
    category: str = None, user_id: str = None,
):
    from app.core.runtime.local_runner import s3_syncer

    # Primary: pull from Supabase (where the Qt client actually uploads files)
    sb_result = await run_in_threadpool(
        s3_syncer.sync_tool_from_supabase, username, project, tool_name, category
    )

    # Secondary: also try S3 (for any files that may live there)
    s3_result = await run_in_threadpool(
        s3_syncer.sync_tool, username, project, tool_name, category, user_id
    )

    total_synced = sb_result.get("files_synced", 0) + s3_result.get("files_synced", 0)
    total_tools = max(sb_result.get("tools_loaded", 0), s3_result.get("tools_loaded", 0))
    success = bool(total_synced) or sb_result.get("success", False) or s3_result.get("success", False)

    return {
        "success": success,
        "files_synced": total_synced,
        "tools_loaded": total_tools,
        "supabase": sb_result,
        "s3": s3_result,
    }
