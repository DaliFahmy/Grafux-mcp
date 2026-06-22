"""Default-value factories shared by ORM models, services and schemas.

A single home for id/timestamp generation so call sites stop carrying their own
private copies of ``uuid.uuid4`` / ``datetime.now(timezone.utc)``. Mirrors the
sibling service's ``app/shared/factories.py`` (see memory ``orchestrator-perf-refactor``),
adapted to Grafux-mcp's conventions: UUID columns store ``uuid.UUID`` and
timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def generate_uuid() -> uuid.UUID:
    """Return a new random UUID4 (as ``uuid.UUID``, matching the model columns)."""
    return uuid.uuid4()


def utcnow() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)
