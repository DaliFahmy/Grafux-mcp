"""
routes.py — All HTTP and WebSocket route handlers.

Defines a single FastAPI APIRouter that mcp_server.py includes into the main
app.  Also owns the output-file embedding helpers and the S3 upload helper
that is shared between the REST and WebSocket tool-call paths.
"""

import asyncio
import base64
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from config import S3_AVAILABLE, _get_project_root, _tool_call_logs
from bridge import bridge
from block_downloader import block_downloader

router = APIRouter()


# ── Output file helpers ───────────────────────────────────────────────────────

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_PDF_EXTENSIONS = {".pdf"}


def _build_output_files(
    project_root: Path, output_port_paths: list, log_prefix: str = ""
) -> list:
    """Read output port files and return them so the WASM client can populate
    its virtual filesystem without a separate S3 download round-trip.

    Text files are embedded as-is.  If a text file names a binary asset (image
    or PDF), that binary is also embedded base64-encoded.
    """
    tag = f"[{log_prefix}] " if log_prefix else ""
    output_files = []

    for rel_path in output_port_paths:
        local_file = project_root / rel_path
        if not local_file.exists():
            print(f"[WARN] {tag}Output file not found: {local_file}", file=sys.stderr)
            continue
        try:
            text_content = local_file.read_text(encoding="utf-8")
            output_files.append({"path": rel_path, "content": text_content})
            print(f"[INFO] {tag}Attaching output file: {rel_path}", file=sys.stderr)

            candidate_name = text_content.strip()
            ext = Path(candidate_name).suffix.lower()
            if ext in _IMAGE_EXTENSIONS or ext in _PDF_EXTENSIONS:
                bin_file = local_file.parent / candidate_name
                if bin_file.exists():
                    data = bin_file.read_bytes()
                    bin_rel = rel_path.rsplit("/", 1)[0] + "/" + candidate_name
                    output_files.append({
                        "path": bin_rel,
                        "content": base64.b64encode(data).decode("ascii"),
                        "encoding": "base64",
                    })
                    kind = "image" if ext in _IMAGE_EXTENSIONS else "PDF"
                    print(
                        f"[INFO] {tag}Attaching {kind} ({len(data)} bytes): {candidate_name}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[WARN] {tag}Binary file referenced by port not found: {bin_file}",
                        file=sys.stderr,
                    )
        except Exception as read_err:
            print(
                f"[WARN] {tag}Could not read output file {rel_path}: {read_err}",
                file=sys.stderr,
            )

    return output_files


# ── S3 upload helper ──────────────────────────────────────────────────────────

