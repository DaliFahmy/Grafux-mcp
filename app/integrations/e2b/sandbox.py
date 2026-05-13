"""
app/integrations/e2b/sandbox.py — E2B sandbox lifecycle manager.

Manages ephemeral and persistent sandbox sessions for org-scoped code execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.sandbox import SandboxSession

logger = logging.getLogger(__name__)

_DEFAULT_SANDBOX_TIMEOUT = 300  # seconds


class SandboxManager:
    """
    Manages E2B sandbox sessions:
    - Ephemeral: created per invocation, destroyed after completion.
    - Persistent: reused across invocations for an org (stored in DB).
    """

    async def get_or_create_sandbox(
        self,
        org_id: str,
        db: AsyncSession,
        persistent: bool = False,
        template: str = "base",
    ) -> tuple[Any, bool]:
        """
        Return (sandbox, is_new).

        If persistent=True, attempts to reuse an existing active sandbox for the org.
        """
        if not settings.e2b_api_key:
            raise RuntimeError("E2B_API_KEY not configured")

        if persistent:
            existing = await self._get_active_session(org_id, db)
            if existing:
                try:
                    sandbox = await self._reconnect(existing.sandbox_id)
                    return sandbox, False
                except Exception as exc:
                    logger.warning("Failed to reconnect sandbox %s: %s", existing.sandbox_id, exc)
                    await self._expire_session(existing, db)

        sandbox = await self._create(template=template, timeout=_DEFAULT_SANDBOX_TIMEOUT)

        if persistent:
            expires = datetime.now(timezone.utc) + timedelta(seconds=_DEFAULT_SANDBOX_TIMEOUT)
            session = SandboxSession(
                org_id=uuid.UUID(org_id),
                sandbox_id=sandbox.id,
                status="active",
                expires_at=expires,
            )
            db.add(session)
            await db.commit()

        return sandbox, True

    async def destroy_sandbox(self, sandbox: Any, sandbox_id: str | None = None) -> None:
        try:
            await asyncio.get_event_loop().run_in_executor(None, sandbox.close)
        except Exception as exc:
            logger.warning("Failed to close sandbox %s: %s", sandbox_id, exc)

    # ── Private ───────────────────────────────────────────────────────────────

    async def _create(self, template: str = "base", timeout: int = 300) -> Any:
        try:
            from e2b_code_interpreter import AsyncSandbox as E2BSandbox
            sandbox = await E2BSandbox.create(
                api_key=settings.e2b_api_key,
                template=template,
                timeout=timeout,
            )
            logger.info("E2B sandbox created: %s", sandbox.id)
            return sandbox
        except ImportError:
            from e2b import AsyncSandbox as E2BSandbox
            sandbox = await E2BSandbox.create(
                api_key=settings.e2b_api_key,
                timeout=timeout,
            )
            logger.info("E2B sandbox created (basic): %s", sandbox.id)
            return sandbox

    async def _reconnect(self, sandbox_id: str) -> Any:
        try:
            from e2b_code_interpreter import AsyncSandbox as E2BSandbox
            return await E2BSandbox.reconnect(sandbox_id, api_key=settings.e2b_api_key)
        except ImportError:
            from e2b import AsyncSandbox as E2BSandbox
            return await E2BSandbox.reconnect(sandbox_id, api_key=settings.e2b_api_key)

    async def _get_active_session(
        self, org_id: str, db: AsyncSession
    ) -> SandboxSession | None:
        result = await db.execute(
            select(SandboxSession).where(
                SandboxSession.org_id == uuid.UUID(org_id),
                SandboxSession.status == "active",
                SandboxSession.expires_at > datetime.now(timezone.utc),
            ).order_by(SandboxSession.created_at.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def _expire_session(self, session: SandboxSession, db: AsyncSession) -> None:
        session.status = "expired"
        await db.commit()


sandbox_manager = SandboxManager()
