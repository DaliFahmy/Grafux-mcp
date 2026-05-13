"""
app/worker.py — Background worker process.

Runs alongside the web server on Render's worker dyno.
Handles: health monitoring, E2B sandbox cleanup, periodic S3 sync.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)


async def main() -> None:
    from app.config import settings
    from app.cache.redis_client import get_redis_pool
    from app.core.lifecycle.health_monitor import health_monitor
    from app.core.runtime.local_runner import start_plugin_loader, s3_syncer

    logging.basicConfig(level=getattr(logging, settings.log_level.upper()), stream=sys.stderr)
    logger.info("Grafux-mcp worker starting...")

    # Verify Redis
    try:
        redis = get_redis_pool()
        await redis.ping()
        logger.info("Worker Redis connected")
    except Exception as exc:
        logger.warning("Worker Redis not available: %s", exc)

    # Load plugins
    start_plugin_loader()

    # Initial S3 sync
    if settings.aws_access_key_id:
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, s3_syncer.sync_all),
                timeout=120.0,
            )
            logger.info("Worker S3 sync: %s", result)
        except Exception as exc:
            logger.warning("Worker S3 sync failed: %s", exc)

    # Start health monitor
    health_monitor.start()

    # Periodic S3 sync task
    async def _periodic_sync():
        while True:
            await asyncio.sleep(settings.s3_sync_interval)
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, s3_syncer.sync_all)
            except Exception as exc:
                logger.error("Periodic sync error: %s", exc)

    if settings.s3_sync_interval > 0 and settings.aws_access_key_id:
        asyncio.create_task(_periodic_sync())

    logger.info("Grafux-mcp worker running")

    # Keep alive
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Worker shutting down")
        await health_monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
