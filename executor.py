"""
executor.py — Tool execution engine.

Handles tool key resolution, authorization, editable-code dispatch,
Python-warning capture, and structured error formatting.
"""

import io
import sys
import traceback
import warnings as _warnings_module
from pathlib import Path
from typing import Any, Dict, List

from config import _tool_call_logs, _tl_log_warn
from registry import TOOLS, _plugins_loaded, _resolve_tool_code_path, _load_editable_code_func


# ── Error hint generator ──────────────────────────────────────────────────────

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
            "Check the import name for typos and ensure the package is installed "
            "in the server's environment."
        )
    if exc_type == "NameError":
        return (
            "A name is used before it is defined.\n"
            "Check for typos in variable/function names and ensure everything "
            "is defined before use."
        )
    if exc_type == "AttributeError":
        return (
            "An attribute or method does not exist on the object.\n"
            "Verify the object type and check the attribute name for typos."
        )
    if exc_type == "TypeError":
        return (
            "A function received an argument of the wrong type or the wrong number "
            "of arguments.\n"
            "Check function signatures, argument types, and call sites."
        )
    if exc_type == "KeyError":
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
        return (
            "The operation timed out. Consider increasing the timeout or "
            "optimising the slow operation."
        )
    if any(w in msg_lower for w in ("connection", "refused", "unreachable", "network")):
        return (
            "A network connection failed. Check connectivity and that the "
            "remote service is available."
        )
    if exc_type == "RecursionError":
        return (
            "Maximum recursion depth exceeded. Check for infinite loops or "
            "missing base cases in recursive functions."
        )
    if exc_type == "MemoryError":
        return "Out of memory. Process smaller batches or reduce data size."
    if exc_type == "ZeroDivisionError":
        return "Division by zero. Add a guard: `if denominator != 0:` before dividing."
    if exc_type == "IndexError":
        return (
            "List index out of range. Check list length before indexing or use "
            "`for item in list:` instead."
        )
    if exc_type == "StopIteration":
        return "An iterator was exhausted unexpectedly. Check loop logic and iterator usage."

    return (
        f"Tool '{tool_name}' raised {exc_type}.\n"
        "Review the traceback to identify the failing line and fix the root cause."
    )


# ── Tool dispatcher ───────────────────────────────────────────────────────────

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
                print(f"[ERROR] Fallback execution for '{tool_name}' failed: {_re}",
                      file=sys.stderr)
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
