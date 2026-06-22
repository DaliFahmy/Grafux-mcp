"""Shared pytest configuration and fixtures.

Environment is pinned to a non-Postgres, non-Redis profile *before* any app module
is imported, so importing the app never tries to reach a real database or cache:
  - DATABASE_URL → in-memory SQLite (engine builds without asyncpg)
  - Redis is forced to the in-process fallback for the whole session
"""

from __future__ import annotations

import os

# Must be set before `app.config` is first imported (it builds Settings at import).
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("INTERNAL_SERVICE_KEY", "test-key")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def redis_fallback():
    """Force the in-process Redis fallback and give each test a clean store."""
    from app.cache import redis_client

    redis_client._use_fallback = True
    redis_client._fallback = None  # rebuilt lazily by get_redis_pool()
    yield
    redis_client._fallback = None
