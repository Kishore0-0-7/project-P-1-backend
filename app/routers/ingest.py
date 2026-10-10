"""
Ingest router – high-throughput event ingestion.

POST /ingest/events       single event
POST /ingest/events/batch up to 500 events in one call

Events are stored in raw_events as 'pending', then processed
asynchronously by the rule engine (background task / worker).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_org_context
from app.models import RawEvent
from app.schemas import IngestBatchCreate, IngestBatchOut, RawEventCreate, RawEventOut

router = APIRouter(prefix="/ingest", tags=["ingest"])


async def _store_events(
    events: list[RawEventCreate],
    org_id: str,
    db: AsyncSession,
) -> list[uuid.UUID]:
    rows = [
        RawEvent(
            org_id=uuid.UUID(org_id),
            asset_id=ev.asset_id,
            source=ev.source,
            source_event_id=ev.source_event_id,
            event_type=ev.event_type,
            occurred_at=ev.occurred_at,
            payload=ev.payload if isinstance(ev.payload, dict) else {"items": ev.payload},
        )
        for ev in events
    ]
    db.add_all(rows)
    await db.commit()
    return [r.id for r in rows]


@router.post(
    "/events",
    response_model=RawEventOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single event",
)
async def ingest_event(
    body: RawEventCreate,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RawEventOut:
    await set_org_context(db, org_id)
    ids = await _store_events([body], org_id, db)
    row = await db.get(RawEvent, ids[0])
    return RawEventOut.model_validate(row)


@router.post(
    "/events/batch",
    response_model=IngestBatchOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest up to 500 events",
)
async def ingest_batch(
    body: IngestBatchCreate,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> IngestBatchOut:
    await set_org_context(db, org_id)
    ids = await _store_events(body.events, org_id, db)
    return IngestBatchOut(accepted=len(ids), rejected=0, ids=ids)


@router.get(
    "/health",
    summary="Ingestion pipeline health",
    response_model=dict,
)
async def ingest_health(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from sqlalchemy import func, select, text
    await set_org_context(db, org_id)
    result = await db.execute(
        text(
            "SELECT processing_status, COUNT(*) AS cnt "
            "FROM opstriage.raw_events "
            "WHERE org_id = :org_id "
            "GROUP BY processing_status"
        ),
        {"org_id": org_id},
    )
    rows = result.fetchall()
    return {row[0]: row[1] for row in rows}
