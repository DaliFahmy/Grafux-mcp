"""
app/config.py — Service-wide configuration loaded from environment / .env file.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from typing import Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Service ───────────────────────────────────────────────────────────────
    service_name: str = "grafux-mcp"
    environment: str = "development"
    log_level: str = "INFO"
    port: int = 8002

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/grafux_mcp"

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/1"

    # ── Internal service auth ─────────────────────────────────────────────────
    internal_service_key: str = "change_me"
    backend_internal_url: str = "http://localhost:8000"
    orchestrator_internal_url: str = "http://localhost:8001"
    devices_internal_url: str = "http://localhost:8003"

    # ── E2B ───────────────────────────────────────────────────────────────────
    e2b_api_key: Optional[str] = None

    # ── Composio ─────────────────────────────────────────────────────────────
    composio_api_key: Optional[str] = None

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    s3_bucket_name: str = "grafux-user-files"
    s3_sync_interval: int = 600

    # ── GCP ───────────────────────────────────────────────────────────────────
    gcp_service_account_json: Optional[str] = None

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: list[str] = ["*"]

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 60

    # ── Execution ─────────────────────────────────────────────────────────────
    default_tool_timeout: float = 120.0
    max_concurrent_invocations: int = 100

    # ── Computed (not from env) ───────────────────────────────────────────────
    is_production: bool = False
    gcp_credentials_file: Optional[str] = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def setup_derived(self) -> "Settings":
        self.is_production = self.environment == "production"

        # Write GCP service account JSON to a temp file so ADC picks it up
        if self.gcp_service_account_json and not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            try:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, prefix="gcp_sa_"
                )
                tmp.write(self.gcp_service_account_json)
                tmp.flush()
                tmp.close()
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
                self.gcp_credentials_file = tmp.name
            except Exception:
                pass

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
