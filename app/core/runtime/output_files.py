"""Read tool output port files from the MCP data directory."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_PDF_EXTS = {".pdf"}

# Grafux-mcp repo root (app/core/runtime/output_files.py -> parents[3])
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def project_root() -> Path:
    return _PROJECT_ROOT


def _normalize_rel_path(rel: str) -> str:
    return rel.replace("\\", "/").lstrip("/")


def infer_output_port_paths(arguments: dict[str, Any]) -> list[str]:
    """Derive output port file paths from input port paths in tool arguments."""
    tool_base: str | None = None
    for val in arguments.values():
        if not isinstance(val, str):
            continue
        norm = _normalize_rel_path(val)
        if "/inputs/" in norm:
            tool_base = norm.split("/inputs/", 1)[0]
            break
        if "/tools/" in norm and "/outputs/" not in norm:
            # e.g. data/user/proj/tools/cat/tool/inputs/a.txt already handled
            pass
    if not tool_base:
        return []

    out_dir = _PROJECT_ROOT / tool_base / "outputs"
    if not out_dir.is_dir():
        return []

    paths: list[str] = []
    for txt in sorted(out_dir.glob("*.txt")):
        rel = txt.relative_to(_PROJECT_ROOT).as_posix()
        paths.append(rel)
    return paths


def read_output_files(paths: list[str]) -> list[dict[str, Any]]:
    """Read UTF-8 text (and referenced binary) for each relative path under data/."""
    result: list[dict[str, Any]] = []
    for rel in paths:
        rel = _normalize_rel_path(rel)
        local = _PROJECT_ROOT / rel
        if not local.exists():
            continue
        try:
            text = local.read_text(encoding="utf-8")
            result.append({"path": rel, "content": text})
            candidate = local.parent / text.strip()
            ext = Path(text.strip()).suffix.lower()
            if (ext in _IMAGE_EXTS or ext in _PDF_EXTS) and candidate.exists():
                data = candidate.read_bytes()
                bin_rel = rel.rsplit("/", 1)[0] + "/" + text.strip()
                result.append({
                    "path": bin_rel,
                    "content": base64.b64encode(data).decode("ascii"),
                    "encoding": "base64",
                })
        except Exception:
            pass
    return result
