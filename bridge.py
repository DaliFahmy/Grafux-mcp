"""
bridge.py — In-process MCP tool registry bridge.

MCPBridge wraps the registry and executor layers and exposes a single, clean
interface used by the route handlers.  The module-level ``bridge`` singleton
is created here so that all importers share one instance.
"""

import sys
import threading
import traceback
from typing import Any, Dict

from registry import (
    load_plugins,
    reload_plugins,
    TOOLS,
    RESOURCES,
    PROMPTS,
    _plugins_loaded,
)
from executor import _call_tool_direct


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

    # ── Public API (called from async route handlers via run_in_executor) ─────

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


# ── Module-level singleton ────────────────────────────────────────────────────

bridge = MCPBridge()
