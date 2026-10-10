"""
Redis cache client – async, connection-pooled.

Exposes a thin helper layer on top of redis-py's async client:
  - get / set / delete with automatic JSON (de)serialisation
  - cache_or_fetch:  cache-aside pattern in one call
  - invalidate_prefix: bulk delete by key prefix (scan-based)

The client is created once per process and shared via `get_cache()`.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urlsplit

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")

_client: Redis | None = None


def _redis_endpoint() -> str:
    parsed = urlsplit(settings.redis_url)
    return parsed.hostname or "configured endpoint"


def _build_client() -> Redis:
    return aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=50,
        socket_connect_timeout=3,
        socket_timeout=2,
        retry_on_timeout=True,
    )


async def startup_cache() -> None:
    global _client
    _client = _build_client()
    try:
        await _client.ping()
        log.info("Redis connected: %s", _redis_endpoint())
    except Exception as exc:  # noqa: BLE001
        log.warning("Redis unavailable (%s) – caching disabled", exc)
        _client = None


async def shutdown_cache() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None


def get_cache() -> Redis | None:
    """Return the Redis client, or None if Redis is unavailable."""
    return _client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def cache_get(key: str) -> Any | None:
    client = get_cache()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("cache_get(%s) failed: %s", key, exc)
        return None


async def cache_set(key: str, value: Any, ttl: int = settings.redis_default_ttl) -> None:
    client = get_cache()
    if client is None:
        return
    try:
        await client.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        log.debug("cache_set(%s) failed: %s", key, exc)


async def cache_delete(key: str) -> None:
    client = get_cache()
    if client is None:
        return
    try:
        await client.delete(key)
    except Exception as exc:  # noqa: BLE001
        log.debug("cache_delete(%s) failed: %s", key, exc)


async def cache_or_fetch(
    key: str,
    fetcher: Callable[[], Awaitable[T]],
    ttl: int = settings.redis_default_ttl,
) -> T:
    """
    Cache-aside: return cached value if present, otherwise call `fetcher`,
    cache the result, and return it.
    """
    cached = await cache_get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    value = await fetcher()
    await cache_set(key, value, ttl=ttl)
    return value


async def invalidate_prefix(prefix: str) -> int:
    """Delete all keys that start with `prefix`. Returns number deleted."""
    client = get_cache()
    if client is None:
        return 0
    deleted = 0
    async for key in client.scan_iter(match=f"{prefix}*", count=100):
        await client.delete(key)
        deleted += 1
    return deleted
