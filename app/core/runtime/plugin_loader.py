"""app/core/runtime/plugin_loader.py — Plugin discovery, registration and registries.

Owns the in-memory tool/resource/prompt registries and the filesystem discovery
that populates them from the ``data/{username}/{project}/tools/{category}/{tool}/
references/*.py`` layout (plus built-in ``tools/`` at the repo root).

The registries are plain module-level dicts mutated in place — ``reload_plugins()``
clears and repopulates them rather than reassigning, so references imported
elsewhere (e.g. the WS gateway's ``TOOLS``) stay valid across reloads.
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Project root resolution ───────────────────────────────────────────────────


def _get_data_root() -> Path:
    """Return the data/ directory at the Grafux-mcp repo root."""
    return Path(__file__).resolve().parents[3] / "data"


def _get_tools_root() -> Path:
    """Return the tools/ directory for built-in system tools."""
    return Path(__file__).resolve().parents[3] / "tools"


# ── In-memory registries ──────────────────────────────────────────────────────

_plugins_loaded = threading.Event()
TOOLS: dict[str, dict[str, Any]] = {}
# Secondary index: display name → registry keys, so a display-name lookup is O(1)
# instead of a full scan of TOOLS (a tool's display name is not unique across
# username/project/category, hence a list of keys per name).
NAME_INDEX: dict[str, list[str]] = {}
RESOURCES: dict[str, dict[str, Any]] = {}
PROMPTS: dict[str, dict[str, Any]] = {}
PLUGIN_ERRORS: list[dict[str, Any]] = []
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
            logger.warning("Duplicate tool key '%s' — replacing", key)
        TOOLS[key] = {
            "func": func,
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "username": username,
            "project": project,
            "category": category,
        }
        keys = NAME_INDEX.setdefault(name, [])
        if key not in keys:
            keys.append(key)
        return func

    return wrapper


def register_resource(
    uri: str,
    name: str,
    description: str = "",
    mime_type: str = "text/plain",
    content: str = "",
) -> None:
    RESOURCES[uri] = {
        "name": name,
        "description": description,
        "mimeType": mime_type,
        "uri": uri,
        "content": content,
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
            logger.error("Syntax error in %s: %s", py_file.name, se)
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
                    def _rt(
                        name: str,
                        description: str,
                        input_schema: dict,
                    ):
                        return register_tool(name, description, input_schema, u, p, c)

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
            logger.exception("Failed to load %s: %s", py_file.name, exc)

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
        logger.error("Cannot list data dir: %s", exc)
        return

    for user_dir in user_dirs:
        for project_dir in (
            d for d in user_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
        ):
            tools_dir = project_dir / "tools"
            if not tools_dir.exists():
                continue
            for category_dir in (
                d for d in tools_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
            ):
                for tool_dir in (
                    d for d in category_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
                ):
                    refs_dir = tool_dir / "references"
                    if refs_dir.exists():
                        total += _load_directory(
                            refs_dir,
                            username=user_dir.name,
                            project=project_dir.name,
                            category=category_dir.name,
                        )

    logger.info("Local plugin loader: %d tools loaded", total)


def reload_plugins() -> dict[str, Any]:
    """Thread-safe full reload of all tool plugins."""
    with _reload_lock:
        _plugins_loaded.clear()
        TOOLS.clear()
        NAME_INDEX.clear()
        RESOURCES.clear()
        PROMPTS.clear()
        PLUGIN_ERRORS.clear()

        # Purge cached tool modules from sys.modules
        _SKIP = (
            "google.",
            "vertexai.",
            "grpc.",
            "proto.",
            "boto",
            "botocore.",
            "flask.",
            "werkzeug.",
            "jinja2.",
            "requests.",
            "urllib3.",
            "pydantic.",
            "fastapi.",
            "uvicorn.",
            "starlette.",
            "anyio.",
            "sniffio.",
            "jwt.",
            "cryptography.",
            "stripe.",
            "httpx.",
            "httpcore.",
            "app.",
        )
        to_del = [
            m
            for m in sys.modules
            if "." in m
            and not m.startswith("_")
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


def start_plugin_loader() -> None:
    """Kick off background plugin loading. Called once at app startup."""

    def _bg() -> None:
        try:
            load_plugins()
        except Exception as exc:
            logger.exception("Plugin loader crashed: %s", exc)
        finally:
            _plugins_loaded.set()

    t = threading.Thread(target=_bg, daemon=True, name="plugin-loader")
    t.start()
