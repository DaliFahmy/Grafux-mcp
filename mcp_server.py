#!/usr/bin/env python3
"""
MCP Server — HTTP/WebSocket Entry Point
Wraps the stdio-based MCP core and exposes tools via REST API and WebSocket
for browser-based clients (WebAssembly Qt application).
"""

import asyncio
import base64
import importlib.util
import io
import json
import os
import sys
import threading
import traceback
import time
import warnings as _warnings_module
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    import uvicorn
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Install with: pip install fastapi uvicorn websockets")
    sys.exit(1)

try:
    import boto3
    S3_AVAILABLE = True
except ImportError:
    print("[WARN] boto3 not installed - S3 sync will not be available")
    print("       Install with: pip install boto3")
    S3_AVAILABLE = False

# Set to 0 to disable periodic sync (startup sync still runs once)
SYNC_INTERVAL_SECONDS = int(os.environ.get('S3_SYNC_INTERVAL', '300'))

# ── Google Cloud credentials setup ───────────────────────────────────────────
# On Render, set GCP_SERVICE_ACCOUNT_JSON to the full JSON content of your
# service account key file so Vertex AI picks up credentials automatically.
_gcp_sa_json = os.environ.get('GCP_SERVICE_ACCOUNT_JSON', '')
if _gcp_sa_json:
    import tempfile
    _creds_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='.json', delete=False, prefix='gcp_sa_'
    )
    _creds_file.write(_gcp_sa_json)
    _creds_file.flush()
    _creds_file.close()
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = _creds_file.name
    print(f"[INFO] GCP credentials written to {_creds_file.name}")
else:
    print("[WARN] GCP_SERVICE_ACCOUNT_JSON not set - Vertex AI tools will fail unless credentials are configured another way")
# ─────────────────────────────────────────────────────────────────────────────

# ── Per-thread log accumulator for tool calls ─────────────────────────────────
# Used by the WebSocket handler to collect [WARN]/[ERROR] messages emitted
# during a tool call and include them in the JSON-RPC error response so the
# Qt client can display them in the warnings / errors output ports.
_tool_call_logs = threading.local()


def _tl_log_warn(msg: str) -> None:
    """Print [WARN] to stderr and accumulate in the current thread's buffer."""
    print(f"[WARN] {msg}", file=sys.stderr)
    if not hasattr(_tool_call_logs, 'warnings'):
        _tool_call_logs.warnings = []
    _tool_call_logs.warnings.append(msg)
# ─────────────────────────────────────────────────────────────────────────────


def _get_project_root() -> Path:
    """Return the repository/project root directory.

    In the standalone repo mcp_server.py lives at the repo root, so both
    locally and on Render (/opt/render/project/src/mcp_server.py) the parent
    directory of this file is always the correct project root.
    """
    result = Path(__file__).resolve().parent
    # #region agent log
    import json as _json, time as _time
    try:
        with open("debug-c5eccb.log", "a") as _lf:
            _lf.write(_json.dumps({"sessionId": "c5eccb", "hypothesisId": "A-B-D", "location": "mcp_server.py:_get_project_root", "message": "project root resolved", "data": {"__file__": __file__, "resolved": str(Path(__file__).resolve()), "result": str(result), "cwd": str(Path.cwd())}, "timestamp": int(_time.time() * 1000)}) + "\n")
    except Exception:
        pass
    # #endregion
    return result


# ── S3 client cache ───────────────────────────────────────────────────────────
# Re-creating boto3.client on every call is expensive. Cache it and only rebuild
# when the credentials environment variables change.
_s3_client_instance: Optional[Any] = None
_s3_client_creds: Tuple[str, str] = ("", "")


def _get_s3_client() -> Any:
    """Return a cached boto3 S3 client, rebuilding if credentials have changed."""
    global _s3_client_instance, _s3_client_creds
    key: Tuple[str, str] = (
        os.environ.get('AWS_ACCESS_KEY_ID', ''),
        os.environ.get('AWS_SECRET_ACCESS_KEY', ''),
    )
    if _s3_client_instance is None or key != _s3_client_creds:
        _s3_client_instance = boto3.client(
            's3',
            aws_access_key_id=key[0],
            aws_secret_access_key=key[1],
        )
        _s3_client_creds = key
    return _s3_client_instance


# ── user_id lookup cache ──────────────────────────────────────────────────────
# Avoids a full S3 paginator scan on every tool-call upload.
_user_id_cache: Dict[Tuple[str, str], str] = {}


# ── Tool / Resource / Prompt registry ────────────────────────────────────────

_plugins_loaded = threading.Event()
TOOLS:         Dict[str, Dict[str, Any]] = {}
RESOURCES:     Dict[str, Dict[str, Any]] = {}
PROMPTS:       Dict[str, Dict[str, Any]] = {}
PLUGIN_ERRORS: List[Dict[str, Any]]      = []


