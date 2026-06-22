"""R6 — production refuses to boot with the default internal service key."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_rejects_default_service_key():
    with pytest.raises(ValidationError):
        Settings(environment="production", internal_service_key="change_me")


def test_production_accepts_real_service_key():
    settings = Settings(environment="production", internal_service_key="a-real-secret")
    assert settings.is_production is True


def test_development_allows_default_key():
    settings = Settings(environment="development", internal_service_key="change_me")
    assert settings.is_production is False
