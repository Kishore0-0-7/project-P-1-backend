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
    exclude_synthetic: bool = Query(False),
    source_type: str | None = Query(None),
    sla_status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[IncidentOut]:
    await set_org_context(db, org_id)
    q = select(Incident).where(Incident.org_id == uuid.UUID(org_id))
    if exclude_synthetic:
        q = q.where(Incident.is_synthetic.is_(False))
    if status_filter:
        q = q.where(Incident.status.in_(status_filter))
    if priority:
        q = q.where(Incident.priority == priority)
    if source_type:
        q = q.where(Incident.source == source_type)
    q = q.order_by(Incident.priority, Incident.sla_due_at.asc().nulls_last(), Incident.opened_at.desc()).offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    out = [IncidentOut.model_validate(r) for r in rows]
    if sla_status:
        now = datetime.now(timezone.utc)
        filtered = []
        for inc in out:
            if inc.sla_due_at:
                due = inc.sla_due_at if inc.sla_due_at.tzinfo else inc.sla_due_at.replace(tzinfo=timezone.utc)
                if inc.status in ("resolved", "closed"):
                    res_time = inc.resolved_at or inc.closed_at
                    if res_time and not res_time.tzinfo:
                        res_time = res_time.replace(tzinfo=timezone.utc)
                    st = "met" if not res_time or res_time <= due else "breached"
                else:
                    if due < now:
                        st = "overdue"
                    elif (due - now).total_seconds() <= 7200:
                        st = "at_risk"
                    else:
                        st = "on_track"
            else:
                st = "no_sla"
            if st == sla_status or (sla_status == "compliant" and st == "on_track"):
                filtered.append(inc)
        return filtered
    return out


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
        is_synthetic=body.is_synthetic,
        provenance=body.provenance,
        status="open",
    )
    db.add(incident)
    await db.flush()

    from app.models import AuditEvent, Finding
    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="incident",
        entity_id=incident.id,
        action="incident_created",
        after_state={"title": incident.title, "priority": incident.priority, "status": incident.status},
    ))

    await db.commit()
    await db.refresh(incident)
    background_tasks.add_task(redis.invalidate_prefix, f"incidents:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
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

    old_status = row.status
    now = datetime.now(tz=timezone.utc)
    for field, value in update_data.items():
        setattr(row, field, value)
    if row.status == "acknowledged" and row.acknowledged_at is None:
        row.acknowledged_at = now
    if row.status == "resolved" and row.resolved_at is None:
        row.resolved_at = now
    if row.status == "closed" and row.closed_at is None:
        row.closed_at = now
    if row.status == "open" and old_status in ("resolved", "closed"):
        row.resolved_at = None
        row.closed_at = None

    # Re-score if impact/urgency changed
    if "impact" in update_data or "urgency" in update_data:
        scored = score_incident(impact=row.impact, urgency=row.urgency)
        row.priority = scored.priority
        row.priority_score = scored.score
        row.priority_explanation = scored.explanation

    from app.models import AuditEvent, Finding
    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="incident",
        entity_id=row.id,
        action="status_changed" if old_status != row.status else "incident_updated",
        before_state={"status": old_status},
        after_state={"status": row.status, "priority": row.priority},
    ))

    # Cascade resolution to linked findings
    if row.status == "resolved":
        lf_q = select(Finding).where(Finding.incident_id == row.id)
        linked_findings = (await db.scalars(lf_q)).all()
        for lf in linked_findings:
            if lf.status != "resolved":
                lf.status = "resolved"
                lf.resolved_at = now
                db.add(AuditEvent(
                    org_id=uuid.UUID(org_id),
                    entity_type="finding",
                    entity_id=lf.id,
                    action="resolved_by_incident",
                    after_state={"incident_id": str(row.id), "status": "resolved"},
                ))

    await db.commit()
    await db.refresh(row)
    background_tasks.add_task(redis.invalidate_prefix, f"incidents:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    return IncidentOut.model_validate(row)

