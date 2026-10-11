"""
OpsTriage – Application configuration.

All settings are read from environment variables (or a .env file).
Pydantic-settings validates types and raises a descriptive error at
startup rather than at the first bad DB call.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "OpsTriage"
    app_version: str = "0.1.0"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(
        ...,
        description=(
            "asyncpg-compatible PostgreSQL URL, e.g. "
            "postgresql+asyncpg://user:pass@host/db?ssl=require"
        ),
    )
    db_schema: str = "opstriage"
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: float = 30.0
    db_pool_recycle: int = 1800  # seconds
    db_echo: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_default_ttl: int = 60          # seconds
    redis_findings_ttl: int = 10         # low – near-real-time for triage UI
    redis_reports_ttl: int = 300

    # ── Auth / JWT ────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # ── CORS ─────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:4173"]

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_limit_per_minute: int = 300     # per-IP/per-org

    # ── Workers / Concurrency ─────────────────────────────────────────────────
    worker_concurrency: int = 4          # background task parallelism
    ingest_batch_size: int = 500

    # ── AI / Gemini Service ───────────────────────────────────────────────────
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    ai_enabled: bool = True
    ai_timeout_seconds: float = 15.0
    ai_max_output_tokens: int = 1500

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("database_url", mode="before")
    @classmethod
    def _coerce_asyncpg(cls, v: str) -> str:
        """
        Accept both plain postgresql:// (Neon style) and
        postgresql+asyncpg:// (SQLAlchemy style).
        Cleans up Neon query parameters like sslmode/channel_binding incompatible with asyncpg.
        """
        if v.startswith("postgresql://") or v.startswith("postgres://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1).replace(
                "postgres://", "postgresql+asyncpg://", 1
            )
        v = v.replace("sslmode=require", "ssl=require").replace("sslmode=prefer", "ssl=prefer")
        v = v.replace("&channel_binding=require", "").replace("channel_binding=require&", "")
        v = v.replace("?channel_binding=require", "?").rstrip("?")
        return v



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
