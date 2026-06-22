"""Lightweight test doubles.

The ORM models use PostgreSQL-only column types (``JSONB``, ``UUID``) that don't
render on SQLite, so spinning up a real test database is impractical here. These
fakes stand in for an ``AsyncSession`` and let us drive the service layer while
asserting *how many* queries it issues — which is exactly the behaviour the A1
refactor is about (one routing lookup, not four).
"""

from __future__ import annotations

import uuid
from typing import Any


class FakeResult:
    """Stand-in for a SQLAlchemy Result with a single scalar value."""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value


class FakeSession:
    """Minimal async session double.

    ``execute`` pops from a preloaded list of FakeResult objects (returning an
    empty result once exhausted) and counts how many times it was called.
    ``refresh`` assigns an id the way a real INSERT would.
    """

    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self._results = list(results or [])
        self.execute_count = 0
        self.commit_count = 0
        self.added: list[Any] = []

    async def execute(self, *args: Any, **kwargs: Any) -> FakeResult:
        self.execute_count += 1
        return self._results.pop(0) if self._results else FakeResult(None)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def refresh(self, obj: Any) -> None:
        if getattr(obj, "id", None) is None:
            try:
                obj.id = uuid.uuid4()
            except Exception:
                pass

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass
