"""
app/services/audit_service.py — Execution audit logging.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.streaming.event_bus import event_bus

logger = logging.getLogger(__name__)


class AuditService:
    async def log(
        self,
        invocation_id: str,
        level: str,
        message: str,
        redis: Any = None,
    ) -> None:
        """Publish an audit log entry for an invocation."""
        await event_bus.publish_log(invocation_id, level, message)
        logger.info("[audit] invocation=%s level=%s msg=%s", invocation_id, level, message)

    async def log_invocation_start(self, invocation_id: str, tool_name: str, redis: Any = None) -> None:
        await self.log(invocation_id, "info", f"Invocation started: {tool_name}", redis=redis)

    async def log_invocation_end(self, invocation_id: str, status: str, redis: Any = None) -> None:
        await self.log(invocation_id, "info", f"Invocation ended: {status}", redis=redis)


audit_service = AuditService()
