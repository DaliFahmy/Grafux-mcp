"""
app/core/runtime/local_runner.py — In-process Python tool plugin runner.

Replaces the original bridge.py + registry.py + executor.py flat files.
Preserves full backward compatibility with the @register_tool decorator API
and the data/{username}/{project}/tools/{category}/{tool}/references/*.py
filesystem layout so existing tools need zero modification.

Also retains the S3 ETag-aware sync from block_downloader.py.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import threading
import traceback
import warnings as _warnings_module
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import boto3 as _boto3  # noqa: F401
    S3_AVAILABLE = True
except ImportError:
    _boto3 = None  # type: ignore
    S3_AVAILABLE = False

from app.config import settings


# ── Project root resolution ───────────────────────────────────────────────────

def _get_data_root() -> Path:
    """Return the data/ directory at the Grafux-mcp repo root."""
    return Path(__file__).resolve().parents[3] / "data"


def _get_tools_root() -> Path:
    """Return the tools/ directory for built-in system tools."""
    return Path(__file__).resolve().parents[3] / "tools"


# ── In-memory registries ──────────────────────────────────────────────────────

_plugins_loaded = threading.Event()
TOOLS:         Dict[str, Dict[str, Any]] = {}
RESOURCES:     Dict[str, Dict[str, Any]] = {}
PROMPTS:       Dict[str, Dict[str, Any]] = {}
PLUGIN_ERRORS: List[Dict[str, Any]]      = []
_reload_lock = threading.Lock()


# ── Registration decorators ───────────────────────────────────────────────────

def register_tool(
    name: str,
    description: str,
    input_schema: dict,
    username: str | None = None,
    project: str | None = None,
    category: str | None = None,
) -> Callable:
    """Decorator — register a tool function with JSON Schema and owner metadata."""

    def wrapper(func: Callable) -> Callable:
        if username and project and category:
            key = f"{username}/{project}/{category}/{name}"
        elif username and project:
            key = f"{username}/{project}/{name}"
        else:
            key = name
        if key in TOOLS:
            print(f"[WARN] Duplicate tool key '{key}' — replacing", file=sys.stderr)
        TOOLS[key] = {
            "func": func,
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "username": username,
            "project": project,
            "category": category,
        }
        return func

    return wrapper


def register_resource(
    uri: str, name: str, description: str = "",
    mime_type: str = "text/plain", content: str = "",
) -> None:
    RESOURCES[uri] = {
        "name": name, "description": description,
        "mimeType": mime_type, "uri": uri, "content": content,
    }


def register_prompt(name: str, description: str, arguments: list | None = None):
    def wrapper(func: Callable) -> Callable:
        PROMPTS[name] = {"func": func, "description": description, "arguments": arguments or []}
        return func
    return wrapper


# ── Plugin loader ─────────────────────────────────────────────────────────────

def _load_directory(
    base: Path,
    *,
    username: str | None = None,
    project: str | None = None,
    category: str | None = None,
) -> int:
    """Load all *.py files from a single references/ directory."""
    tools_found = 0
    py_files = [f for f in base.glob("*.py") if not f.name.startswith("_")]
    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            compile(source, str(py_file), "exec")
        except SyntaxError as se:
            PLUGIN_ERRORS.append({"file": str(py_file), "error": str(se)})
            print(f"[ERROR] Syntax error in {py_file.name}: {se}", file=sys.stderr)
            continue

        try:
            parts = [p for p in [username, project, category, py_file.stem] if p]
            mod_name = ".".join(parts) if parts else py_file.stem
            spec = importlib.util.spec_from_file_location(mod_name, py_file)
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            if username and project and category:
                def _make_rt(u: str, p: str, c: str):
                    def _rt(n: str, desc: str, schema: dict):
                        return register_tool(n, desc, schema, u, p, c)
                    return _rt
                mod.register_tool = _make_rt(username, project, category)  # type: ignore[attr-defined]
            else:
                mod.register_tool = register_tool  # type: ignore[attr-defined]
            mod.register_resource = register_resource  # type: ignore[attr-defined]
            mod.register_prompt = register_prompt  # type: ignore[attr-defined]
            spec.loader.exec_module(mod)
            tools_found += 1
        except Exception as exc:
            PLUGIN_ERRORS.append({"file": str(py_file), "error": str(exc)})
            print(f"[ERROR] Failed to load {py_file.name}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    return tools_found


def load_plugins() -> None:
    """Scan data/ and tools/ directories and register all tool plugins."""
    total = 0

    # Built-in system tools (tools/ at repo root)
    system_tools = _get_tools_root()
    if system_tools.exists():
        total += _load_directory(system_tools)

    # User/org tools (data/{username}/{project}/tools/{category}/{tool}/references/)
    data_path = _get_data_root()
    if not data_path.exists():
        data_path.mkdir(parents=True, exist_ok=True)

    try:
        user_dirs = [d for d in data_path.iterdir() if d.is_dir() and not d.name.startswith(".")]
    except Exception as exc:
        print(f"[ERROR] Cannot list data dir: {exc}", file=sys.stderr)
        return

    for user_dir in user_dirs:
        for project_dir in (d for d in user_dir.iterdir() if d.is_dir() and not d.name.startswith(".")):
            tools_dir = project_dir / "tools"
            if not tools_dir.exists():
                continue
            for category_dir in (d for d in tools_dir.iterdir() if d.is_dir() and not d.name.startswith(".")):
                for tool_dir in (d for d in category_dir.iterdir() if d.is_dir() and not d.name.startswith(".")):
                    refs_dir = tool_dir / "references"
                    if refs_dir.exists():
                        total += _load_directory(
                            refs_dir,
                            username=user_dir.name,
                            project=project_dir.name,
                            category=category_dir.name,
                        )

    print(f"[INFO] Local plugin loader: {total} tools loaded", file=sys.stderr)


def reload_plugins() -> Dict[str, Any]:
    """Thread-safe full reload of all tool plugins."""
    global TOOLS, RESOURCES, PROMPTS, PLUGIN_ERRORS
    with _reload_lock:
        _plugins_loaded.clear()
        TOOLS.clear()
        RESOURCES.clear()
        PROMPTS.clear()
        PLUGIN_ERRORS.clear()

        # Purge cached tool modules from sys.modules
        _SKIP = (
            "google.", "vertexai.", "grpc.", "proto.", "boto", "botocore.",
            "flask.", "werkzeug.", "jinja2.", "requests.", "urllib3.",
            "pydantic.", "fastapi.", "uvicorn.", "starlette.", "anyio.", "sniffio.",
            "jwt.", "cryptography.", "stripe.", "httpx.", "httpcore.", "app.",
        )
        to_del = [
            m for m in sys.modules
            if "." in m and not m.startswith("_")
            and not any(m.startswith(p) for p in _SKIP)
            and len(m.split(".")) >= 3
        ]
        for m in to_del:
            del sys.modules[m]

        for cache_dir in _get_data_root().rglob("__pycache__"):
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass

        load_plugins()
        _plugins_loaded.set()
        return {
            "tools": len(TOOLS),
            "resources": len(RESOURCES),
            "prompts": len(PROMPTS),
            "errors": len(PLUGIN_ERRORS),
        }


# ── Editable-code helpers (live tool editing in Qt app) ──────────────────────

def _resolve_editable_path(arguments: Dict[str, Any]) -> Optional[Path]:
    raw_path = arguments.get("code") if isinstance(arguments, dict) else None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    candidate = Path(raw_path.replace("\\", "/"))
    if not candidate.is_absolute() and str(candidate).startswith("data/"):
        candidate = Path(__file__).resolve().parents[3] / candidate
    try:
        candidate = candidate.resolve()
    except Exception:
        pass
    return candidate if candidate.exists() and candidate.is_file() else None


def _load_editable_func(
    tool_metadata: Dict[str, Any], arguments: Dict[str, Any]
) -> Optional[Callable]:
    code_path = _resolve_editable_path(arguments)
    if not code_path:
        return None
    source = code_path.read_text(encoding="utf-8")
    compiled = compile(source, str(code_path), "exec")
    captured: Dict[str, Any] = {}

    def _capture_rt(name: str, description: str, input_schema: dict):
        def inner(func: Callable) -> Callable:
            captured.update({"func": func, "name": name,
                             "description": description, "inputSchema": input_schema})
            return func
        return inner

    exec(compiled, {
        "__file__": str(code_path),
        "__name__": f"_editable_{tool_metadata.get('name', 'tool')}",
        "register_tool": _capture_rt,
        "register_resource": register_resource,
        "register_prompt": register_prompt,
    })
    func = captured.get("func")
    if not callable(func):
        raise ValueError("Editable code did not call @register_tool")
    return func


# ── Error hint generator ──────────────────────────────────────────────────────

def _suggest_fix(exc: Exception) -> str:
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in ("ImportError", "ModuleNotFoundError"):
        mod = str(exc).split("'")[1] if "'" in str(exc) else str(exc).split()[-1]
        return f"Missing module — run: pip install {mod}"
    if name == "NameError":
        return "Name used before definition. Check for typos."
    if name == "AttributeError":
        return "Attribute/method not found. Check the object type and attribute name."
    if name == "TypeError":
        return "Wrong argument type or count. Check function signatures."
    if name == "KeyError":
        return f"Key {exc} missing. Use .get() or verify the key exists."
    if name == "ValueError":
        return "Invalid value. Check input data and expected ranges."
    if name in ("FileNotFoundError", "IsADirectoryError"):
        return "File not found. Check paths and working directory."
    if "timeout" in msg or name in ("TimeoutError", "asyncio.TimeoutError"):
        return "Operation timed out. Check API calls and long-running operations."
    if any(w in msg for w in ("connection", "refused", "unreachable", "network")):
        return "Network error. Check connectivity and remote service availability."
    return f"{name}: review the traceback for the failing line."


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def call_tool_direct(
    tool_name: str,
    arguments: Dict[str, Any],
    caller_username: str | None = None,
    caller_project: str | None = None,
    caller_category: str | None = None,
) -> Dict[str, Any]:
    """Look up and execute a registered local tool synchronously."""
    if not _plugins_loaded.is_set():
        _plugins_loaded.wait(timeout=120)

    # Key resolution: namespaced → direct → display-name scan
    tool_key = tool_meta = None
    if caller_username and caller_project:
        candidates = []
        if caller_category:
            candidates.append(f"{caller_username}/{caller_project}/{caller_category}/{tool_name}")
        candidates.append(f"{caller_username}/{caller_project}/{tool_name}")
        for k in candidates:
            if k in TOOLS:
                tool_key, tool_meta = k, TOOLS[k]
                break

    if not tool_key:
        if tool_name in TOOLS:
            tool_key, tool_meta = tool_name, TOOLS[tool_name]
        else:
            for k, info in TOOLS.items():
                if info.get("name") == tool_name:
                    if caller_username and caller_project:
                        if info.get("username") == caller_username and info.get("project") == caller_project:
                            if caller_category and info.get("category") != caller_category:
                                continue
                            tool_key, tool_meta = k, info
                            break
                    else:
                        tool_key, tool_meta = k, info
                        break

    # Fallback: editable-code (live-chat tools without references/*.py)
    if not tool_key:
        try:
            fb_func = _load_editable_func({"name": tool_name}, arguments)
        except Exception:
            fb_func = None
        if fb_func:
            try:
                result = fb_func(arguments)
                if not isinstance(result, dict) or "content" not in result:
                    result = {"content": [{"type": "text", "text": str(result)}]}
                return result
            except Exception as exc:
                tb = traceback.format_exc()
                return {
                    "content": [{"type": "text", "text": tb}],
                    "isError": True,
                    "diagnostics": {"improvements": _suggest_fix(exc)},
                }
        raise ValueError(f"Unknown tool: {tool_name}")

    # Authorization check
    tu, tp = tool_meta.get("username"), tool_meta.get("project")
    if tu and tp:
        if not caller_username or not caller_project:
            raise ValueError("Missing username/project for namespaced tool")
        if tu != caller_username or tp != caller_project:
            raise ValueError(f"Unauthorized: tool '{tool_name}' belongs to {tu}/{tp}")

    # Execution
    try:
        caught_warns: List[str] = []
        with _warnings_module.catch_warnings(record=True) as cw:
            _warnings_module.simplefilter("always")
            editable_func = _load_editable_func(tool_meta, arguments)
            result = (editable_func or tool_meta["func"])(arguments)
            if not isinstance(result, dict) or "content" not in result:
                result = {"content": [{"type": "text", "text": str(result)}]}
            caught_warns = [
                f"{Path(w.filename).name if w.filename else 'code'}:{w.lineno}: "
                f"{w.category.__name__}: {w.message}"
                for w in cw
                if not issubclass(w.category, (DeprecationWarning, PendingDeprecationWarning))
            ]
        if caught_warns:
            result.setdefault("diagnostics", {})["warnings"] = "\n".join(caught_warns)
        return result

    except SyntaxError as se:
        tb = traceback.format_exc()
        pointer = " " * max((se.offset or 1) - 1, 0) + "^"
        lines = [f"{Path(se.filename).name if se.filename else 'code'}:{se.lineno}: SyntaxError: {se.msg}"]
        if se.text:
            lines += [f"    {se.text.rstrip()}", f"    {pointer}"]
        lines.append(tb)
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "isError": True,
            "diagnostics": {"improvements": "Fix syntax error at indicated line."},
        }

    except Exception as exc:
        return {
            "content": [{"type": "text", "text": traceback.format_exc()}],
            "isError": True,
            "diagnostics": {"improvements": _suggest_fix(exc)},
        }


def list_local_tools() -> List[Dict[str, Any]]:
    """Return serializable list of all loaded local tools."""
    _plugins_loaded.wait(timeout=120)
    return [
        {
            "name": info["name"],
            "description": info.get("description", ""),
            "inputSchema": info.get("inputSchema", {}),
            "category": info.get("category"),
            "username": info.get("username"),
            "project": info.get("project"),
            "source": "local_plugin",
        }
        for info in TOOLS.values()
    ]


# ── S3 ETag-aware sync (preserved from block_downloader.py) ──────────────────

class _S3Syncer:
    def __init__(self) -> None:
        self._client: Any = None
        self._creds: Tuple[str, str] = ("", "")
        self._uid_cache: Dict[Tuple[str, str], str] = {}
        self._lock = threading.Lock()

    @property
    def _etag_path(self) -> Path:
        p = _get_data_root()
        p.mkdir(exist_ok=True)
        return p / ".s3_etag_cache.json"

    def _load_etags(self) -> dict:
        if self._etag_path.exists():
            try:
                return json.loads(self._etag_path.read_text())
            except Exception:
                return {}
        return {}

    def _save_etags(self, cache: dict) -> None:
        tmp = self._etag_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(cache))
            tmp.replace(self._etag_path)
        except Exception:
            pass

    def _get_client(self) -> Any:
        if _boto3 is None:
            raise RuntimeError("boto3 not installed")
        key = (
            os.environ.get("AWS_ACCESS_KEY_ID", ""),
            os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        )
        if self._client is None or key != self._creds:
            self._client = _boto3.client("s3", aws_access_key_id=key[0], aws_secret_access_key=key[1])
            self._creds = key
        return self._client

    def lookup_user_id(self, username: str, project: str | None = None) -> str:
        cache_key = (username, project or "")
        if cache_key in self._uid_cache:
            return self._uid_cache[cache_key]
        uid = "10"
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            pattern = f"{username}/{project}/" if project else f"{username}/"
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix="users/"):
                for obj in page.get("Contents", []):
                    if pattern in obj["Key"]:
                        parts = obj["Key"].split("/")
                        if len(parts) >= 2 and parts[0] == "users":
                            uid = parts[1]
                            break
                else:
                    continue
                break
        except Exception:
            pass
        self._uid_cache[cache_key] = uid
        return uid

    def sync_all(
        self,
        username: str | None = None,
        project: str | None = None,
        clean_first: bool = False,
    ) -> Dict[str, Any]:
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed"}
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            return {"success": False, "error": "AWS credentials not configured"}
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            data_dir = _get_data_root()
            data_dir.mkdir(exist_ok=True)

            paginator = s3.get_paginator("list_objects_v2")
            all_objs: list = []
            for page in paginator.paginate(Bucket=bucket, Prefix="users/"):
                all_objs.extend(page.get("Contents", []))

            if not all_objs:
                return {"success": True, "files_synced": 0, "message": "No files in S3"}

            if clean_first:
                target = data_dir / username if username else None
                if project and username:
                    target = data_dir / username / project
                if target and target.exists():
                    shutil.rmtree(target, ignore_errors=True)

            files_synced = files_skipped = files_deleted = 0
            s3_files: set[str] = set()

            with self._lock:
                etag_cache = self._load_etags()
                for obj in all_objs:
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    if username and username not in key:
                        continue
                    if project and project not in key:
                        continue
                    if "/tools/" not in key:
                        continue
                    parts = key.split("/")
                    local_key = "/".join(parts[2:]) if len(parts) >= 3 and parts[0] == "users" else key
                    s3_files.add(local_key)
                    local_path = data_dir / local_key
                    s3_etag = obj.get("ETag", "").strip('"')
                    if etag_cache.get(key) == s3_etag and local_path.exists():
                        files_skipped += 1
                        continue
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        s3.download_file(bucket, key, str(local_path))
                        etag_cache[key] = s3_etag
                        files_synced += 1
                    except Exception as exc:
                        print(f"[ERROR] S3 download failed {key}: {exc}", file=sys.stderr)

                # Delete orphans
                for tp in data_dir.rglob("tools"):
                    if not tp.is_dir():
                        continue
                    for lf in tp.rglob("*"):
                        if lf.is_file():
                            try:
                                lf_key = lf.relative_to(data_dir).as_posix()
                                if lf_key not in s3_files:
                                    lf.unlink()
                                    files_deleted += 1
                            except Exception:
                                pass

                self._save_etags(etag_cache)

            tools_loaded = 0
            if files_synced or files_deleted:
                result = reload_plugins()
                tools_loaded = result.get("tools", 0)

            return {
                "success": True,
                "files_synced": files_synced,
                "files_skipped": files_skipped,
                "files_deleted": files_deleted,
                "tools_loaded": tools_loaded,
            }
        except Exception as exc:
            traceback.print_exc()
            return {"success": False, "error": str(exc)}

    def sync_tool(
        self,
        username: str,
        project: str,
        tool_name: str,
        category: str | None = None,
        user_id: str | None = None,
    ) -> Dict[str, Any]:
        if not S3_AVAILABLE:
            return {"success": False, "error": "boto3 not installed"}
        if not os.environ.get("AWS_ACCESS_KEY_ID"):
            return {"success": False, "error": "AWS credentials not configured"}
        try:
            s3 = self._get_client()
            bucket = settings.s3_bucket_name
            data_dir = _get_data_root()
            uid = user_id or self.lookup_user_id(username, project)
            prefix = (
                f"users/{uid}/{username}/{project}/tools/{category}/{tool_name}/"
                if category
                else f"users/{uid}/{username}/{project}/tools/{tool_name}/"
            )
            files_synced = files_skipped = 0
            paginator = s3.get_paginator("list_objects_v2")
            with self._lock:
                etag_cache = self._load_etags()
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        if key.endswith("/"):
                            continue
                        parts = key.split("/", 2)
                        local_key = parts[2] if len(parts) == 3 else key
                        local_path = data_dir / local_key
                        s3_etag = obj.get("ETag", "").strip('"')
                        if etag_cache.get(key) == s3_etag and local_path.exists():
                            files_skipped += 1
                            continue
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            s3.download_file(bucket, key, str(local_path))
                            etag_cache[key] = s3_etag
                            files_synced += 1
                        except Exception as exc:
                            print(f"[ERROR] S3 single-tool download failed {key}: {exc}", file=sys.stderr)
                self._save_etags(etag_cache)

            tools_loaded = 0
            if files_synced:
                result = reload_plugins()
                tools_loaded = result.get("tools", 0)

            return {"success": True, "files_synced": files_synced, "files_skipped": files_skipped, "tools_loaded": tools_loaded}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


s3_syncer = _S3Syncer()


# ── Module-level initialisation ───────────────────────────────────────────────

def start_plugin_loader() -> None:
    """Kick off background plugin loading. Called once at app startup."""
    def _bg() -> None:
        try:
            load_plugins()
        except Exception as exc:
            print(f"[ERROR] Plugin loader crashed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        finally:
            _plugins_loaded.set()

    t = threading.Thread(target=_bg, daemon=True, name="plugin-loader")
    t.start()
