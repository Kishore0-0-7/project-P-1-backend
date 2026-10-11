"""
Findings router – GET (list + detail), POST, PATCH, occurrences.

Caching strategy:
  - GET /findings uses a short Redis TTL (10 s) keyed by org_id + query params
  - PATCH / POST invalidate the org prefix so the next GET is fresh

All queries run inside a DB session that has the tenant RLS context set,
so a misbehaving token that claims a wrong org_id will get zero rows.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.config import settings
from app.db import get_db, set_org_context
from app.models import Asset, Finding, FindingOccurrence
from app.schemas import (
    FindingCreate,
    FindingOut,
    FindingUpdate,
    OccurrenceCreate,
    OccurrenceOut,
    PageMeta,
)
from app.scoring import score_finding

router = APIRouter(prefix="/findings", tags=["findings"])


# ── helpers ──────────────────────────────────────────────────────────────────

def _cache_key(org_id: str, **kwargs) -> str:
    parts = "&".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
    return f"findings:{org_id}:{parts}"


async def _require_finding(
    finding_id: uuid.UUID,
    org_id: str,
    db: AsyncSession,
) -> Finding:
    row = await db.get(Finding, finding_id)
    if row is None or str(row.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Finding not found")
    return row


# ── list ─────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[FindingOut], summary="List findings")
async def list_findings(
    response: Response,
    org_id: str = Query(..., description="Organization UUID"),
    status_filter: list[str] | None = Query(None, alias="status"),
    severity: list[str] | None = Query(None),
    asset_id: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[FindingOut]:
    cache_key = _cache_key(
        org_id, status=status_filter, severity=severity,
        asset_id=str(asset_id), limit=limit, offset=offset,
    )
    cached = await redis.cache_get(cache_key)
    if cached:
        response.headers["X-Cache-Status"] = "HIT"
        return [FindingOut.model_validate(item) for item in cached]

    response.headers["X-Cache-Status"] = "MISS"

    await set_org_context(db, org_id)
    q = (
        select(Finding, Asset.name.label("asset_name"), Asset.asset_type.label("asset_type"))
        .outerjoin(Asset, Finding.asset_id == Asset.id)
        .where(Finding.org_id == uuid.UUID(org_id))
    )

    if status_filter:
        q = q.where(Finding.status.in_(status_filter))
    if severity:
        q = q.where(Finding.severity.in_(severity))
    if asset_id:
        q = q.where(Finding.asset_id == asset_id)

    q = q.order_by(Finding.priority, Finding.last_seen_at.desc()).offset(offset).limit(limit)
    res = (await db.execute(q)).all()
    out = []
    for f_row, a_name, a_type in res:
        item = FindingOut.model_validate(f_row)
        item.asset_name = a_name
        item.asset_type = a_type
        out.append(item)
    await redis.cache_set(cache_key, [o.model_dump(mode="json") for o in out], ttl=settings.redis_findings_ttl)
    return out


# ── detail ────────────────────────────────────────────────────────────────────

@router.get("/{finding_id}", response_model=FindingOut, summary="Get finding")
async def get_finding(
    finding_id: uuid.UUID,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> FindingOut:
    await set_org_context(db, org_id)
    row = await _require_finding(finding_id, org_id, db)
    out = FindingOut.model_validate(row)
    if row.asset_id:
        asset_row = await db.get(Asset, row.asset_id)
        if asset_row:
            out.asset_name = asset_row.name
            out.asset_type = asset_row.asset_type
    return out


# ── create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=FindingOut, status_code=status.HTTP_201_CREATED, summary="Create finding")
async def create_finding(
    body: FindingCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> FindingOut:
    await set_org_context(db, org_id)

    crit = 3
    if body.asset_id:
        asset = await db.get(Asset, body.asset_id)
        if asset and str(asset.org_id) == org_id:
            crit = asset.criticality

    # Re-score using the engine with actual asset criticality
    result = score_finding(
        severity=body.severity,
        asset_criticality=crit,
        occurrence_count=1,
    )

    finding = Finding(
        org_id=uuid.UUID(org_id),
        fingerprint=body.fingerprint,
        title=body.title,
        description=body.description,
        severity=body.severity,
        priority=result.priority,
        priority_score=result.score,
        priority_explanation=result.explanation,
        evidence=body.evidence,
        asset_id=body.asset_id,
        incident_id=body.incident_id,
        rule_version_id=body.rule_version_id,
    )
    db.add(finding)
    await db.commit()
    await db.refresh(finding)
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    return FindingOut.model_validate(finding)


# ── update ────────────────────────────────────────────────────────────────────

@router.patch("/{finding_id}", response_model=FindingOut, summary="Update finding status")
async def update_finding(
    finding_id: uuid.UUID,
    body: FindingUpdate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> FindingOut:
    await set_org_context(db, org_id)
    row = await _require_finding(finding_id, org_id, db)

    update_data = body.model_dump(exclude_none=True)
    if "status" in update_data:
        if update_data["status"] == "acknowledged" and row.acknowledged_at is None:
            update_data["acknowledged_at"] = datetime.now(tz=timezone.utc)
        elif update_data["status"] == "resolved" and row.resolved_at is None:
            update_data["resolved_at"] = datetime.now(tz=timezone.utc)

    for field, value in update_data.items():
        setattr(row, field, value)

    from app.models import AuditEvent
    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="finding",
        entity_id=row.id,
        action="status_updated",
        after_state={"status": row.status, "priority": row.priority},
    ))

    await db.commit()
    await db.refresh(row)
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")
    return FindingOut.model_validate(row)


# ── occurrences ───────────────────────────────────────────────────────────────

@router.post(
    "/{finding_id}/occurrences",
    response_model=OccurrenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a new occurrence of a finding",
)
async def add_occurrence(
    finding_id: uuid.UUID,
    body: OccurrenceCreate,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> OccurrenceOut:
    await set_org_context(db, org_id)
    finding = await _require_finding(finding_id, org_id, db)

    occ = FindingOccurrence(
        org_id=uuid.UUID(org_id),
        finding_id=finding_id,
        raw_event_id=body.raw_event_id,
        observed_at=body.observed_at,
        metric=body.metric,
        value=body.value,
        evidence=body.evidence,
    )
    db.add(occ)

    # bump last_seen_at on parent finding
    finding.last_seen_at = body.observed_at
    await db.commit()
    await db.refresh(occ)
    return OccurrenceOut.model_validate(occ)


@router.get(
    "/{finding_id}/occurrences",
    response_model=list[OccurrenceOut],
    summary="List occurrences for a finding",
)
async def list_occurrences(
    finding_id: uuid.UUID,
    org_id: str = Query(...),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[OccurrenceOut]:
    await set_org_context(db, org_id)
    await _require_finding(finding_id, org_id, db)
    q = (
        select(FindingOccurrence)
        .where(
            FindingOccurrence.org_id == uuid.UUID(org_id),
            FindingOccurrence.finding_id == finding_id,
        )
        .order_by(FindingOccurrence.observed_at.desc())
        .limit(limit)
    )
    rows = (await db.scalars(q)).all()
    return [OccurrenceOut.model_validate(r) for r in rows]


@router.get(
    "/{finding_id}/source-event",
    response_model=dict,
    summary="Get the raw telemetry event that triggered this finding",
)
async def get_source_event(
    finding_id: uuid.UUID,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.models import RawEvent
    await set_org_context(db, org_id)
    finding = await _require_finding(finding_id, org_id, db)

    occ_q = (
        select(FindingOccurrence)
        .where(
            FindingOccurrence.org_id == uuid.UUID(org_id),
            FindingOccurrence.finding_id == finding_id,
            FindingOccurrence.raw_event_id.isnot(None),
        )
        .order_by(FindingOccurrence.observed_at.asc())
        .limit(1)
    )
    occ = (await db.scalars(occ_q)).first()
    if not occ or not occ.raw_event_id:
        return {
            "finding_id": str(finding.id),
            "source": "evidence_snapshot",
            "occurred_at": finding.first_seen_at.isoformat() if finding.first_seen_at else None,
            "payload": finding.evidence,
            "raw_event_id": None,
        }

    raw = await db.get(RawEvent, occ.raw_event_id)
    if not raw:
        return {
            "finding_id": str(finding.id),
            "source": "occurrence_record",
            "occurred_at": occ.observed_at.isoformat(),
            "payload": occ.evidence,
            "raw_event_id": str(occ.raw_event_id),
        }

    return {
        "finding_id": str(finding.id),
        "raw_event_id": str(raw.id),
        "source": raw.source,
        "source_event_id": raw.source_event_id,
        "event_type": raw.event_type,
        "occurred_at": raw.occurred_at.isoformat() if raw.occurred_at else None,
        "received_at": raw.received_at.isoformat() if raw.received_at else None,
        "payload": raw.payload,
        "processing_status": raw.processing_status,
        "triggering_metric": occ.metric,
        "triggering_value": occ.value,
    }

