"""
Ingest router – high-throughput event ingestion and immediate rule evaluation.

POST /ingest/events       single event
POST /ingest/events/batch up to 500 events in one call
GET  /ingest/metrics      aggregated volume, latency, and processing health
GET  /ingest/health       raw status counts
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.db import get_db, set_org_context
from app.models import RawEvent, Rule, RuleVersion
from app.rules_engine import evaluate_event_and_correlate, load_active_rule_pairs
from app.schemas import (
    IngestBatchCreate,
    IngestBatchOut,
    IngestMetricsOut,
    RawEventCreate,
    RawEventOut,
)

logger = logging.getLogger("opstriage.ingest")
router = APIRouter(prefix="/ingest", tags=["ingest"])


def _validate_event_payload(ev: RawEventCreate) -> dict[str, Any]:
    """
    Validates the event payload before database persistence.
    Rejects invalid inputs with 422 to prevent metric distortion.
    """
    if not ev.source or not ev.source.strip():
        raise HTTPException(
            status_code=422,
            detail="Validation error: 'source' identifier is required and cannot be empty.",
        )
    if ev.payload is None or (isinstance(ev.payload, (dict, list)) and len(ev.payload) == 0):
        raise HTTPException(
            status_code=422,
            detail="Validation error: 'payload' must contain non-empty telemetry readings.",
        )

    return ev.payload if isinstance(ev.payload, dict) else {"items": ev.payload}


async def _store_and_process_event(
    ev: RawEventCreate,
    org_id: str,
    db: AsyncSession,
    active_rule_pairs: list[tuple[Rule, RuleVersion]] | None = None,
) -> tuple[RawEvent, list[uuid.UUID]]:
    cleaned_payload = _validate_event_payload(ev)
    org_uuid = uuid.UUID(org_id)

    row = RawEvent(
        org_id=org_uuid,
        asset_id=ev.asset_id,
        source=ev.source.strip(),
        source_event_id=ev.source_event_id,
        event_type=ev.event_type,
        occurred_at=ev.occurred_at or datetime.now(tz=timezone.utc),
        received_at=datetime.now(tz=timezone.utc),
        payload=cleaned_payload,
        is_synthetic=ev.is_synthetic,
        provenance=ev.provenance,
        processing_status="pending",
    )
    db.add(row)
    await db.flush()

    # Rule evaluation & finding correlation
    matched_finding_ids: list[uuid.UUID] = []
    try:
        # Keep rule effects atomic per event while allowing the raw event itself
        # to be retained with a failed status for diagnosis.
        async with db.begin_nested():
            matched_finding_ids = await evaluate_event_and_correlate(
                event_id=row.id,
                org_id=org_uuid,
                asset_id=row.asset_id,
                source=row.source,
                payload=cleaned_payload,
                occurred_at=row.occurred_at,
                db=db,
                active_rule_pairs=active_rule_pairs,
            )
        row.processing_status = "processed"
        row.processed_at = datetime.now(tz=timezone.utc)
    except Exception as exc:
        logger.exception("Failed during rule evaluation for raw_event %s", row.id)
        row.processing_status = "failed"
        row.processing_error = str(exc)

    return row, matched_finding_ids


@router.post(
    "/events",
    response_model=RawEventOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single event",
)
async def ingest_event(
    body: RawEventCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> RawEventOut:
    await set_org_context(db, org_id)
    row, matched_findings = await _store_and_process_event(body, org_id, db)
    await db.commit()
    await db.refresh(row)

    # Invalidate caches to keep all screens synchronized
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")

    res = RawEventOut.model_validate(row)
    res.matched_findings = matched_findings
    return res


@router.post(
    "/events/batch",
    response_model=IngestBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest up to 500 events",
)
async def ingest_batch(
    body: IngestBatchCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> IngestBatchOut:
    await set_org_context(db, org_id)
    if not body.events:
        raise HTTPException(
            status_code=422,
            detail="Validation error: Batch must contain at least 1 event.",
        )

    # Reject malformed batches before any row is inserted, so clients can
    # correct the batch without leaving a partially persisted request behind.
    invalid_events = []
    for index, event in enumerate(body.events):
        try:
            _validate_event_payload(event)
        except HTTPException as exc:
            invalid_events.append(f"event {index}: {exc.detail}")
    if invalid_events:
        raise HTTPException(status_code=422, detail="; ".join(invalid_events))

    accepted_ids: list[uuid.UUID] = []
    total_triggered = 0
    processed_count = 0
    failed_count = 0
    pending_count = 0

    # Rules are stable for the duration of this request. Fetch them once,
    # rather than issuing the same query for each telemetry event.
    try:
        async with db.begin_nested():
            active_rule_pairs = await load_active_rule_pairs(uuid.UUID(org_id), db)
    except Exception:
        # Preserve per-event failure reporting if the optimization query itself
        # fails. Each event will then retry rule loading inside its savepoint.
        logger.exception("Could not prefetch active rules for ingest batch")
        active_rule_pairs = None
    for ev in body.events:
        row, matched = await _store_and_process_event(ev, org_id, db, active_rule_pairs)
        accepted_ids.append(row.id)
        total_triggered += len(matched)
        processed_count += row.processing_status == "processed"
        failed_count += row.processing_status == "failed"
        pending_count += row.processing_status == "pending"

    await db.commit()

    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")

    return IngestBatchOut(
        accepted=len(accepted_ids),
        rejected=0,
        ids=accepted_ids,
        triggered_findings=total_triggered,
        processed=processed_count,
        failed=failed_count,
        pending=pending_count,
    )


@router.get(
    "/metrics",
    response_model=IngestMetricsOut,
    summary="Aggregated ingestion volume and latency metrics",
)
async def ingest_metrics(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> IngestMetricsOut:
    await set_org_context(db, org_id)
    bind = db.bind
    dialect_name = bind.dialect.name if bind else ""

    # Status breakdown
    status_counts_q = (
        select(RawEvent.processing_status, func.count(RawEvent.id))
        .where(RawEvent.org_id == uuid.UUID(org_id))
        .group_by(RawEvent.processing_status)
    )
    status_res = (await db.execute(status_counts_q)).all()
    s_map = {r[0]: r[1] for r in status_res}
    total = sum(s_map.values())
    processed = s_map.get("processed", 0)
    pending = s_map.get("pending", 0)
    failed = s_map.get("failed", 0)

    # Source breakdown
    source_q = (
        select(RawEvent.source, func.count(RawEvent.id))
        .where(RawEvent.org_id == uuid.UUID(org_id))
        .group_by(RawEvent.source)
        .order_by(func.count(RawEvent.id).desc())
        .limit(10)
    )
    source_res = (await db.execute(source_q)).all()
    sources_breakdown = {r[0]: r[1] for r in source_res}

    # Use a bounded recent sample so latency stays responsive as history grows.
    latency_rows = (await db.execute(
        select(RawEvent.received_at, RawEvent.processed_at)
        .where(
            RawEvent.org_id == uuid.UUID(org_id),
            RawEvent.processed_at.is_not(None),
            RawEvent.processed_at >= RawEvent.received_at,
        )
        .order_by(RawEvent.processed_at.desc())
        .limit(2000)
    )).all()
    latency_samples = sorted(
        max((processed_at - received_at).total_seconds() * 1000.0, 1.0)
        for received_at, processed_at in latency_rows
        if received_at is not None and processed_at is not None
    )
    avg_ms = round(sum(latency_samples) / len(latency_samples), 1) if latency_samples else None
    p95_ms = round(latency_samples[max(0, math.ceil(len(latency_samples) * 0.95) - 1)], 1) if latency_samples else None

    # Recent errors
    err_q = (
        select(RawEvent.id, RawEvent.source, RawEvent.processing_error, RawEvent.occurred_at)
        .where(RawEvent.org_id == uuid.UUID(org_id), RawEvent.processing_status == "failed")
        .order_by(RawEvent.occurred_at.desc())
        .limit(5)
    )
    err_rows = (await db.execute(err_q)).all()
    recent_errors = [
        {
            "id": str(r[0]),
            "source": r[1],
            "error": r[2],
            "occurred_at": r[3].isoformat() if r[3] else None,
        }
        for r in err_rows
    ]

    # PostgreSQL health & latency
    t_db_start = time.perf_counter()
    db_health = "ok"
    db_latency_ms = 0.0
    try:
        await db.execute(text("SELECT 1"))
        db_latency_ms = round((time.perf_counter() - t_db_start) * 1000.0, 2)
    except Exception:
        db_health = "error"

    # Valkey / Redis cache health & latency
    t_cache_start = time.perf_counter()
    cache_health = "ok"
    cache_latency_ms = 0.0
    client = redis.get_cache()
    if client is None:
        cache_health = "unavailable"
    else:
        try:
            await client.ping()
            cache_latency_ms = round((time.perf_counter() - t_cache_start) * 1000.0, 2)
        except Exception:
            cache_health = "unavailable"

    # Active detection rules evaluated during ingestion
    rules_count_q = select(func.count(Rule.id)).where(Rule.org_id == uuid.UUID(org_id), Rule.enabled == True)
    rules_eval_count = (await db.scalar(rules_count_q)) or 0

    # Stage timings are not instrumented yet. Leave them absent rather than
    # deriving synthetic values from whole-request latency.
    stages_breakdown: dict[str, float] = {}

    return IngestMetricsOut(
        total_events=total,
        processed_count=processed,
        pending_count=pending,
        failed_count=failed,
        p95_latency_ms=p95_ms,
        avg_latency_ms=avg_ms,
        sources_breakdown=sources_breakdown,
        recent_errors=recent_errors,
        stages_breakdown=stages_breakdown,
        database_health=db_health,
        database_latency_ms=db_latency_ms,
        cache_health=cache_health,
        cache_latency_ms=cache_latency_ms,
        rules_evaluated_count=rules_eval_count,
    )


@router.get(
    "/health",
    summary="Ingestion pipeline health",
    response_model=dict,
)
async def ingest_health(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await set_org_context(db, org_id)
    counts_q = (
        select(RawEvent.processing_status, func.count(RawEvent.id))
        .where(RawEvent.org_id == uuid.UUID(org_id))
        .group_by(RawEvent.processing_status)
    )
    rows = (await db.execute(counts_q)).all()
    res = {row[0]: row[1] for row in rows}
    res["status"] = "healthy"
    return res
