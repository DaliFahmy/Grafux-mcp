"""Integration smoke — invoke() drives resolve → create → route → publish.

Uses a fake session (real DB tables can't be built on SQLite here), so this
asserts the orchestration wiring and event publishing rather than persistence.
"""

from __future__ import annotations

import uuid

from app.core.runtime import plugin_loader
from app.core.streaming.event_bus import event_bus
from app.services.execution_service import execution_service
from tests._fakes import FakeSession


async def test_invoke_runs_local_tool_and_publishes_result(monkeypatch):
    # Register a local tool and mark the loader ready so call_tool_direct won't block.
    @plugin_loader.register_tool("echo_flow_test", "echo", {"type": "object"})
    def _echo(args):
        return {"content": [{"type": "text", "text": f"hi {args.get('x')}"}]}

    plugin_loader._plugins_loaded.set()

    published: list[tuple] = []

    async def _capture(invocation_id, event_type, data, *, is_final=False):
        published.append((event_type, is_final))

    monkeypatch.setattr(event_bus, "publish", _capture)

    db = FakeSession()
    result = await execution_service.invoke(
        tool_name="echo_flow_test",
        arguments={"x": 42},
        org_id=str(uuid.uuid4()),
        db=db,
        redis=None,
    )

    assert result["content"][0]["text"] == "hi 42"
    # A final "result" event was emitted; status writes were committed.
    assert any(et == "result" and final for (et, final) in published)
    assert db.commit_count >= 2  # create + mark-done at minimum
