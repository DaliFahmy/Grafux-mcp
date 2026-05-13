"""
app/core/registry/tool_registry.py — Tool registry operations.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_tool import MCPTool, ToolStatus


async def upsert_tools(
    server_id: uuid.UUID,
    org_id: uuid.UUID,
    tools: list[dict[str, Any]],
    db: AsyncSession,
) -> int:
    """
    Upsert discovered tools for a server. Returns the count of new/updated tools.
    Existing tools not in the new list are marked unavailable.
    """
    if not tools:
        return 0

    incoming_names = {t["name"] for t in tools}

    # Mark missing tools unavailable
    await db.execute(
        select(MCPTool)
        .where(
            MCPTool.server_id == server_id,
            MCPTool.name.notin_(incoming_names),
        )
    )
    # Upsert via INSERT ... ON CONFLICT
    stmt = insert(MCPTool).values(
        [
            {
                "server_id": server_id,
                "org_id": org_id,
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("input_schema") or t.get("inputSchema") or {},
                "tool_type": t.get("tool_type", "generic"),
                "category": t.get("category"),
                "status": ToolStatus.AVAILABLE,
            }
            for t in tools
        ]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["server_id", "org_id", "name"],
        set_={
            "description": stmt.excluded.description,
            "input_schema": stmt.excluded.input_schema,
            "status": ToolStatus.AVAILABLE,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(tools)


async def get_tools_for_server(
    server_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> list[MCPTool]:
    result = await db.execute(
        select(MCPTool).where(
            MCPTool.server_id == server_id,
            MCPTool.org_id == org_id,
        )
    )
    return list(result.scalars().all())
