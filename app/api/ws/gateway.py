"""
app/api/ws/gateway.py — WebSocket MCP gateway (JSON-RPC 2.0).

Fully backward-compatible with the existing Qt / WASM client method surface:
  initialize, ping, tools/list, tools/call, server/reload,
  resources/list, resources/read

New methods added:
  invocation/stream  — subscribe to a running invocation's event stream
  server/status      — get MCP server health
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.runtime.local_runner import (
    TOOLS as LOCAL_TOOLS,
    RESOURCES,
    list_local_tools,
    reload_plugins,
    s3_syncer,
    start_plugin_loader,
    _plugins_loaded,
)
from app.core.streaming.event_bus import event_bus
from app.core.streaming.stream_manager import stream_manager
from app.security.auth import get_ws_auth_context

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ok(request_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result or {}})


def _err(request_id: Any, code: int, message: str, data: Any = None) -> str:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": err})


def _notification(method: str, params: Any = None) -> str:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_gateway(
    websocket: WebSocket,
    token: str | None = Query(default=None),
):
    """
    Main WebSocket endpoint.

    Authentication: pass JWT as query param ?token=<jwt>
    If no token is provided the connection is accepted in anonymous mode
    (local-only tool calls still work for backward compat during migration).
    """
    await websocket.accept()

    auth = None
    if token:
        try:
            auth = await get_ws_auth_context(token)
        except Exception:
            await websocket.send_text(_err(None, -32001, "Invalid token"))
            await websocket.close(code=4001)
            return

    org_id = auth.org_id if auth else "anonymous"
    await stream_manager.connect(websocket, org_id)
    logger.info("WS connected org=%s", org_id)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Keepalive ping — prevents Render's proxy from closing idle connections
                await websocket.send_text(_notification("ping"))
                continue

            try:
                req = json.loads(raw)
            except json.JSONDecodeError as exc:
                await websocket.send_text(_err(None, -32700, f"Parse error: {exc}"))
                continue

            if not isinstance(req, dict):
                await websocket.send_text(_err(None, -32600, "Request must be a JSON object"))
                continue

            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}

            if not isinstance(params, dict):
                await websocket.send_text(_err(req_id, -32602, "params must be an object"))
                continue

            try:
                result = await _dispatch(method, params, req_id, websocket, auth)
                if result is not None:
                    await websocket.send_text(_ok(req_id, result))
            except _JsonRpcError as exc:
                await websocket.send_text(_err(req_id, exc.code, exc.message, exc.data))
            except Exception as exc:
                logger.error("WS dispatch error method=%s: %s", method, exc)
                traceback.print_exc()
                await websocket.send_text(_err(req_id, -32603, str(exc)))

    except WebSocketDisconnect:
        logger.info("WS disconnected org=%s", org_id)
    except Exception as exc:
        logger.error("WS error org=%s: %s", org_id, exc)
    finally:
        await stream_manager.disconnect(websocket, org_id)


# ── Dispatcher ────────────────────────────────────────────────────────────────

class _JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data


async def _dispatch(
    method: str,
    params: dict,
    req_id: Any,
    websocket: WebSocket,
    auth: Any,
) -> Any:
    """Route a JSON-RPC method to the appropriate handler. Returns the result dict."""

    if method == "ping":
        return {}

    if method == "initialize":
        return {
            "status": "healthy",
            "initialized": _plugins_loaded.is_set(),
            "tools_count": len(LOCAL_TOOLS),
            "serverInfo": {"name": "Grafux-mcp", "version": "2.0.0"},
        }

    if method == "tools/list":
        return {"tools": list_local_tools()}

    if method == "tools/call":
        return await _handle_tools_call(params, auth)

    if method == "server/reload":
        return await _handle_reload()

    if method == "resources/list":
        return {
            "resources": [
                {"uri": uri, "name": info["name"],
                 "description": info.get("description", ""),
                 "mimeType": info.get("mimeType", "text/plain")}
                for uri, info in RESOURCES.items()
            ]
        }

    if method == "resources/read":
        uri = params.get("uri")
        if not uri or uri not in RESOURCES:
            raise _JsonRpcError(-32602, f"Unknown resource: {uri}")
        info = RESOURCES[uri]
        return {"contents": [{"uri": uri, "mimeType": info.get("mimeType", "text/plain"),
                               "text": info.get("content", "")}]}

    if method == "invocation/stream":
        await _handle_invocation_stream(params, req_id, websocket)
        return None  # response sent inline

    if method == "server/status":
        return await _handle_server_status(params, auth)

    # S3 sync (preserved from v1)
    if method == "sync/all":
        return await _handle_sync_all(params)

    if method == "sync/tool":
        return await _handle_sync_tool(params)

    raise _JsonRpcError(-32601, f"Unknown method: {method}")


# ── Method handlers ───────────────────────────────────────────────────────────

async def _handle_tools_call(params: dict, auth: Any) -> dict:
    tool_name: str | None = params.get("name")
    if not tool_name:
        raise _JsonRpcError(-32602, "Missing required param: name")

    arguments: dict = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise _JsonRpcError(-32602, "arguments must be an object")

    username: str | None = params.get("username")
    project: str | None = params.get("project")
    category: str | None = params.get("category")

    # Infer username/project from arguments if not provided (v1 compat)
    if not username or not project:
        for v in arguments.values():
            if isinstance(v, str) and v.startswith("data/") and "/tools/" in v:
                parts = v.split("/")
                if len(parts) >= 4:
                    username = username or parts[1]
                    project = project or parts[2]
                if len(parts) >= 6:
                    category = category or parts[4]
                break

    # Use auth context when available
    if auth and not username:
        username = auth.user_id

    from app.core.runtime.local_runner import call_tool_direct
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: call_tool_direct(tool_name, arguments, username, project, category),
            ),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        return {
            "content": [{"type": "text", "text": f"Tool '{tool_name}' timed out after 120s"}],
            "isError": True,
            "error": "tool_timeout",
        }

    # Embed output files (v1 compat)
    output_port_paths = params.get("output_port_paths", [])
    if output_port_paths and isinstance(result, dict):
        output_files = _read_output_files(output_port_paths)
        if output_files:
            result["output_files"] = output_files

    return result


async def _handle_reload() -> dict:
    loop = asyncio.get_event_loop()
    counts = await loop.run_in_executor(None, reload_plugins)
    return {
        "success": True,
        "tools": counts.get("tools", 0),
        "resources": counts.get("resources", 0),
        "prompts": counts.get("prompts", 0),
        "counts": counts,
    }


async def _handle_invocation_stream(
    params: dict, req_id: Any, websocket: WebSocket
) -> None:
    invocation_id: str | None = params.get("invocation_id")
    if not invocation_id:
        await websocket.send_text(_err(req_id, -32602, "Missing invocation_id"))
        return

    await stream_manager.subscribe_invocation(websocket, invocation_id)
    # Send acknowledgement
    await websocket.send_text(_ok(req_id, {"subscribed": True, "invocation_id": invocation_id}))

    # Fan-out events from Redis pub/sub
    try:
        async for event in event_bus.subscribe(invocation_id, timeout=300.0):
            notification = _notification(
                "invocation/event",
                {"invocation_id": invocation_id, **event},
            )
            try:
                await websocket.send_text(notification)
            except Exception:
                break
            if event.get("final"):
                break
    finally:
        await stream_manager.unsubscribe_invocation(websocket, invocation_id)


async def _handle_server_status(params: dict, auth: Any) -> dict:
    from app.cache.redis_client import get_redis_pool
    from app.cache.keys import MCPKeys

    org_id = auth.org_id if auth else "anonymous"
    redis = get_redis_pool()
    pattern = MCPKeys.server_status_pattern(org_id)
    statuses: list[dict] = []
    async for key in redis.scan_iter(pattern):
        val = await redis.get(key)
        if val:
            try:
                statuses.append(json.loads(val))
            except Exception:
                pass
    return {"servers": statuses}


async def _handle_sync_all(params: dict) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: s3_syncer.sync_all(
            username=params.get("username"),
            project=params.get("project"),
            clean_first=params.get("clean_first", False),
        ),
    )


async def _handle_sync_tool(params: dict) -> dict:
    username = params.get("username")
    project = params.get("project")
    tool_name = params.get("tool_name")
    if not all([username, project, tool_name]):
        raise _JsonRpcError(-32602, "Missing username, project, or tool_name")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: s3_syncer.sync_tool(
            username=username,
            project=project,
            tool_name=tool_name,
            category=params.get("category"),
            user_id=params.get("user_id"),
        ),
    )


# ── Output file embedding (v1 backward compat) ────────────────────────────────

import base64
from pathlib import Path

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_PDF_EXTS = {".pdf"}
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _read_output_files(paths: list[str]) -> list[dict]:
    result: list[dict] = []
    for rel in paths:
        local = _PROJECT_ROOT / rel
        if not local.exists():
            continue
        try:
            text = local.read_text(encoding="utf-8")
            result.append({"path": rel, "content": text})
            # Try to attach referenced binary (image/PDF)
            candidate = local.parent / text.strip()
            ext = Path(text.strip()).suffix.lower()
            if (ext in _IMAGE_EXTS or ext in _PDF_EXTS) and candidate.exists():
                data = candidate.read_bytes()
                bin_rel = rel.rsplit("/", 1)[0] + "/" + text.strip()
                result.append({
                    "path": bin_rel,
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                })
        except Exception:
            pass
    return result