def register_tool(
    name: str,
    description: str,
    input_schema: dict,
    username: str = None,
    project: str  = None,
    category: str = None,
):
    """Decorator — register a tool function with JSON Schema + owner metadata."""
    def wrapper(func: Callable):
        if username and project and category:
            key = f"{username}/{project}/{category}/{name}"
        elif username and project:
            key = f"{username}/{project}/{name}"
        else:
            key = name
        if key in TOOLS:
            print(f"[WARN] Duplicate tool key '{key}' — replacing", file=sys.stderr)
        TOOLS[key] = {
            "func": func, "name": name, "description": description,
            "inputSchema": input_schema,
            "username": username, "project": project, "category": category,
        }
        print(f"[DEBUG] ✓ Registered tool: {name} (key: {key})", file=sys.stderr)
        return func
    return wrapper


def register_resource(uri: str, name: str, description: str = "",
                      mime_type: str = "text/plain", content: str = ""):
    RESOURCES[uri] = {"name": name, "description": description,
                      "mimeType": mime_type, "uri": uri, "content": content}


def register_prompt(name: str, description: str, arguments: list = None):
    def wrapper(func: Callable):
        PROMPTS[name] = {"func": func, "description": description,
                         "arguments": arguments or []}
        return func
    return wrapper


def _resolve_tool_code_path(arguments: Dict[str, Any]) -> Optional[Path]:
    """Return the editable-code input file when a tool call provides one."""
    raw_path = arguments.get("code") if isinstance(arguments, dict) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    normalized = raw_path.replace("\\", "/")
    if normalized.startswith("/"):
        candidate = Path(raw_path)
    elif normalized.startswith("data/"):
        candidate = _get_project_root() / normalized
    else:
        candidate = Path(raw_path)
    try:
        candidate = candidate.resolve()
    except Exception:
        pass
    return candidate if candidate.exists() and candidate.is_file() else None


def _load_editable_code_func(
    tool_metadata: Dict[str, Any], arguments: Dict[str, Any]
) -> Optional[Callable]:
    """Compile the editable code port and return the registered tool function."""
    code_path = _resolve_tool_code_path(arguments)
    if not code_path:
        return None
    source = code_path.read_text(encoding="utf-8")
    compiled = compile(source, str(code_path), "exec")
    captured: Dict[str, Any] = {}

    def _register_tool_capture(name: str, description: str, input_schema: dict):
        def wrapper(func: Callable):
            captured.update({"func": func, "name": name,
                             "description": description, "inputSchema": input_schema})
            return func
        return wrapper

    module_globals: Dict[str, Any] = {
        "__file__": str(code_path),
        "__name__": f"_editable_tool_{tool_metadata.get('name', 'tool')}",
        "register_tool": _register_tool_capture,
        "register_resource": register_resource,
        "register_prompt": register_prompt,
    }
    exec(compiled, module_globals)
    func = captured.get("func")
    if not callable(func):
        raise ValueError("Editable code did not register a tool function with @register_tool")
    return func


