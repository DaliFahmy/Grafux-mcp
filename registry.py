"""
registry.py — Tool / Resource / Prompt plugin system.

Owns the in-memory registries and the filesystem scanner that populates them.
"""

import importlib.util
import shutil
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from config import _get_project_root


# ── In-memory registries ──────────────────────────────────────────────────────

_plugins_loaded = threading.Event()
TOOLS:         Dict[str, Dict[str, Any]] = {}
RESOURCES:     Dict[str, Dict[str, Any]] = {}
PROMPTS:       Dict[str, Dict[str, Any]] = {}
PLUGIN_ERRORS: List[Dict[str, Any]]      = []


# ── Registration decorators / helpers ─────────────────────────────────────────

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


# ── Editable-code helpers ─────────────────────────────────────────────────────

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


# ── Plugin loader ─────────────────────────────────────────────────────────────

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
                            print(f"[INFO] ✓ Loaded {py_file.stem} ({uname}/{pname}/{cname})",
                                  file=sys.stderr)
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