def _upload_outputs_to_s3_helper(
    username: str,
    project: str,
    tool_name: str,
    data_dir: Path,
    category: str = None,
) -> dict:
    """Upload tool output files from local disk to S3.

    Blocking — callers in async contexts must run this via run_in_executor.
    """
    if not S3_AVAILABLE:
        return {"success": False, "error": "boto3 not available"}

    try:
        aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
        aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        bucket_name = os.environ.get('S3_BUCKET_NAME', 'grafux-user-files')

        if not aws_access_key or not aws_secret_key:
            return {"success": False, "error": "AWS credentials not configured"}

        s3_client = block_downloader.get_client()

        outputs_path = (
            data_dir / username / project / "tools" / category / tool_name / "outputs"
            if category
            else data_dir / username / project / "tools" / tool_name / "outputs"
        )

        if not outputs_path.exists():
            return {"success": True, "files_uploaded": 0, "message": "No output files"}

        user_id = block_downloader.lookup_user_id(username, project)
        files_uploaded = 0
        output_contents: Dict[str, str] = {}

        # Text outputs (.txt port files)
        for file_path in outputs_path.glob("*.txt"):
            rel = file_path.relative_to(data_dir)
            s3_key = f"users/{user_id}/{rel.as_posix()}"
            try:
                content = file_path.read_text(encoding="utf-8")
                output_contents[file_path.stem] = content
                s3_client.upload_file(str(file_path), bucket_name, s3_key)
                files_uploaded += 1
                print(f"[INFO] ✓ Uploaded: {file_path.name} → {s3_key}", file=sys.stderr)
            except Exception as e:
                print(f"[ERROR] Upload failed ({file_path.name}): {e}", file=sys.stderr)

        # Binary outputs — images, videos, PDFs
        _binary_content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".webm": "video/webm",
            ".pdf": "application/pdf",
        }
        for ext, content_type in _binary_content_types.items():
            for file_path in outputs_path.glob(f"*{ext}"):
                rel = file_path.relative_to(data_dir)
                s3_key = f"users/{user_id}/{rel.as_posix()}"
                try:
                    s3_client.upload_file(
                        str(file_path), bucket_name, s3_key,
                        ExtraArgs={"ContentType": content_type},
                    )
                    files_uploaded += 1
                    print(f"[INFO] ✓ Uploaded: {file_path.name} → {s3_key}", file=sys.stderr)
                except Exception as e:
                    print(f"[ERROR] Upload failed ({file_path.name}): {e}", file=sys.stderr)

        # Update the tool's JSON with output port contents and re-upload it
        if output_contents:
            tool_json_path = (
                data_dir / username / project / "tools" / category / tool_name / f"{tool_name}.json"
                if category
                else data_dir / username / project / "tools" / tool_name / f"{tool_name}.json"
            )
            if tool_json_path.exists():
                try:
                    tool_json = json.loads(tool_json_path.read_text(encoding="utf-8"))
                    tool_calls = tool_json.get('tool_calls', [])
                    if tool_calls:
                        for port in tool_calls[0].get('params', {}).get('output_ports', []):
                            pname = port.get('port_name', '')
                            if pname in output_contents:
                                port['port_content'] = output_contents[pname]
                        tool_json_path.write_text(
                            json.dumps(tool_json, indent=4, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        rel = tool_json_path.relative_to(data_dir)
                        s3_key = f"users/{user_id}/{rel.as_posix()}"
                        s3_client.upload_file(str(tool_json_path), bucket_name, s3_key)
                        files_uploaded += 1
                        print(f"[INFO] ✓ Uploaded updated JSON → {s3_key}", file=sys.stderr)
                except Exception as e:
                    print(f"[WARN] Failed to update tool JSON: {e}", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

        return {"success": True, "files_uploaded": files_uploaded}

    except Exception as e:
        print(f"[ERROR] Upload helper error: {e}", file=sys.stderr)
        return {"success": False, "error": str(e)}


# ── Core REST endpoints ───────────────────────────────────────────────────────

@router.get("/")
async def root():
    """API information and available endpoints."""
    return {
        "service": "MCP Web Bridge",
        "version": "1.0.0",
        "status": "running",
        "initialized": bridge.initialized,
        "endpoints": {
            "tools_list": "/api/tools",
            "tool_call": "/api/tools/{tool_name}",
            "resources_list": "/api/resources",
            "resource_read": "/api/resources/{uri}",
            "websocket": "/ws",
        },
    }


@router.get("/api/tools")
async def get_tools():
    """List all available tools."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, bridge.list_tools)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tools/{tool_name}")
async def call_tool(
    tool_name: str,
    username: str = None,
    project: str = None,
    category: str = None,
    arguments: Dict[str, Any] = None,
):
    """Call a specific tool with optional user context for authorization."""
    try:
        loop = asyncio.get_running_loop()
        args = arguments or {}
        output_port_paths = args.pop("__output_port_paths__", [])
        inferred_category = category
        if not inferred_category:
            for _arg_val in args.values():
                if isinstance(_arg_val, str) and _arg_val.startswith('data/') and '/tools/' in _arg_val:
                    _parts = _arg_val.split('/')
                    if len(_parts) >= 6:
                        inferred_category = _parts[4]
                    break

        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: bridge.call_tool(tool_name, args, username, project, inferred_category),
            ),
            timeout=120.0,
        )

        if S3_AVAILABLE and args:
            for _arg_val in args.values():
                if isinstance(_arg_val, str) and _arg_val.startswith('data/') and '/tools/' in _arg_val:
                    _parts = _arg_val.split('/')
                    if len(_parts) >= 5:
                        _upload_username = _parts[1]
                        _upload_project = _parts[2]
                        if len(_parts) >= 6:
                            _upload_category = _parts[4]
                            _upload_tool = _parts[5]
                        else:
                            _upload_category = None
                            _upload_tool = _parts[4]
                        _data_dir = _get_project_root() / "data"
                        await loop.run_in_executor(
                            None,
                            lambda u=_upload_username, p=_upload_project,
                                   t=_upload_tool, c=_upload_category, d=_data_dir:
                                _upload_outputs_to_s3_helper(u, p, t, d, c),
                        )
                    break

        if output_port_paths and isinstance(result, dict):
            output_files = _build_output_files(
                _get_project_root(), output_port_paths, log_prefix="HTTP"
            )
            if output_files:
                result["output_files"] = output_files

        return result
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Tool '{tool_name}' timed out after 120 seconds. "
                        "Check API rate limits, network calls, retries, and large inputs."
                    ),
                }
            ],
            "isError": True,
            "error": "tool_timeout",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/resources")
async def get_resources():
    """List all available resources."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, bridge.list_resources)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/resources/{uri:path}")
async def read_resource(uri: str):
    """Read a specific resource by URI."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: bridge.read_resource(uri))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def health_check():
    """Health check endpoint (also used by Render)."""
    return {
        "status": "healthy",
        "initialized": bridge.initialized,
        "tools_count": len(bridge.tools_cache),
    }


@router.post("/reload-tools")
async def reload_tools_endpoint():
    """Reload MCP server tools (re-scans plugins in-process)."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, bridge.reload_tools)
        if result.get("success"):
            return JSONResponse(content={
                "success": True,
                "message": "Tools reloaded successfully",
                "counts": result.get("counts", {}),
            })
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "Failed to reload tools",
                "error": result.get("error"),
            },
        )
    except Exception as e:
        print(f"[ERROR] Reload endpoint error: {e}")
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Exception during reload", "error": str(e)},
        )