def load_plugins():
    """Scan data/{user}/{project}/tools/{category}/{tool}/references/*.py and register tools."""
    data_path = _get_project_root() / "data"
    if not data_path.exists():
        data_path.mkdir(parents=True, exist_ok=True)
    print(f"[INFO] Scanning for tools in: {data_path}", file=sys.stderr)
    tools_found = 0
    try:
        user_dirs = [d for d in data_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except Exception as e:
        print(f"[ERROR] Cannot list data dir: {e}", file=sys.stderr)
        return
    for user_dir in user_dirs:
        for project_dir in user_dir.iterdir():
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue
            tools_dir = project_dir / "tools"
            if not tools_dir.exists():
                continue
            for category_dir in tools_dir.iterdir():
                if not category_dir.is_dir() or category_dir.name.startswith("."):
                    continue
                try:
                    tool_dirs = [d for d in category_dir.iterdir()
                                 if d.is_dir() and not d.name.startswith(".")]
                except Exception:
                    continue
                for tool_dir in tool_dirs:
                    refs_dir = tool_dir / "references"
                    if not refs_dir.exists():
                        continue
                    py_files = [f for f in refs_dir.glob("*.py") if not f.name.startswith("_")]
                    if not py_files:
                        continue
                    uname, pname, cname = user_dir.name, project_dir.name, category_dir.name

                    def _make_rt(u, p, c):
                        def _rt(name: str, description: str, input_schema: dict):
                            return register_tool(name, description, input_schema, u, p, c)
                        return _rt

                    for py_file in py_files:
                        try:
                            source = py_file.read_text(encoding="utf-8")
                            compile(source, str(py_file), "exec")
                        except SyntaxError as se:
                            PLUGIN_ERRORS.append({"file": str(py_file), "error": str(se)})
                            print(f"[ERROR] Syntax error in {py_file.name}: {se}", file=sys.stderr)
                            continue
                        try:
                            mod_name = f"{uname}.{pname}.{cname}.{tool_dir.name}.{py_file.stem}"
                            spec = importlib.util.spec_from_file_location(mod_name, py_file)
                            if not spec or not spec.loader:
                                continue
                            mod = importlib.util.module_from_spec(spec)
                            mod.register_tool     = _make_rt(uname, pname, cname)
                            mod.register_resource = register_resource
                            mod.register_prompt   = register_prompt
                            spec.loader.exec_module(mod)
                            tools_found += 1
                            print(f"[INFO] ✓ Loaded {py_file.stem} ({uname}/{pname}/{cname})", file=sys.stderr)
                        except Exception as e:
                            PLUGIN_ERRORS.append({"file": str(py_file), "error": str(e)})
                            print(f"[ERROR] Failed to load {py_file.name}: {e}", file=sys.stderr)
                            traceback.print_exc(file=sys.stderr)
    print(f"[INFO] Total tools loaded: {tools_found}", file=sys.stderr)


def reload_plugins() -> Dict[str, Any]:
    """Clear TOOLS/RESOURCES/PROMPTS, purge cached modules, reload from disk."""
    global TOOLS, RESOURCES, PROMPTS, PLUGIN_ERRORS
    _plugins_loaded.clear()
    print("[INFO] Reloading plugins...", file=sys.stderr)
    TOOLS.clear(); RESOURCES.clear(); PROMPTS.clear(); PLUGIN_ERRORS.clear()
    # Remove cached tool modules (avoid purging third-party C-extension namespaces)
    _SKIP = ("google.", "vertexai.", "grpc.", "proto.", "boto", "botocore.",
             "flask.", "werkzeug.", "jinja2.", "requests.", "urllib3.",
             "pydantic.", "fastapi.", "uvicorn.", "starlette.", "anyio.", "sniffio.",
             "jwt.", "cryptography.", "stripe.", "httpx.", "httpcore.")
    to_del = [m for m in sys.modules
              if "." in m and not m.startswith("_")
              and not any(m.startswith(p) for p in _SKIP)
              and len(m.split(".")) >= 3]
    for m in to_del:
        del sys.modules[m]
    import shutil
    data_path = _get_project_root() / "data"
    if data_path.exists():
        for pc in data_path.rglob("__pycache__"):
            try:
                shutil.rmtree(pc)
            except Exception:
                pass
    load_plugins()
    _plugins_loaded.set()
    return {"tools": len(TOOLS), "resources": len(RESOURCES),
            "prompts": len(PROMPTS), "errors": len(PLUGIN_ERRORS)}


def _suggest_improvements(exc: Exception, tb_text: str, tool_name: str) -> str:
    """Return a short, actionable suggestion based on the exception type."""
    exc_type = type(exc).__name__
    msg = str(exc)
    msg_lower = msg.lower()

    if exc_type in ("ImportError", "ModuleNotFoundError"):
        mod = msg.split("'")[1] if "'" in msg else msg.split()[-1]
        return (
            f"A required module is missing.\n"
            f"  pip install {mod}\n"
            "Check the import name for typos and ensure the package is installed in the server's environment."
        )
    if exc_type == "NameError":
        return (
            "A name is used before it is defined.\n"
            "Check for typos in variable/function names and ensure everything is defined before use."
        )
    if exc_type == "AttributeError":
        return (
            "An attribute or method does not exist on the object.\n"
            "Verify the object type and check the attribute name for typos."
        )
    if exc_type == "TypeError":
        return (
            "A function received an argument of the wrong type or the wrong number of arguments.\n"
            "Check function signatures, argument types, and call sites."
        )
    if exc_type == "KeyError":
        key = msg.strip("'\"")
        return (
            f"Key {msg} was not found in a dictionary.\n"
            "Use .get() for safe access or verify the key exists before using it."
        )
    if exc_type == "ValueError":
        return (
            "A function received a valid type but an invalid value.\n"
            "Check input data, format strings, and expected value ranges."
        )
    if exc_type in ("FileNotFoundError", "IsADirectoryError"):
        return (
            "A required file or directory was not found.\n"
            "Check file paths, working directory, and ensure required data files exist."
        )
    if exc_type == "PermissionError":
        return "Access was denied to a file or resource. Check file permissions."
    if "timeout" in msg_lower or exc_type in ("TimeoutError", "asyncio.TimeoutError"):
        return "The operation timed out. Consider increasing the timeout or optimising the slow operation."
    if any(w in msg_lower for w in ("connection", "refused", "unreachable", "network")):
        return "A network connection failed. Check connectivity and that the remote service is available."
    if exc_type == "RecursionError":
        return "Maximum recursion depth exceeded. Check for infinite loops or missing base cases in recursive functions."
    if exc_type == "MemoryError":
        return "Out of memory. Process smaller batches or reduce data size."
    if exc_type == "ZeroDivisionError":
        return "Division by zero. Add a guard: `if denominator != 0:` before dividing."
    if exc_type == "IndexError":
        return "List index out of range. Check list length before indexing or use `for item in list:` instead."
    if exc_type == "StopIteration":
        return "An iterator was exhausted unexpectedly. Check loop logic and iterator usage."

    # Generic fallback
    return (
        f"Tool '{tool_name}' raised {exc_type}.\n"
        "Review the traceback to identify the failing line and fix the root cause."
    )


def _call_tool_direct(
    tool_name: str,
    arguments: Dict[str, Any],
    caller_username: str = None,
    caller_project:  str = None,
    caller_category: str = None,
) -> Dict[str, Any]:
    """Look up and execute a tool, with editable-code fallback for live-chat tools."""
    if not _plugins_loaded.is_set():
        print(f"[INFO] tools/call '{tool_name}': waiting for plugin loader...", file=sys.stderr)
        _plugins_loaded.wait(timeout=120)

    # Key lookup (namespaced, then direct, then display-name scan)
    tool_key = tool_metadata = None
    if caller_username and caller_project:
        if caller_category:
            k = f"{caller_username}/{caller_project}/{caller_category}/{tool_name}"
            if k in TOOLS:
                tool_key, tool_metadata = k, TOOLS[k]
        if not tool_key:
            k = f"{caller_username}/{caller_project}/{tool_name}"
            if k in TOOLS:
                tool_key, tool_metadata = k, TOOLS[k]
    if not tool_key and tool_name in TOOLS:
        tool_key, tool_metadata = tool_name, TOOLS[tool_name]
    if not tool_key:
        for k, info in TOOLS.items():
            if info.get("name") == tool_name:
                if caller_username and caller_project:
                    if (info.get("username") == caller_username
                            and info.get("project") == caller_project):
                        if caller_category and info.get("category") != caller_category:
                            continue
                        tool_key, tool_metadata = k, info
                        break
                else:
                    tool_key, tool_metadata = k, info
                    break

    if not tool_key:
        # Fallback: live-chat tools have no references/*.py but do have inputs/code.txt
        try:
            _fb_func = _load_editable_code_func({"name": tool_name}, arguments)
        except Exception as _fe:
            _fb_func = None
            _tl_log_warn(f"Editable-code fallback for '{tool_name}' failed: {_fe}")
        if _fb_func:
            print(f"[INFO] Tool '{tool_name}' running via editable-code fallback", file=sys.stderr)
            try:
                result = _fb_func(arguments)
                if not isinstance(result, dict) or "content" not in result:
                    result = {"content": [{"type": "text", "text": str(result)}]}
                return result
            except Exception as _re:
                _tb_io = io.StringIO()
                traceback.print_exc(file=_tb_io)
                _tb_text = _tb_io.getvalue().strip()
                _imps = _suggest_improvements(_re, _tb_text, tool_name)
                print(f"[ERROR] Fallback execution for '{tool_name}' failed: {_re}", file=sys.stderr)
                return {
                    "content": [{"type": "text", "text": _tb_text}],
                    "isError": True,
                    "diagnostics": {"warnings": "", "improvements": _imps},
                }
        raise ValueError(f"Unknown tool: {tool_name}")

    # Authorization
    tu, tp = tool_metadata.get("username"), tool_metadata.get("project")
    if tu and tp:
        if not caller_username or not caller_project:
            raise ValueError("Missing required fields: username and project")
        if tu != caller_username or tp != caller_project:
            raise ValueError(f"Unauthorized: Tool '{tool_name}' belongs to {tu}/{tp}")

    try:
        caught_pywarns: List[str] = []
        with _warnings_module.catch_warnings(record=True) as _cw:
            _warnings_module.simplefilter("always")
            editable_func = _load_editable_code_func(tool_metadata, arguments)
            if editable_func:
                print(f"[INFO] Tool '{tool_name}' using editable source", file=sys.stderr)
            result = (editable_func or tool_metadata["func"])(arguments)
            if not isinstance(result, dict) or "content" not in result:
                result = {"content": [{"type": "text", "text": str(result)}]}
            caught_pywarns = [
                f"{Path(w.filename).name if w.filename else 'code'}:{w.lineno}: "
                f"{w.category.__name__}: {w.message}"
                for w in _cw
                if not issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning))
            ]
        if caught_pywarns:
            result.setdefault("diagnostics", {})["warnings"] = "\n".join(caught_pywarns)
        print(f"[INFO] Tool '{tool_name}' completed successfully", file=sys.stderr)
        return result

    except SyntaxError as se:
        tb_io = io.StringIO()
        traceback.print_exc(file=tb_io)
        tb_text = tb_io.getvalue().strip()
        filename = Path(se.filename).name if se.filename else "code.txt"
        pointer = " " * max((se.offset or 1) - 1, 0) + "^"
        lines = [f"{filename}:{se.lineno}: SyntaxError: {se.msg}"]
        if se.text:
            lines.append(f"    {se.text.rstrip()}")
            lines.append(f"    {pointer}")
        lines += ["", tb_text]
        full_error = "\n".join(lines)
        improvements = (
            "Fix the syntax error at the indicated line.\n"
            "Common causes:\n"
            "  • Missing ':' after if / for / while / def / class / with\n"
            "  • Unmatched parentheses, brackets, or quotes\n"
            "  • Invalid indentation or mixed tabs and spaces\n"
            "  • Incomplete statement or expression"
        )
        print(f"[ERROR] Tool '{tool_name}' SyntaxError: {se}", file=sys.stderr)
        return {
            "content": [{"type": "text", "text": full_error}],
            "isError": True,
            "diagnostics": {"warnings": "", "improvements": improvements},
        }

    except Exception as e:
        tb_io = io.StringIO()
        traceback.print_exc(file=tb_io)
        tb_text = tb_io.getvalue().strip()
        improvements = _suggest_improvements(e, tb_text, tool_name)
        print(f"[ERROR] Tool '{tool_name}' failed: {e}", file=sys.stderr)
        return {
            "content": [{"type": "text", "text": tb_text}],
            "isError": True,
            "diagnostics": {"warnings": "", "improvements": improvements},
        }


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


