"""
app/cache/keys.py — Redis key naming conventions for Grafux-mcp.

All keys follow:   mcp:{scope}:{entity}:{id}:{field}
"""

from __future__ import annotations


class MCPKeys:
    """Centralised Redis key factory."""

    # ── Tool list cache ───────────────────────────────────────────────────────
    @staticmethod
    def org_tools(org_id: str) -> str:
        return f"mcp:{org_id}:tools"

    @staticmethod
    def project_tools(org_id: str, project_id: str) -> str:
        return f"mcp:{org_id}:project:{project_id}:tools"

    # ── Server health ─────────────────────────────────────────────────────────
    @staticmethod
    def server_health(org_id: str, server_id: str) -> str:
        return f"mcp:{org_id}:server:{server_id}:health"

    @staticmethod
    def server_status_pattern(org_id: str) -> str:
        return f"mcp:{org_id}:server:*:health"

    # ── Invocation events (pub/sub channel) ───────────────────────────────────
    @staticmethod
    def invocation_events(invocation_id: str) -> str:
        return f"mcp:invocation:{invocation_id}:events"

    @staticmethod
    def invocation_status(invocation_id: str) -> str:
        return f"mcp:invocation:{invocation_id}:status"

    # ── Session ───────────────────────────────────────────────────────────────
    @staticmethod
    def session(session_id: str) -> str:
        return f"mcp:session:{session_id}"

    # ── Rate limiting ─────────────────────────────────────────────────────────
    @staticmethod
    def rate_limit(org_id: str, endpoint: str) -> str:
        return f"mcp:rate:{org_id}:{endpoint}"

    # ── Capability cache ──────────────────────────────────────────────────────
    @staticmethod
    def server_capabilities(server_id: str) -> str:
        return f"mcp:capabilities:{server_id}"

    # ── Lock keys ─────────────────────────────────────────────────────────────
    @staticmethod
    def server_connect_lock(server_id: str) -> str:
        return f"mcp:lock:connect:{server_id}"


# TTLs (seconds)
TTL_TOOLS = 300         # 5 minutes
TTL_HEALTH = 30         # 30 seconds
TTL_SESSION = 3600      # 1 hour
TTL_CAPABILITIES = 600  # 10 minutes
TTL_LOCK = 30           # 30 seconds
