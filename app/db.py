"""
Database engine, session factory, and shared base.

We use SQLAlchemy 2.x async engine backed by asyncpg.
Connection pool is sized for high concurrency; connections are recycled
to survive Neon's idle-disconnect policy.

The `set_org_context` helper sets the PostgreSQL session-local variable
`app.current_org_id` that the row-level-security policies read.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


# ---------------------------------------------------------------------------
# Engine  – created once at import time; reused for the process lifetime.
# ---------------------------------------------------------------------------
def _build_engine() -> AsyncEngine:
    connect_args: dict = {}
    if "neon.tech" in settings.database_url or "ssl=require" in settings.database_url:
        connect_args["ssl"] = "require"

    if "sqlite" in settings.database_url:
        from sqlalchemy.pool import StaticPool
        return create_async_engine(
            settings.database_url,
            poolclass=StaticPool,
            echo=settings.db_echo,
            connect_args=connect_args,
            json_serializer=_json_serializer,
            json_deserializer=_json_deserializer,
        )

    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,          # detect stale connections before use
        echo=settings.db_echo,
        connect_args=connect_args,
        # JSON serialiser – use orjson for speed when available
        json_serializer=_json_serializer,
        json_deserializer=_json_deserializer,
    )



def _json_serializer(obj: object) -> str:
    try:
        import orjson  # type: ignore
        return orjson.dumps(obj).decode()
    except ImportError:
        import json
        return json.dumps(obj)


def _json_deserializer(s: str) -> object:
    try:
        import orjson  # type: ignore
        return orjson.loads(s)
    except ImportError:
        import json
        return json.loads(s)


engine: AsyncEngine = _build_engine()

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,   # avoid implicit lazy loads after commit
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Session dependency  – use in FastAPI `Depends(get_db)`
# ---------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Tenant context  – call once per request inside the session.
# ---------------------------------------------------------------------------
async def set_org_context(session: AsyncSession, org_id: str) -> None:
    """
    Write `app.current_org_id` into the PostgreSQL session so that
    row-level security policies enforce per-tenant isolation.
    This is a local (transaction-scoped) setting.
    """
    bind = session.bind
    dialect_name = bind.dialect.name if bind else ""
    if dialect_name != "sqlite":
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": org_id},
        )



# ---------------------------------------------------------------------------
# Lifecycle helpers  – call from FastAPI lifespan
# ---------------------------------------------------------------------------
async def startup_db() -> None:
    """Warm up one connection to catch mis-configuration early."""
    async with engine.begin() as conn:
        if conn.dialect.name == "sqlite":
            for table in Base.metadata.tables.values():
                table.schema = None
            await conn.run_sync(Base.metadata.create_all)
        else:
            await conn.execute(text(f"SET search_path TO {settings.db_schema}, public"))


async def shutdown_db() -> None:
    await engine.dispose()