class MCPBridge:
    """In-process MCP tool registry — no subprocess required."""

    def __init__(self):
        self.initialized = False
        self._lock = threading.RLock()
        # Start plugin loader in background; endpoints wait on _plugins_loaded
        self._start_plugin_loader()

    def _start_plugin_loader(self):
        def _bg():
            try:
                load_plugins()
                print(f"[INFO] Plugin loader finished — {len(TOOLS)} tools loaded",
                      file=sys.stderr)
            except Exception as e:
                print(f"[ERROR] Plugin loader crashed: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
            finally:
                _plugins_loaded.set()
                self.initialized = True

        t = threading.Thread(target=_bg, daemon=True, name="plugin-loader")
        t.start()

    @property
    def tools_cache(self) -> Dict[str, Any]:
        """Expose the in-process TOOLS registry under the same name used by callers."""
        return TOOLS

    def shutdown(self):
        print("[INFO] MCPBridge shutdown", file=sys.stderr)

    # ------------------------------------------------------------------
    # Public API (called from async route handlers via run_in_executor)
    # ------------------------------------------------------------------

    def list_tools(self) -> Dict[str, Any]:
        _plugins_loaded.wait(timeout=120)
        tools_list = []
        for key, info in TOOLS.items():
            tools_list.append({
                "name": info.get("name", key),
                "description": info.get("description", ""),
                "inputSchema": info.get("inputSchema", {}),
                "category": info.get("category"),
            })
        return {"tools": tools_list}

    def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        username: str = None,
        project:  str = None,
        category: str = None,
    ) -> Dict[str, Any]:
        return _call_tool_direct(tool_name, arguments, username, project, category)

    def list_resources(self) -> Dict[str, Any]:
        resources_list = [
            {"uri": uri, "name": info["name"],
             "description": info["description"], "mimeType": info["mimeType"]}
            for uri, info in RESOURCES.items()
        ]
        return {"resources": resources_list}

    def read_resource(self, uri: str) -> Dict[str, Any]:
        if uri not in RESOURCES:
            raise ValueError(f"Unknown resource: {uri}")
        info = RESOURCES[uri]
        return {"contents": [{"uri": uri, "mimeType": info["mimeType"],
                               "text": info.get("content", "")}]}

    def reload_tools(self) -> Dict[str, Any]:
        """Reload all tool plugins from disk."""
        try:
            print("[INFO] Reloading tools in-process...", file=sys.stderr)
            counts = reload_plugins()
            tool_count = counts.get("tools", 0)
            print(f"[INFO] Reload done — {tool_count} tools", file=sys.stderr)
            return {"success": True, "counts": {"tools": tool_count,
                                                 "resources": counts.get("resources", 0),
                                                 "prompts": counts.get("prompts", 0)}}
        except Exception as e:
            print(f"[ERROR] Reload failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return {"success": False, "error": str(e)}


# Global bridge instance
bridge = MCPBridge()


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/")
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


@app.get("/api/tools")
async def get_tools():
    """List all available tools."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, bridge.list_tools)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tools/{tool_name}")
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

        # Determine upload scope from argument path values.
        # Use local variables (_upload_*) to avoid shadowing the 'username' and
        # 'project' parameters that were passed by the caller.
        if S3_AVAILABLE and args:
            for _arg_val in args.values():
                if isinstance(_arg_val, str) and _arg_val.startswith('data/') and '/tools/' in _arg_val:
                    _parts = _arg_val.split('/')
                    if len(_parts) >= 5:
                        _upload_username = _parts[1]
                        _upload_project = _parts[2]
                        # parts[3] == 'tools'
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


@app.get("/api/resources")
async def get_resources():
    """List all available resources."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, bridge.list_resources)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/resources/{uri:path}")
