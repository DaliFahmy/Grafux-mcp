"""Background async invocations don't re-SELECT the invocation they just created.

``_run_background`` now carries the needed fields in an ``_InvocationRef`` and opens a
session only for the status writes — so a successful run issues exactly two
``execute`` calls (RUNNING update + DONE update), not three (the old extra SELECT).
"""

from __future__ import annotations

import uuid

import app.db.database as db_mod
import app.services.execution_service as es_mod
from app.models.mcp_server import ServerType
from app.services.execution_service import RoutingInfo, _InvocationRef, execution_service
from tests._fakes import FakeSession


class _SessionCtx:
    """Async-context-manager wrapper around a FakeSession (mimics AsyncSessionLocal())."""

    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *exc) -> bool:
        return False


async def test_background_run_issues_no_extra_select(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", lambda: _SessionCtx(fake))

    async def fake_route(**kwargs):
        return {"content": [{"type": "text", "text": "ok"}]}

    monkeypatch.setattr(es_mod.execution_router, "route", fake_route)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(es_mod.event_bus, "publish", _noop)

    ref = _InvocationRef(id=uuid.uuid4(), tool_name="t", org_id=str(uuid.uuid4()), project_id=None)
    await execution_service._run_background(
        ref=ref,
        arguments={},
        routing=RoutingInfo(ServerType.LOCAL_PLUGIN),
        username=None,
        project=None,
        category=None,
        timeout=5.0,
        redis=None,
    )

    # RUNNING update + DONE update only — the old code added a leading SELECT.
    assert fake.execute_count == 2
