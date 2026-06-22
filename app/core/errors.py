"""app/core/errors.py — Helpers for the MCP tool-result envelope.

The ``{"content": [{"type": "text", "text": ...}], "isError": ...}`` shape was
being hand-built in a dozen places (routes, gateway, runtime, the global handler),
which drifts easily. These helpers are the single source of truth for it.
"""

from __future__ import annotations

from typing import Any


def tool_text(text: str) -> dict[str, Any]:
    """A successful MCP text result."""
    return {"content": [{"type": "text", "text": text}]}


def tool_error(message: str, *, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    """An error MCP result (``isError=True``), optionally carrying diagnostics."""
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result
