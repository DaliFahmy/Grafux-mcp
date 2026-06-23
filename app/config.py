"""
app/config.py — Service-wide configuration loaded from environment / .env file.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache

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
    # Pool sized to track max_concurrent_invocations; echo is opt-in (never tied to
    # environment — leaving it on in dev/staging logs every statement).
    db_echo: bool = False
    db_pool_size: int = 20
    db_max_overflow: int = 20

    # ── Outbound HTTP (pooled client — see app/core/http_client.py) ───────────
    http_default_timeout: float = 30.0
    http_max_connections: int = 100
    http_max_keepalive_connections: int = 20

    # ── Remote MCP connection pool (see app/core/protocol/connection_pool.py) ──
    # Live MCP-SDK sessions are reused per endpoint instead of being rebuilt on
    # every invocation. Idle sessions are closed after remote_pool_idle_seconds;
    # the pool holds at most remote_pool_max_size distinct endpoints.
    remote_pool_max_size: int = 50
    remote_pool_idle_seconds: float = 300.0

    # ── Remote MCP HTTP fallback (used when the mcp SDK isn't installed) ───────
    remote_probe_timeout: float = 10.0
    remote_call_timeout: float = 60.0

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/1"

    # ── Internal service auth ─────────────────────────────────────────────────
    internal_service_key: str = "change_me"
    backend_internal_url: str = "http://localhost:8000"
    orchestrator_internal_url: str = "http://localhost:8001"
    devices_internal_url: str = "http://localhost:8003"

    # ── OpenAI (used by AI-powered generated tools, which read these from env) ──
    # Model overrides are opt-in: when unset, generated tools keep their own built-in
    # default model, so we never silently change a model that already works in a deployment.
    openai_api_key: str | None = None
    openai_text_model: str | None = None
    openai_image_model: str | None = None
    openai_video_model: str | None = None

    # ── E2B ───────────────────────────────────────────────────────────────────
    e2b_api_key: str | None = None

    # ── Composio ─────────────────────────────────────────────────────────────
    composio_api_key: str | None = None

    # ── AWS S3 ────────────────────────────────────────────────────────────────
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    s3_bucket_name: str = "grafux-user-files"
    s3_sync_interval: int = 600

    # ── Supabase (tool-file sync — primary storage for client uploads) ────────
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = "grafux-projects"

    # ── GCP ───────────────────────────────────────────────────────────────────
    gcp_service_account_json: str | None = None

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: list[str] = ["*"]

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 60

    # ── Execution ─────────────────────────────────────────────────────────────
    default_tool_timeout: float = 120.0
    max_concurrent_invocations: int = 100
    # Bounded pool for blocking I/O (S3/Supabase sync, file reads, in-process tool
    # calls) so heavy sync work can't exhaust the default unbounded executor.
    threadpool_max_workers: int = 8

    # ── Computed (not from env) ───────────────────────────────────────────────
    is_production: bool = False
    gcp_credentials_file: str | None = None

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def setup_derived(self) -> Settings:
        self.is_production = self.environment == "production"

        # Fail fast on insecure production config rather than booting a service
        # that trusts the shipped default service key. Dev/test are unaffected.
        if self.is_production and self.internal_service_key == "change_me":
            raise ValueError(
                "internal_service_key is still the default 'change_me' in a "
                "production environment — set INTERNAL_SERVICE_KEY before boot."
            )

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

        # Generated AI tools run in-process via call_tool_direct and read their OpenAI
        # credentials/model straight from os.environ. Export the configured values under
        # every name the generated code probes, without clobbering a real env var that is
        # already set (so the process environment always wins over the .env file).
        def _export(name: str, value: str | None) -> None:
            if value and not os.environ.get(name):
                os.environ[name] = value

        if self.openai_api_key:
            _export("OPENAI_API_KEY", self.openai_api_key)
            _export("GARAGE_OPENAI_API_KEY", self.openai_api_key)
            _export("GRAFUX_GPT_KEY", self.openai_api_key)
        _export("GARAGE_OPENAI_TEXT_MODEL", self.openai_text_model)
        _export("GARAGE_OPENAI_IMAGE_MODEL", self.openai_image_model)
        _export("GARAGE_OPENAI_VIDEO_MODEL", self.openai_video_model)

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
