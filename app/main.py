"""
OpsTriage FastAPI application.

Entry point used by uvicorn:
    uvicorn app.main:app --reload

Startup sequence:
  1. Validate DB connection
  2. Connect Redis (non-fatal if down)
  3. Attach all routers

Shutdown sequence:
  1. Flush Redis connections
  2. Dispose DB engine pool
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app import cache as redis_cache
from app.config import settings
from app.db import shutdown_db, startup_db
from app.routers import ai, assets, auth, cases, findings, health, incidents, ingest, integrations, reports, rules

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    log.info("Starting OpsTriage API v%s (%s)", settings.app_version, settings.app_env)
    await startup_db()
    await redis_cache.startup_cache()
    yield
    log.info("Shutting down")
    await redis_cache.shutdown_cache()
    await shutdown_db()


# ── App ───────────────────────────────────────────────────────────────────────

TAGS_METADATA = [
    {
        "name": "auth",
        "description": "JWT authentication, login/logout session lifecycle, organization user creation, registration, and role management.",
    },
    {
        "name": "ai",
        "description": "Explainable AI triage analysis, SLA counterfactual simulation, and HVAC AFDD benchmarks.",
    },
    {
        "name": "findings",
        "description": "Alert deduplication, multi-factor scoring (1-5), and occurrence correlation.",
    },
    {
        "name": "cases",
        "description": "Operational case lifecycle, engineer assignments, and resolution notes.",
    },
    {
        "name": "incidents",
        "description": "High-level incident tracking with asset-enriched auto-scoring and SLA deadlines.",
    },
    {
        "name": "assets",
        "description": "Equipment, server, and service registry with tenant-isolated criticality tiers.",
    },
    {
        "name": "ingest",
        "description": "High-throughput single and batched event ingestion into raw_events pipeline.",
    },
    {
        "name": "reports",
        "description": "Aggregated SLA breach analytics and anomalous asset rankings with Redis caching.",
    },
    {
        "name": "rules",
        "description": "Detection rule management with immutable versioning (threshold, rolling window, expression, ML).",
    },
    {
        "name": "health",
        "description": "Liveness/readiness probes, PostgreSQL connectivity, and Redis cache health.",
    },
]

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="""
### OpsTriage — High-Concurrency Incident Triage & Automated Fault Diagnostics

OpsTriage eliminates alert fatigue by applying explainable multi-factor scoring,
automated correlation, and AI-driven SLA protection.

#### Key Architectural Highlights:
* **High-Concurrency Async Core**: Built on Python 3.12+ / FastAPI / asyncpg / SQLAlchemy 2.0.
* **Tenant Isolation**: PostgreSQL Row-Level Security (RLS) driven by transaction session context.
* **Low Latency Caching**: Sub-millisecond Redis response caching and Nginx micro-caching.
* **AI Diagnostics & AFDD**: Automated HVAC fault detection (Precision: 1.0, F1: 0.955) and counterfactual SLA optimization.
""",
    openapi_tags=TAGS_METADATA,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    swagger_ui_parameters={
        "defaultModelsExpandDepth": -1,
        "docExpansion": "list",
        "filter": True,
        "syntaxHighlight.theme": "monokai",
    },
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    """Inject defense-in-depth HTTP security headers (OWASP recommendations)."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.middleware("http")
async def add_request_id(request: Request, call_next) -> Response:
    """Inject X-Request-ID into every response for distributed tracing."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def add_process_time(request: Request, call_next) -> Response:
    """Add X-Process-Time-Ms header for performance monitoring."""
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Process-Time-Ms"] = str(elapsed_ms)
    return response


# ── Routers ───────────────────────────────────────────────────────────────────

API_PREFIX = "/api/v1"

app.include_router(health.router)
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(findings.router, prefix=API_PREFIX)
app.include_router(cases.router, prefix=API_PREFIX)
app.include_router(incidents.router, prefix=API_PREFIX)
app.include_router(assets.router, prefix=API_PREFIX)
app.include_router(ingest.router, prefix=API_PREFIX)
app.include_router(reports.router, prefix=API_PREFIX)
app.include_router(rules.router, prefix=API_PREFIX)
app.include_router(ai.router, prefix=API_PREFIX)
app.include_router(integrations.router, prefix=API_PREFIX)
