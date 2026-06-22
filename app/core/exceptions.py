"""app/core/exceptions.py — Typed exception hierarchy for the MCP runtime.

A small, explicit hierarchy so callers can distinguish *why* an invocation failed
(bad routing, a dead remote, a plugin that won't load) instead of catching bare
``Exception`` everywhere. The execution layer records these as structured failures.
"""

from __future__ import annotations


class MCPError(Exception):
    """Base class for all Grafux-mcp runtime errors."""


class RoutingError(MCPError):
    """A tool invocation could not be routed to a backend.

    Raised for unknown server types or missing routing configuration
    (e.g. a remote server with no ``endpoint_url``).
    """


class ExecutionError(MCPError):
    """A tool ran but failed, or its backend raised while executing it."""


class RemoteCallError(ExecutionError):
    """A call to a remote/Composio/device backend failed (transport or protocol)."""


class PluginLoadError(MCPError):
    """A local plugin could not be discovered, imported, or registered."""
