"""app/core/runtime/tool_executor.py — In-process tool lookup and execution.

Resolves a tool name (namespaced → direct → display-name scan), supports the Qt
app's live "editable code" path (running a tool straight from a source file), runs
the tool synchronously, and shapes the result into the MCP ``content`` envelope —
attaching diagnostics on failure.
"""

from __future__ import annotations

import traceback
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.errors import tool_error
from app.core.runtime.diagnostics import suggest_fix
from app.core.runtime.plugin_loader import (
    NAME_INDEX,
    TOOLS,
    _plugins_loaded,
    register_prompt,
    register_resource,
)


def infer_category_from_arguments(arguments: dict[str, Any]) -> str | None:
    """Extract tools/{category}/{tool}/ from port paths in the request body."""
    for val in arguments.values():
        if not isinstance(val, str):
            continue
        norm = val.replace("\\", "/")
        marker = "/tools/"
        if marker not in norm:
            continue
        rest = norm.split(marker, 1)[1]
        parts = rest.split("/")
        if len(parts) >= 2:
            return parts[0]
    return None


# ── Editable-code helpers (live tool editing in Qt app) ──────────────────────


def _resolve_editable_path(arguments: dict[str, Any]) -> Path | None:
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
    tool_metadata: dict[str, Any], arguments: dict[str, Any]
) -> Callable | None:
    code_path = _resolve_editable_path(arguments)
    if not code_path:
        return None
    source = code_path.read_text(encoding="utf-8")
    compiled = compile(source, str(code_path), "exec")
    captured: dict[str, Any] = {}

    def _capture_rt(name: str, description: str, input_schema: dict):
        def inner(func: Callable) -> Callable:
            captured.update(
                {
                    "func": func,
                    "name": name,
                    "description": description,
                    "inputSchema": input_schema,
                }
            )
            return func

        return inner

    exec(
        compiled,
        {
            "__file__": str(code_path),
            "__name__": f"_editable_{tool_metadata.get('name', 'tool')}",
            "register_tool": _capture_rt,
            "register_resource": register_resource,
            "register_prompt": register_prompt,
        },
    )
    func = captured.get("func")
    if not callable(func):
        raise ValueError(
            "Tool code is not in the required MCP format: it must define a function "
            "decorated with @register_tool(name=..., description=..., input_schema=...). "
            "Regenerate the tool to produce a correctly-formatted file."
        )
    return func


# ── Tool dispatcher ───────────────────────────────────────────────────────────


def call_tool_direct(
    tool_name: str,
    arguments: dict[str, Any],
    caller_username: str | None = None,
    caller_project: str | None = None,
    caller_category: str | None = None,
) -> dict[str, Any]:
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
            # Resolve by display name via the secondary index (O(1) lookup of the
            # candidate keys) instead of scanning the whole TOOLS registry.
            for k in NAME_INDEX.get(tool_name, []):
                info = TOOLS.get(k)
                if not info:
                    continue
                if caller_username and caller_project:
                    if (
                        info.get("username") == caller_username
                        and info.get("project") == caller_project
                    ):
                        if caller_category and info.get("category") != caller_category:
                            continue
                        tool_key, tool_meta = k, info
                        break
                else:
                    tool_key, tool_meta = k, info
                    break

    # Fallback: editable-code (live-chat tools without references/*.py)
    if not tool_key:
        editable_error: str | None = None
        fb_func = None
        if _resolve_editable_path(arguments):
            try:
                fb_func = _load_editable_func({"name": tool_name}, arguments)
            except Exception as exc:
                # Editable code exists but is malformed (e.g. missing @register_tool).
                # Surface the descriptive reason rather than a misleading "Unknown tool".
                editable_error = str(exc)
        if fb_func:
            try:
                result = fb_func(arguments)
                if not isinstance(result, dict) or "content" not in result:
                    result = {"content": [{"type": "text", "text": str(result)}]}
                return result
            except Exception as exc:
                return tool_error(
                    traceback.format_exc(),
                    diagnostics={"improvements": suggest_fix(exc)},
                )
        if editable_error:
            return tool_error(
                editable_error,
                diagnostics={
                    "improvements": "Regenerate the tool so its code includes the @register_tool decorator."
                },
            )
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
        caught_warns: list[str] = []
        with warnings.catch_warnings(record=True) as cw:
            warnings.simplefilter("always")
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
        lines = [
            f"{Path(se.filename).name if se.filename else 'code'}:{se.lineno}: SyntaxError: {se.msg}"
        ]
        if se.text:
            lines += [f"    {se.text.rstrip()}", f"    {pointer}"]
        lines.append(tb)
        return tool_error(
            "\n".join(lines),
            diagnostics={"improvements": "Fix syntax error at indicated line."},
        )

    except Exception as exc:
        return tool_error(
            traceback.format_exc(),
            diagnostics={"improvements": suggest_fix(exc)},
        )


def list_local_tools() -> list[dict[str, Any]]:
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
