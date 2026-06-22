"""A1 — tool→server routing resolves in a single query (was four)."""

from __future__ import annotations

import uuid

from app.models.mcp_server import MCPServer, ServerType
from app.models.mcp_tool import MCPTool
from app.services.execution_service import RoutingInfo, execution_service
from tests._fakes import FakeResult, FakeSession


async def test_no_tool_id_is_local_and_hits_no_db():
    db = FakeSession()
    info = await execution_service._resolve_tool_routing(None, db)

    assert info.server_type == ServerType.LOCAL_PLUGIN
    assert db.execute_count == 0


async def test_resolves_server_in_single_query():
    org_id = uuid.uuid4()
    server = MCPServer(
        name="remote",
        slug="remote",
        org_id=org_id,
        server_type=ServerType.REMOTE_HTTP,
        endpoint_url="https://example.test/mcp",
        config={"token": "abc"},
    )
    tool = MCPTool(
        name="do_thing",
        description="",
        input_schema={},
        org_id=org_id,
        server_id=uuid.uuid4(),
    )
    tool.server = server  # what joinedload would populate

    db = FakeSession([FakeResult(tool)])
    info = await execution_service._resolve_tool_routing(uuid.uuid4(), db)

    assert info == RoutingInfo(
        ServerType.REMOTE_HTTP, "https://example.test/mcp", {"token": "abc"}
    )
    assert db.execute_count == 1  # the whole point of A1


async def test_missing_tool_falls_back_to_local():
    db = FakeSession([FakeResult(None)])
    info = await execution_service._resolve_tool_routing(uuid.uuid4(), db)

    assert info.server_type == ServerType.LOCAL_PLUGIN
    assert info.endpoint_url is None