async def read_resource(uri: str):
    """Read a specific resource by URI."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: bridge.read_resource(uri))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Health check endpoint (also used by Render)."""
    return {
        "status": "healthy",
        "initialized": bridge.initialized,
        "tools_count": len(bridge.tools_cache),
    }


@app.post("/reload-tools")
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
                    print(f"[INFO] {tag}Attaching {kind} ({len(data)} bytes): {candidate_name}", file=sys.stderr)
                else:
                    print(f"[WARN] {tag}Binary file referenced by port not found: {bin_file}", file=sys.stderr)
        except Exception as read_err:
            print(f"[WARN] {tag}Could not read output file {rel_path}: {read_err}", file=sys.stderr)

    return output_files


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _lookup_user_id(s3_client: Any, bucket_name: str, username: str, project: str = None) -> str:
    """Resolve the numeric user_id for an (username, project) pair.

    Results are cached so we only do the full-bucket paginator scan once per
    (username, project) combination per process lifetime.
    """
    cache_key: Tuple[str, str] = (username, project or "")
    if cache_key in _user_id_cache:
        return _user_id_cache[cache_key]

    uid = "10"  # safe fallback
    try:
        search_pattern = f"{username}/{project}/" if project else f"{username}/"
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket_name, Prefix="users/"):
            for obj in page.get('Contents', []):
                s3_key = obj['Key']
                if search_pattern in s3_key:
                    parts = s3_key.split('/')
                    if len(parts) >= 2 and parts[0] == 'users':
                        uid = parts[1]
                        print(f"[INFO] Resolved user_id={uid} for {username}", file=sys.stderr)
                        break
            else:
                continue
            break
    except Exception as e:
        print(f"[WARN] _lookup_user_id failed: {e}", file=sys.stderr)

    _user_id_cache[cache_key] = uid
    if uid == "10":
        print(f"[WARN] Could not resolve user_id for {username} — using fallback '10'", file=sys.stderr)
    return uid


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

        s3_client = _get_s3_client()

        outputs_path = (
            data_dir / username / project / "tools" / category / tool_name / "outputs"
            if category
            else data_dir / username / project / "tools" / tool_name / "outputs"
        )

        if not outputs_path.exists():
            return {"success": True, "files_uploaded": 0, "message": "No output files"}

        user_id = _lookup_user_id(s3_client, bucket_name, username, project)
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


