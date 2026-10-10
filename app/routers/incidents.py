"""Incidents router – CRUD with priority auto-scoring."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.db import get_db, set_org_context
from app.models import Asset, Incident
from app.schemas import IncidentCreate, IncidentOut, IncidentUpdate
from app.scoring import score_incident

router = APIRouter(prefix="/incidents", tags=["incidents"])


async def _require_incident(incident_id: uuid.UUID, org_id: str, db: AsyncSession) -> Incident:
    row = await db.get(Incident, incident_id)
    if row is None or str(row.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Incident not found")
    return row


@router.get("", response_model=list[IncidentOut], summary="List incidents")
async def list_incidents(
    org_id: str = Query(...),
    status_filter: list[str] | None = Query(None, alias="status"),
    priority: int | None = Query(None, ge=1, le=5),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[IncidentOut]:
    await set_org_context(db, org_id)
    q = select(Incident).where(Incident.org_id == uuid.UUID(org_id))
    if status_filter:
        q = q.where(Incident.status.in_(status_filter))
    if priority:
        q = q.where(Incident.priority == priority)
    q = q.order_by(Incident.priority, Incident.opened_at.desc()).offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    return [IncidentOut.model_validate(r) for r in rows]


@router.get("/{incident_id}", response_model=IncidentOut, summary="Get incident")
async def get_incident(incident_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> IncidentOut:
    await set_org_context(db, org_id)
    return IncidentOut.model_validate(await _require_incident(incident_id, org_id, db))


@router.post("", response_model=IncidentOut, status_code=status.HTTP_201_CREATED, summary="Create incident")
async def create_incident(
    body: IncidentCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    await set_org_context(db, org_id)

    # Enrich with asset criticality if asset exists
    asset_criticality = 3
    if body.asset_id:
        asset = await db.get(Asset, body.asset_id)
        if asset and str(asset.org_id) == org_id:
            asset_criticality = asset.criticality

    scored = score_incident(
        impact=body.impact,
        urgency=body.urgency,
        asset_criticality=asset_criticality,
    )

    incident = Incident(
        org_id=uuid.UUID(org_id),
        asset_id=body.asset_id,
        title=body.title,
        description=body.description,
        category=body.category,
        source=body.source,
        external_key=body.external_key,
        impact=body.impact,
        urgency=body.urgency,
        priority=scored.priority,
        priority_score=scored.score,
        priority_explanation=scored.explanation,
        sla_due_at=body.sla_due_at,
    )
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    background_tasks.add_task(redis.invalidate_prefix, f"incidents:{org_id}")
    return IncidentOut.model_validate(incident)


@router.patch("/{incident_id}", response_model=IncidentOut, summary="Update incident")
async def update_incident(
    incident_id: uuid.UUID,
    body: IncidentUpdate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> IncidentOut:
    await set_org_context(db, org_id)
    row = await _require_incident(incident_id, org_id, db)
    update_data = body.model_dump(exclude_none=True)

    now = datetime.now(tz=timezone.utc)
    for field, value in update_data.items():
        setattr(row, field, value)
    if row.status == "acknowledged" and row.acknowledged_at is None:
        row.acknowledged_at = now
    if row.status == "resolved" and row.resolved_at is None:
        row.resolved_at = now

    # Re-score if impact/urgency changed
    if "impact" in update_data or "urgency" in update_data:
        scored = score_incident(impact=row.impact, urgency=row.urgency)
        row.priority = scored.priority
        row.priority_score = scored.score
        row.priority_explanation = scored.explanation

    await db.commit()
    await db.refresh(row)
    background_tasks.add_task(redis.invalidate_prefix, f"incidents:{org_id}")
    return IncidentOut.model_validate(row)
