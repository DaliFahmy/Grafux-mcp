"""
app/api/v1/registry.py — MCP server registration and management endpoints.
"""

from __future__ import annotations

import uuid
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import cache_delete_pattern, get_redis
from app.cache.keys import MCPKeys
from app.core.lifecycle.server_manager import server_manager
from app.core.registry.server_registry import (
    create_server,
    delete_server,
    get_server,
    list_servers,
)
from app.db.database import get_db
from app.models.mcp_server import MCPServer
from app.schemas.registry import RegisterServerRequest, ServerResponse, UpdateServerRequest
from app.security.auth import AuthContext, get_auth_context
from app.security.permissions import assert_server_owned_by_org

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/registry", tags=["registry"])


@router.post("/servers", response_model=ServerResponse, status_code=status.HTTP_201_CREATED)
async def register_server(
    body: RegisterServerRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Register a new MCP server for the organization."""
    server = await create_server(
        org_id=uuid.UUID(auth.org_id),
        data={
            "name": body.name,
            "slug": body.slug,
            "server_type": body.server_type.value,
            "transport": body.transport,
            "endpoint_url": body.endpoint_url,
            "config": body.config,
        },
        db=db,
    )
    return ServerResponse.model_validate(server)


@router.get("/servers", response_model=list[ServerResponse])
async def list_mcp_servers(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List all MCP servers registered for the org."""
    servers = await list_servers(org_id=uuid.UUID(auth.org_id), db=db)
    return [ServerResponse.model_validate(s) for s in servers]


@router.get("/servers/{server_id}", response_model=ServerResponse)
async def get_mcp_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    server = await get_server(server_id, uuid.UUID(auth.org_id), db)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return ServerResponse.model_validate(server)


@router.put("/servers/{server_id}", response_model=ServerResponse)
async def update_mcp_server(
    server_id: uuid.UUID,
    body: UpdateServerRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    server = await get_server(server_id, uuid.UUID(auth.org_id), db)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    if body.name is not None:
        server.name = body.name
    if body.endpoint_url is not None:
        server.endpoint_url = body.endpoint_url
    if body.config is not None:
        server.config = body.config
    if body.status is not None:
        server.status = body.status.value

    await db.commit()
    await db.refresh(server)
    return ServerResponse.model_validate(server)


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    server = await get_server(server_id, uuid.UUID(auth.org_id), db)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Disconnect if active
    try:
        await server_manager.disconnect(server, db, redis)
    except Exception:
        pass

    await delete_server(server, db)

    # Invalidate tool cache
    await cache_delete_pattern(MCPKeys.org_tools(auth.org_id), redis=redis)


@router.post("/servers/{server_id}/connect")
async def connect_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Connect to a registered MCP server and trigger tool discovery.
    """
    server = await get_server(server_id, uuid.UUID(auth.org_id), db)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    try:
        result = await server_manager.connect(server, db, redis)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    # Trigger tool discovery
    from app.services.discovery_service import discovery_service
    discovered = await discovery_service.discover(
        org_id=uuid.UUID(auth.org_id),
        server_id=server_id,
        db=db,
        redis=redis,
    )
    result["tools_discovered"] = discovered
    return result


@router.post("/servers/{server_id}/disconnect")
async def disconnect_server(
    server_id: uuid.UUID,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    server = await get_server(server_id, uuid.UUID(auth.org_id), db)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    await server_manager.disconnect(server, db, redis)
    return {"status": "disconnected", "server_id": str(server_id)}
