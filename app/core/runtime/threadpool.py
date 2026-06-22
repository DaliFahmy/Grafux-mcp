"""app/core/runtime/threadpool.py — Shared bounded thread pool for blocking I/O.

Sync work (S3/Supabase sync, file discovery, in-process tool execution) was being
offloaded to ``loop.run_in_executor(None, ...)`` — the default executor, which is
unbounded-ish and shared with everything else, so a burst of S3 syncs could starve
other awaited work. Routing it through one explicitly-sized pool keeps that work
bounded and observable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from app.config import settings

_executor: ThreadPoolExecutor | None = None


def get_threadpool() -> ThreadPoolExecutor:
    """Return the shared bounded thread pool, creating it on first use."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=settings.threadpool_max_workers,
            thread_name_prefix="grafux-mcp-io",
        )
    return _executor


async def run_in_threadpool(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking callable in the shared pool and await its result."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_threadpool(), partial(func, *args, **kwargs))


def shutdown_threadpool() -> None:
    """Shut the pool down (best effort) — call from lifespan shutdown."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None