# ── S3 REST endpoints ─────────────────────────────────────────────────────────

@router.post("/upload-outputs-to-s3")
async def upload_outputs_to_s3(
    username: str = None, project: str = None, tool_name: str = None
):
    """Upload tool output files from Render disk to S3."""
    if not S3_AVAILABLE:
        return JSONResponse(
            status_code=501,
            content={"success": False, "error": "boto3 not installed - S3 upload not available"},
        )

    aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
    aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
    bucket_name = os.environ.get('S3_BUCKET_NAME', 'grafux-user-files')

    if not aws_access_key or not aws_secret_key:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "AWS credentials not configured"},
        )

    data_dir = _get_project_root() / "data"
    if not data_dir.exists():
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": f"Data directory not found: {data_dir}"},
        )

    if username and project and tool_name:
        flat_path = data_dir / username / project / "tools" / tool_name / "outputs"
        if flat_path.exists():
            local_path = flat_path
        else:
            tools_dir = data_dir / username / project / "tools"
            local_path = flat_path
            if tools_dir.exists():
                for cat_dir in tools_dir.iterdir():
                    if cat_dir.is_dir():
                        candidate = cat_dir / tool_name / "outputs"
                        if candidate.exists():
                            local_path = candidate
                            break
    elif username and project:
        local_path = data_dir / username / project / "tools"
    elif username:
        local_path = data_dir / username
    else:
        local_path = data_dir

    if not local_path.exists():
        return JSONResponse(content={
            "success": True, "message": "No output files found", "files_uploaded": 0
        })

    s3_client = block_downloader.get_client()
    loop = asyncio.get_running_loop()

    def _do_upload():
        files_uploaded = 0
        uid = block_downloader.lookup_user_id(username, project)
        for file_path in local_path.rglob("*"):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(data_dir)
            s3_key = f"users/{uid}/{rel.as_posix()}"
            try:
                s3_client.upload_file(str(file_path), bucket_name, s3_key)
                files_uploaded += 1
                print(f"[INFO] ✓ Uploaded: {file_path.name} → {s3_key}")
            except Exception as e:
                print(f"[ERROR] Failed to upload {file_path.name}: {e}")
        return files_uploaded

    try:
        files_uploaded = await loop.run_in_executor(None, _do_upload)
        return JSONResponse(content={
            "success": True,
            "message": f"Uploaded {files_uploaded} files to S3",
            "files_uploaded": files_uploaded,
        })
    except Exception as e:
        print(f"[ERROR] S3 upload error: {e}")
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.post("/sync-tools-from-s3")
async def sync_tools_from_s3(
    username: str = None, project: str = None, clean_first: bool = False
):
    """Download tools from S3 to local disk and reload the tool registry."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: block_downloader.sync_all(username, project, clean_first),
    )
    if result.get("success"):
        return JSONResponse(content=result)
    error_msg = result.get("error", "")
    return JSONResponse(
        status_code=501 if "boto3 not installed" in error_msg else 500,
        content=result,
    )


@router.post("/sync-tool")
async def sync_tool_endpoint(
    username: str,
    project: str,
    tool_name: str,
    category: str = None,
    user_id: str = None,
):
    """Fast targeted sync for a single tool.

    Downloads only the files under the tool's specific S3 prefix and reloads
    the tool registry.  Called by both the client (after tool creation) and by
    the backend (push notification after file upload).
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: block_downloader.sync_tool(username, project, tool_name, category, user_id),
    )
    if result.get("success"):
        return JSONResponse(content=result)
    error_msg = result.get("error", "")
    return JSONResponse(
        status_code=501 if "boto3 not installed" in error_msg else 500,
        content=result,
    )


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time MCP communication.

    Uses a 30 s receive timeout to send application-level ping frames that
    prevent Render's proxy from closing idle connections during long tool calls.
    All blocking bridge and S3 operations are offloaded to a thread executor so
    the event loop stays responsive.
    """
    await websocket.accept()
    print("[INFO] WebSocket client connected")
    loop = asyncio.get_running_loop()

    async def send_result(request_id: Any, result: Dict[str, Any]) -> None:
        await websocket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
        )

    async def send_error(request_id: Any, code: int, message: str,
                         data: Optional[Dict[str, Any]] = None) -> None:
        err_obj: Dict[str, Any] = {"code": code, "message": message}
        if data:
            err_obj["data"] = data
        await websocket.send_text(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "error": err_obj})
        )

    try:
        while True:
            # Short timeout lets us send periodic keepalive pings without a
            # separate concurrent task that would race on websocket.send_text.
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
                continue

            try:
                request = json.loads(data)
            except json.JSONDecodeError as exc:
                await send_error(None, -32700, f"Invalid JSON: {exc.msg}")
                continue

            if not isinstance(request, dict):
                await send_error(None, -32600, "Invalid request: JSON object expected")
                continue

            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                await send_error(request_id, -32602, "Invalid params: object expected")
                continue

            if method == "ping":
                await send_result(request_id, {})
                continue

            _captured_logs: Dict[str, List[str]] = {"warnings": [], "errors": []}

            try:
                if method == "initialize":
                    result = {
                        "status": "healthy",
                        "initialized": bridge.initialized,
                        "tools_count": len(bridge.tools_cache),
                        "serverInfo": {
                            "name": "MCP Web Bridge",
                            "version": "1.0.0",
                        },
                    }

                elif method == "server/reload":
                    reload_result = await loop.run_in_executor(None, bridge.reload_tools)
                    counts = (
                        reload_result.get("counts", {})
                        if isinstance(reload_result, dict) else {}
                    )
                    result = {
                        "success": (
                            bool(reload_result.get("success"))
                            if isinstance(reload_result, dict) else False
                        ),
                        "tools": int(counts.get("tools", 0)),
                        "resources": int(counts.get("resources", 0)),
                        "prompts": int(counts.get("prompts", 0)),
                        "counts": counts,
                    }
                    if isinstance(reload_result, dict) and reload_result.get("error"):
                        result["error"] = reload_result["error"]

                elif method == "tools/list":
                    result = await loop.run_in_executor(None, bridge.list_tools)

                elif method == "tools/call":
                    tool_name = params.get("name")
                    arguments = params.get("arguments", {})
                    output_port_paths = params.get("output_port_paths", [])
                    if not tool_name:
                        await send_error(request_id, -32602, "Missing required param: name")
                        continue
                    if not isinstance(arguments, dict):
                        await send_error(request_id, -32602,
                                         "Invalid arguments: object expected")
                        continue

                    _ws_username: Optional[str] = params.get("username")
                    _ws_project: Optional[str] = params.get("project")
                    _ws_category: Optional[str] = params.get("category")
                    if not _ws_username or not _ws_project or not _ws_category:
                        for _v in arguments.values():
                            if isinstance(_v, str) and _v.startswith('data/') and '/tools/' in _v:
                                _p = _v.split('/')
                                if len(_p) >= 4:
                                    _ws_username = _p[1]
                                    _ws_project = _p[2]
                                if len(_p) >= 6:
                                    _ws_category = _p[4]
                                break

                    def _call_and_capture_logs(
                        tn=tool_name, a=arguments,
                        u=_ws_username, p=_ws_project, c=_ws_category,
                    ):
                        _tool_call_logs.warnings = []
                        r = bridge.call_tool(tn, a, u, p, c)
                        _captured_logs["warnings"] = list(
                            getattr(_tool_call_logs, 'warnings', [])
                        )
                        return r

                    result = await loop.run_in_executor(None, _call_and_capture_logs)

                    if output_port_paths and isinstance(result, dict):
                        output_files = _build_output_files(
                            _get_project_root(), output_port_paths, log_prefix="WS"
                        )
                        if output_files:
                            result["output_files"] = output_files

                    if S3_AVAILABLE and arguments:
                        for _v in arguments.values():
                            if isinstance(_v, str) and _v.startswith('data/') and '/tools/' in _v:
                                _parts = _v.split('/')
                                if len(_parts) >= 6:
                                    _u2, _pr2, _t2, _c2 = (
                                        _parts[1], _parts[2], _parts[5], _parts[4]
                                    )
                                elif len(_parts) >= 5:
                                    _u2, _pr2, _t2, _c2 = (
                                        _parts[1], _parts[2], _parts[4], None
                                    )
                                else:
                                    break
                                _dd = _get_project_root() / "data"
                                loop.run_in_executor(
                                    None,
                                    lambda u=_u2, p=_pr2, t=_t2, c=_c2, d=_dd:
                                        _upload_outputs_to_s3_helper(u, p, t, d, c),
                                )
                                break

                elif method == "resources/list":
                    result = await loop.run_in_executor(None, bridge.list_resources)

                elif method == "resources/read":
                    _uri = params.get("uri")
                    result = await loop.run_in_executor(
                        None, lambda u=_uri: bridge.read_resource(u)
                    )

                else:
                    await send_error(request_id, -32601, f"Unknown method: {method}")
                    continue

                await send_result(request_id, result or {})

            except Exception as exc:
                print(f"[ERROR] WebSocket request failed: {exc}")
                traceback.print_exc()
                _exc_warnings = _captured_logs.get("warnings", [])
                _err_data: Optional[Dict[str, Any]] = (
                    {"warnings": _exc_warnings,
                     "errors": [f"WebSocket request failed: {exc}"]}
                    if _exc_warnings else None
                )
                await send_error(request_id, -32603, str(exc), data=_err_data)

    except WebSocketDisconnect:
        print("[INFO] WebSocket client disconnected")
    except Exception as e:
        print(f"[ERROR] WebSocket error: {e}")
        traceback.print_exc()
