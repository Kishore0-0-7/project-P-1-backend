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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.config import settings
from app.db import get_db, set_org_context
from app.models import Finding, FindingOccurrence
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
        return [FindingOut.model_validate(item) for item in cached]

    await set_org_context(db, org_id)
    q = select(Finding).where(Finding.org_id == uuid.UUID(org_id))

    if status_filter:
        q = q.where(Finding.status.in_(status_filter))
    if severity:
        q = q.where(Finding.severity.in_(severity))
    if asset_id:
        q = q.where(Finding.asset_id == asset_id)

    q = q.order_by(Finding.priority, Finding.last_seen_at.desc()).offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    out = [FindingOut.model_validate(r) for r in rows]
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
    return FindingOut.model_validate(row)


# ── create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=FindingOut, status_code=status.HTTP_201_CREATED, summary="Create finding")
async def create_finding(
    body: FindingCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> FindingOut:
    await set_org_context(db, org_id)

    # Re-score using the engine (caller may provide their own score too)
    result = score_finding(
        severity=body.severity,
        asset_criticality=3,  # default; enrich from assets table if asset_id present
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

    await db.commit()
    await db.refresh(row)
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
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