@app.post("/upload-outputs-to-s3")
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

    # Determine the upload scope
    if username and project and tool_name:
        flat_path = data_dir / username / project / "tools" / tool_name / "outputs"
        if flat_path.exists():
            local_path = flat_path
        else:
            tools_dir = data_dir / username / project / "tools"
            local_path = flat_path  # default; will return "no files" if missing
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

    s3_client = _get_s3_client()
    loop = asyncio.get_running_loop()

    def _do_upload():
        files_uploaded = 0
        uid = _lookup_user_id(s3_client, bucket_name, username, project)
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


def _sync_tools_from_s3_sync(
    username: str = None,
    project: str = None,
    clean_first: bool = False,
) -> dict:
    """Download tools from S3 to local disk then reload the tool registry.

    Blocking (uses synchronous boto3 API) — always call via run_in_executor.
    """
    if not S3_AVAILABLE:
        return {"success": False, "error": "boto3 not installed - S3 sync not available"}

    try:
        aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
        aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        bucket_name = os.environ.get('S3_BUCKET_NAME', 'grafux-user-files')

        if not aws_access_key or not aws_secret_key:
            return {"success": False, "error": "AWS credentials not configured"}

        s3_client = _get_s3_client()

        print(f"[INFO] Listing S3 objects (bucket={bucket_name}, prefix=users/)")
        paginator = s3_client.get_paginator('list_objects_v2')
        all_objects = []
        for page in paginator.paginate(Bucket=bucket_name, Prefix="users/"):
            all_objects.extend(page.get('Contents', []))

        if not all_objects:
            print("[WARN] No objects found in S3")
            return {"success": True, "message": "No files found in S3", "files_synced": 0}

        print(f"[INFO] Found {len(all_objects)} objects in S3")

        data_dir = _get_project_root() / "data"
        data_dir.mkdir(exist_ok=True)
        print(f"[INFO] Local data directory: {data_dir}")
        # #region agent log
        import json as _json2, time as _time2
        try:
            with open("debug-c5eccb.log", "a") as _lf2:
                _lf2.write(_json2.dumps({"sessionId": "c5eccb", "hypothesisId": "B-C", "location": "mcp_server.py:_sync_tools_from_s3_sync", "message": "data_dir resolved", "data": {"data_dir": str(data_dir), "exists": data_dir.exists()}, "timestamp": int(_time2.time() * 1000)}) + "\n")
        except Exception:
            pass
        # #endregion

        # Optional pre-clean
        import shutil
        if clean_first:
            if username and project:
                target = data_dir / username / project
            elif username:
                target = data_dir / username
            else:
                target = None
            if target and target.exists():
                try:
                    shutil.rmtree(target)
                    print(f"[INFO] ✓ Cleaned {target}")
                except Exception as e:
                    print(f"[WARN] Clean failed: {e}")

        files_synced = 0
        s3_files: set = set()
        _logged_keys = 0

        for obj in all_objects:
            s3_key = obj['Key']
            if s3_key.endswith('/'):
                continue
            if username and username not in s3_key:
                continue
            if project and project not in s3_key:
                continue
            if '/tools/' not in s3_key:
                continue

            # Strip "users/{user_id}/" prefix → local relative path
            parts = s3_key.split('/')
            local_key = '/'.join(parts[2:]) if len(parts) >= 3 and parts[0] == 'users' else s3_key
            s3_files.add(local_key)

            local_path = data_dir / local_key
            # #region agent log
            if _logged_keys < 5:
                import json as _json3, time as _time3
                try:
                    with open("debug-c5eccb.log", "a") as _lf3:
                        _lf3.write(_json3.dumps({"sessionId": "c5eccb", "hypothesisId": "C-E", "location": "mcp_server.py:sync_loop", "message": "s3 key → local path", "data": {"s3_key": s3_key, "local_key": local_key, "local_path": str(local_path)}, "timestamp": int(_time3.time() * 1000)}) + "\n")
                except Exception:
                    pass
                _logged_keys += 1
            # #endregion
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3_client.download_file(bucket_name, s3_key, str(local_path))
                files_synced += 1
                print(f"[INFO] ✓ Downloaded: {s3_key}")
            except Exception as e:
                print(f"[ERROR] Download failed ({s3_key}): {e}")

        print(f"[INFO] Synced {files_synced} files from S3")

        # Delete orphaned local files (exist locally but not in S3)
        files_deleted = 0
        dirs_deleted = 0
        tools_paths = []
        if username and project:
            tools_paths = [data_dir / username / project / "tools"]
        elif username:
            ud = data_dir / username
            if ud.exists():
                tools_paths = [p / "tools" for p in ud.iterdir() if p.is_dir()]
        elif data_dir.exists():
            for ud in data_dir.iterdir():
                if ud.is_dir() and not ud.name.startswith('.'):
                    for pd in ud.iterdir():
                        if pd.is_dir() and not pd.name.startswith('.'):
                            tp = pd / "tools"
                            if tp.exists():
                                tools_paths.append(tp)

        for tp in tools_paths:
            if not tp.exists():
                continue
            for lf in tp.rglob("*"):
                if not lf.is_file():
                    continue
                try:
                    if lf.relative_to(data_dir).as_posix() not in s3_files:
                        lf.unlink()
                        files_deleted += 1
                        print(f"[INFO] ✓ Deleted orphan: {lf}")
                except Exception as e:
                    print(f"[ERROR] Delete check failed ({lf}): {e}")
            for ld in sorted(tp.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if ld.is_dir():
                    try:
                        if not any(ld.iterdir()):
                            ld.rmdir()
                            dirs_deleted += 1
                    except Exception:
                        pass

        if files_deleted or dirs_deleted:
            print(f"[INFO] Cleaned {files_deleted} orphaned files, {dirs_deleted} empty dirs")

        # Reload the tool registry after syncing new files
        tools_loaded = 0
        try:
            reload_result = bridge.reload_tools()
            if reload_result.get("success"):
                tools_loaded = reload_result.get("counts", {}).get("tools", 0)
        except Exception as e:
            print(f"[ERROR] Reload after sync failed: {e}")
            traceback.print_exc()

        return {
            "success": True,
            "message": (
                f"Synced {files_synced} files, deleted {files_deleted} orphans, "
                f"{tools_loaded} tools loaded"
            ),
            "files_synced": files_synced,
            "files_deleted": files_deleted,
            "dirs_deleted": dirs_deleted,
            "tools_loaded": tools_loaded,
            "data_directory": str(data_dir),
        }

    except Exception as e:
        print(f"[ERROR] S3 sync error: {e}")
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@app.post("/sync-tools-from-s3")
async def sync_tools_from_s3(
    username: str = None, project: str = None, clean_first: bool = False
):
    """Download tools from S3 to local disk and reload the tool registry."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _sync_tools_from_s3_sync(username, project, clean_first),
    )
    if result.get("success"):
        return JSONResponse(content=result)
    error_msg = result.get("error", "")
    return JSONResponse(
        status_code=501 if "boto3 not installed" in error_msg else 500,
        content=result,
    )


def _sync_single_tool_sync(
    username: str,
    project: str,
    tool_name: str,
    category: str = None,
    user_id: str = None,
) -> dict:
    """Download a single tool from S3 using a precise prefix, then reload.

    Much faster than _sync_tools_from_s3_sync because it lists only the files
    under users/{user_id}/{username}/{project}/tools/{category}/{tool_name}/
    instead of scanning the entire bucket.

    Blocking — always call via run_in_executor.
    """
    if not S3_AVAILABLE:
        return {"success": False, "error": "boto3 not installed - S3 sync not available"}

    try:
        aws_access_key = os.environ.get('AWS_ACCESS_KEY_ID')
        aws_secret_key = os.environ.get('AWS_SECRET_ACCESS_KEY')
        bucket = os.environ.get('S3_BUCKET_NAME', 'grafux-user-files')

        if not aws_access_key or not aws_secret_key:
            return {"success": False, "error": "AWS credentials not configured"}

        s3_client = _get_s3_client()

        # Populate the user_id cache if the caller already knows it (avoids a
        # full-bucket scan).  Otherwise fall back to the cached lookup.
        if user_id:
            _user_id_cache[(username, project or "")] = user_id
        else:
            user_id = _lookup_user_id(s3_client, bucket, username, project)

        prefix = (
            f"users/{user_id}/{username}/{project}/tools/{category}/{tool_name}/"
            if category
            else f"users/{user_id}/{username}/{project}/tools/{tool_name}/"
        )

        print(f"[INFO] Syncing single tool '{tool_name}' from S3 prefix: {prefix}")

        data_dir = _get_project_root() / "data"
        data_dir.mkdir(exist_ok=True)

        files_synced = 0
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get('Contents', []):
                s3_key = obj['Key']
                if s3_key.endswith('/'):
                    continue
                # Strip "users/{user_id}/" to get the local relative path
                parts = s3_key.split('/', 2)
                local_key = parts[2] if len(parts) == 3 else s3_key
                local_path = data_dir / local_key
                local_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    s3_client.download_file(bucket, s3_key, str(local_path))
                    files_synced += 1
                    print(f"[INFO] ✓ Downloaded: {s3_key}")
                except Exception as e:
                    print(f"[ERROR] Download failed ({s3_key}): {e}")

        print(f"[INFO] Single-tool sync: {files_synced} files downloaded for '{tool_name}'")

        # Reload the tool registry so the new Python module is registered
        tools_loaded = 0
        try:
            reload_result = bridge.reload_tools()
            if reload_result.get("success"):
                tools_loaded = reload_result.get("counts", {}).get("tools", 0)
        except Exception as e:
            print(f"[ERROR] Reload after single-tool sync failed: {e}")

        return {
            "success": True,
            "files_synced": files_synced,
            "tools_loaded": tools_loaded,
            "tool_name": tool_name,
        }

    except Exception as e:
        print(f"[ERROR] Single-tool sync error: {e}")
        traceback.print_exc()
        return {"success": False, "error": str(e)}


@app.post("/sync-tool")
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
        lambda: _sync_single_tool_sync(username, project, tool_name, category, user_id),
    )
    if result.get("success"):
        return JSONResponse(content=result)
    error_msg = result.get("error", "")
    return JSONResponse(
        status_code=501 if "boto3 not installed" in error_msg else 500,
        content=result,
    )


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
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
                # Send a JSON-RPC notification; well-behaved clients ignore it.
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

            # Client-originated ping — reply immediately
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
                    counts = reload_result.get("counts", {}) if isinstance(reload_result, dict) else {}
                    result = {
                        "success": bool(reload_result.get("success")) if isinstance(reload_result, dict) else False,
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
                        await send_error(request_id, -32602, "Invalid arguments: object expected")
                        continue

                    # Extract username/project from argument path values for namespaced lookup
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

                    # Attach output files inline so WASM client can update its virtual FS
                    if output_port_paths and isinstance(result, dict):
                        output_files = _build_output_files(
                            _get_project_root(), output_port_paths, log_prefix="WS"
                        )
                        if output_files:
                            result["output_files"] = output_files

                    # Fire-and-forget S3 upload — does not block the WebSocket response
                    if S3_AVAILABLE and arguments:
                        for _v in arguments.values():
                            if isinstance(_v, str) and _v.startswith('data/') and '/tools/' in _v:
                                _parts = _v.split('/')
                                if len(_parts) >= 6:
                                    _u2, _pr2, _t2, _c2 = _parts[1], _parts[2], _parts[5], _parts[4]
                                elif len(_parts) >= 5:
                                    _u2, _pr2, _t2, _c2 = _parts[1], _parts[2], _parts[4], None
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


# ── Background tasks ──────────────────────────────────────────────────────────

async def periodic_sync_task():
    """Re-download tools from S3 at a fixed interval (skipped when interval=0)."""
    # Brief delay to avoid overlapping with the startup sync
    await asyncio.sleep(60)
    while True:
        try:
            if SYNC_INTERVAL_SECONDS > 0:
                await asyncio.sleep(SYNC_INTERVAL_SECONDS)
                print("[INFO] Periodic sync — downloading tools from S3...")
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(None, _sync_tools_from_s3_sync)
                if result.get("success"):
                    print(
                        f"[INFO] Periodic sync done: "
                        f"{result.get('files_synced', 0)} files, "
                        f"{result.get('tools_loaded', 0)} tools"
                    )
                else:
                    print(f"[ERROR] Periodic sync failed: {result.get('error')}")
            else:
                await asyncio.sleep(86400)
        except Exception as e:
            print(f"[ERROR] Periodic sync task error: {e}")
            traceback.print_exc()
            await asyncio.sleep(300)


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
            loop.run_in_executor(None, _sync_tools_from_s3_sync),
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
