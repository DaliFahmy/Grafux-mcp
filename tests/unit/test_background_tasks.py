"""R1 — background invocation tasks are tracked, observed, and drained."""

from __future__ import annotations

import asyncio
import logging

from app.services.execution_service import ExecutionService


async def test_completed_task_is_dropped_from_tracking():
    svc = ExecutionService()

    async def _work():
        await asyncio.sleep(0)

    task = asyncio.create_task(_work())
    svc._background_tasks.add(task)
    task.add_done_callback(svc._on_background_done)

    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert task not in svc._background_tasks


async def test_failed_task_is_logged_and_dropped(caplog):
    svc = ExecutionService()

    async def _boom():
        raise RuntimeError("kaboom")

    task = asyncio.create_task(_boom())
    svc._background_tasks.add(task)
    task.add_done_callback(svc._on_background_done)

    with caplog.at_level(logging.ERROR):
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert task not in svc._background_tasks
    assert any("crashed" in r.message for r in caplog.records)


async def test_shutdown_cancels_inflight_tasks():
    svc = ExecutionService()

    async def _long():
        await asyncio.sleep(100)

    task = asyncio.create_task(_long())
    svc._background_tasks.add(task)
    task.add_done_callback(svc._on_background_done)

    await svc.shutdown()
    assert task.cancelled()
    assert not svc._background_tasks
