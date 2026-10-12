"""
Shared pytest fixtures for OpsTriage backend tests.

Uses:
  - pytest-asyncio in auto mode
  - httpx AsyncClient against the FastAPI app (no real server needed)
  - SQLite in-memory for fast, isolated DB per test run
  - Redis mocked via fakeredis

The test DB is created fresh for every test session by running the ORM
Base.metadata.create_all() – this matches the schema structure without
needing a running Postgres.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ── Configure test settings before any app imports ───────────────────────────
import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.db import Base, get_db
from app.main import app as _app
from app import cache as _cache

# ── In-memory SQLite engine ───────────────────────────────────────────────────
TEST_ENGINE = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
TEST_SESSION_FACTORY = async_sessionmaker(TEST_ENGINE, expire_on_commit=False)

ORG_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


# ── Session-scoped DB setup ───────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the entire test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """Create all tables once for the session."""
    async with TEST_ENGINE.begin() as conn:
        # SQLite doesn't support schemas, so we monkey-patch table args
        for table in Base.metadata.tables.values():
            table.schema = None
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with TEST_ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ── Per-test DB session ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    # Endpoints commit their own transactions, so rollback alone cannot isolate
    # tests against the session-scoped in-memory database. Clear it before each
    # test to prevent one test's records from changing another test's baseline.
    async with TEST_ENGINE.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    async with TEST_SESSION_FACTORY() as session:
        yield session
        await session.rollback()  # isolate tests


# ── Mock Redis (no actual Redis needed) ──────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_redis():
    """Replace Redis client with a simple in-memory dict."""
    store: dict[str, Any] = {}

    async def fake_get(key: str):
        return store.get(key)

    async def fake_set(key: str, value: Any, ex: int | None = None):
        store[key] = value

    async def fake_delete(key: str):
        store.pop(key, None)

    async def fake_ping():
        return True

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=fake_get)
    mock_client.set = AsyncMock(side_effect=fake_set)
    mock_client.delete = AsyncMock(side_effect=fake_delete)
    mock_client.ping = AsyncMock(side_effect=fake_ping)
    mock_client.scan_iter = MagicMock(return_value=aiter([]))
    mock_client.aclose = AsyncMock()

    with patch.object(_cache, "_client", mock_client):
        yield mock_client


async def aiter(items):
    for item in items:
        yield item


# ── Override DB dependency ────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """
    HTTPX async client wired to the FastAPI app,
    with the DB dependency overridden to use the test session.
    """
    async def override_get_db():
        yield db_session

    # Bypass the RLS set_org_context (SQLite doesn't support it)
    with patch("app.db.set_org_context", new=AsyncMock()):
        _app.dependency_overrides[get_db] = override_get_db
        async with AsyncClient(
            transport=ASGITransport(app=_app),
            base_url="http://testserver",
        ) as ac:
            yield ac
        _app.dependency_overrides.clear()
