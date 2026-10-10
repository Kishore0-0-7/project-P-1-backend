"""Health check router – used by load balancers, k8s liveness/readiness probes."""
from __future__ import annotations

from fastapi import APIRouter, Response
from sqlalchemy import text

from app import cache as redis
from app.config import settings
from app.db import AsyncSessionLocal
from app.schemas import HealthOut

router = APIRouter(tags=["meta"])


@router.get("/health", response_model=HealthOut, summary="Health check")
async def health(response: Response) -> HealthOut:
    db_status = "ok"
    cache_status = "ok"

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_status = "error"

    client = redis.get_cache()
    if client is None:
        cache_status = "unavailable"
    else:
        try:
            await client.ping()
        except Exception:  # noqa: BLE001
            cache_status = "unavailable"

    overall = "ok" if db_status == "ok" else "degraded"
    if db_status == "error":
        response.status_code = 503

    return HealthOut(
        status=overall,
        database=db_status,
        cache=cache_status,
        version=settings.app_version,
    )


@router.get("/", include_in_schema=False)
async def root() -> dict:
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}
