"""
app/core/runtime/local_runner.py — Backward-compatible facade for the local
in-process tool runtime.

The runtime was split into focused modules; this module preserves the original
public surface so existing imports (``from app.core.runtime.local_runner import
...``) keep working unchanged:

  - plugin_loader  — registries, ``@register_tool`` discovery, (re)loading
  - tool_executor  — ``call_tool_direct`` / ``list_local_tools`` + editable code
  - cloud_syncer   — ``s3_syncer`` (S3 + Supabase ETag-aware sync)
  - diagnostics    — failure-hint generation
"""

from __future__ import annotations

from app.core.runtime.cloud_syncer import S3_AVAILABLE, _S3Syncer, s3_syncer
from app.core.runtime.diagnostics import _suggest_fix, suggest_fix
from app.core.runtime.plugin_loader import (
    PLUGIN_ERRORS,
    PROMPTS,
    RESOURCES,
    TOOLS,
    _get_data_root,
    _get_tools_root,
    _load_directory,
    _plugins_loaded,
    _reload_lock,
    load_plugins,
    register_prompt,
    register_resource,
    register_tool,
    reload_plugins,
    start_plugin_loader,
)
from app.core.runtime.tool_executor import (
    _load_editable_func,
    _resolve_editable_path,
    call_tool_direct,
    infer_category_from_arguments,
    list_local_tools,
)

__all__ = [
    # plugin_loader
    "TOOLS",
    "RESOURCES",
    "PROMPTS",
    "PLUGIN_ERRORS",
    "_plugins_loaded",
    "_reload_lock",
    "register_tool",
    "register_resource",
    "register_prompt",
    "load_plugins",
    "reload_plugins",
    "start_plugin_loader",
    "_load_directory",
    "_get_data_root",
    "_get_tools_root",
    # tool_executor
    "call_tool_direct",
    "list_local_tools",
    "infer_category_from_arguments",
    "_resolve_editable_path",
    "_load_editable_func",
    # cloud_syncer
    "s3_syncer",
    "_S3Syncer",
    "S3_AVAILABLE",
    # diagnostics
    "suggest_fix",
    "_suggest_fix",
]
