"""app/core/runtime/diagnostics.py — Human-readable hints for tool failures.

Maps a raised exception to a short, actionable suggestion that is surfaced back
to the tool author in the invocation result's ``diagnostics.improvements`` field.
"""

from __future__ import annotations


def suggest_fix(exc: Exception) -> str:
    """Return a one-line remediation hint for a tool execution exception."""
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


# Backwards-compatible alias (the previous private name).
_suggest_fix = suggest_fix
