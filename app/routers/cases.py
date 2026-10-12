"""Cases router – full CRUD with case-finding linking, cascading resolution, and audit trail."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.db import get_db, set_org_context
from app.models import Asset, AuditEvent, Case, CaseEvent, CaseFinding, Finding, OrganizationMembership, User
from app.schemas import CaseCreate, CaseOut, CaseUpdate

router = APIRouter(prefix="/cases", tags=["cases"])


USER_NAME_FALLBACKS = {
    uuid.UUID("b0000000-0000-0000-0000-000000000001"): "Lead Triage Engineer",
    uuid.UUID("b0000000-0000-0000-0000-000000000002"): "Sarah Chen (Principal SRE)",
    uuid.UUID("b0000000-0000-0000-0000-000000000003"): "Marcus Vance (Duty Officer)",
    uuid.UUID("b0000000-0000-0000-0000-000000000004"): "Elena Rostova (ML Reliability)",
}


async def _enrich_cases(cases: list[Case], db: AsyncSession) -> list[CaseOut]:
    if not cases:
        return []

    now = datetime.now(timezone.utc)
    u_ids = {c.assigned_to for c in cases if c.assigned_to}
    users_map: dict[uuid.UUID, str] = {}
    if u_ids:
        u_q = select(User).where(User.id.in_(u_ids))
        for u in (await db.scalars(u_q)).all():
            users_map[u.id] = u.display_name or u.email or "Assigned Engineer"

    # Fetch linked findings and asset names for all cases
    case_ids = [c.id for c in cases]
    cf_q = (
        select(CaseFinding.case_id, Finding.id, Asset.name)
        .join(Finding, CaseFinding.finding_id == Finding.id)
        .outerjoin(Asset, Finding.asset_id == Asset.id)
        .where(CaseFinding.case_id.in_(case_ids))
    )
    cf_rows = (await db.execute(cf_q)).all()
    case_findings_map: dict[uuid.UUID, list[uuid.UUID]] = {}
    case_assets_map: dict[uuid.UUID, set[str]] = {}
    for c_id, f_id, a_name in cf_rows:
        case_findings_map.setdefault(c_id, []).append(f_id)
        if a_name:
            case_assets_map.setdefault(c_id, set()).add(a_name)

    out: list[CaseOut] = []
    for c in cases:
        data = CaseOut.model_validate(c)
        data.finding_ids = case_findings_map.get(c.id, [])
        data.asset_names = sorted(case_assets_map.get(c.id, set()))

        if c.assigned_to:
            data.assignee_name = (
                users_map.get(c.assigned_to)
                or USER_NAME_FALLBACKS.get(c.assigned_to)
                or "Assigned Engineer"
            )
        else:
            data.assignee_name = "Unassigned"

        if c.due_at:
            due = c.due_at if c.due_at.tzinfo else c.due_at.replace(tzinfo=timezone.utc)
            if c.status in ("resolved", "closed"):
                data.is_overdue = False
                res_time = (
                    c.resolved_at.replace(tzinfo=timezone.utc)
                    if c.resolved_at and not c.resolved_at.tzinfo
                    else c.resolved_at
                ) if c.resolved_at else None
                data.sla_status = "met" if not res_time or res_time <= due else "breached"
            else:
                data.is_overdue = due < now
                rem = (due - now).total_seconds()
                if data.is_overdue:
                    data.sla_status = "overdue"
                elif rem <= 7200:
                    data.sla_status = "at_risk"
                else:
                    data.sla_status = "on_track"
        else:
            data.is_overdue = False
            data.sla_status = "no_sla"

        out.append(data)
    return out


async def _require_case(case_id: str, org_id: str, db: AsyncSession) -> Case:
    parsed_uuid: uuid.UUID | None = None
    try:
        parsed_uuid = uuid.UUID(case_id)
    except (ValueError, AttributeError):
        pass

    if parsed_uuid:
        row = await db.get(Case, parsed_uuid)
    else:
        q = select(Case).where(Case.org_id == uuid.UUID(org_id), Case.external_key == case_id)
        row = (await db.scalars(q)).first()

    if row is None or str(row.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Case not found")
    return row


async def _validate_assignee(assignee_id: uuid.UUID | None, org_id: str, db: AsyncSession) -> None:
    if assignee_id is None:
        return
    membership = await db.scalar(
        select(OrganizationMembership.user_id).where(
            OrganizationMembership.org_id == uuid.UUID(org_id),
            OrganizationMembership.user_id == assignee_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=422, detail="Case owner must be an active member of this organization.")


@router.get("", response_model=list[CaseOut], summary="List cases")
async def list_cases(
    org_id: str = Query(...),
    status_filter: list[str] | None = Query(None, alias="status"),
    priority: int | None = Query(None, ge=1, le=5),
    assigned_to: uuid.UUID | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[CaseOut]:
    await set_org_context(db, org_id)
    q = select(Case).where(Case.org_id == uuid.UUID(org_id))
    if status_filter:
        q = q.where(Case.status.in_(status_filter))
    if priority:
        q = q.where(Case.priority == priority)
    if assigned_to:
        q = q.where(Case.assigned_to == assigned_to)
    q = q.order_by(Case.priority, Case.created_at.desc()).offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    return await _enrich_cases(list(rows), db)


@router.get("/{case_id}", response_model=CaseOut, summary="Get case")
async def get_case(case_id: str, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> CaseOut:
    await set_org_context(db, org_id)
    case = await _require_case(case_id, org_id, db)
    enriched = await _enrich_cases([case], db)
    return enriched[0]


@router.post("", response_model=CaseOut, status_code=status.HTTP_201_CREATED, summary="Create case")
async def create_case(
    body: CaseCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> CaseOut:
    await set_org_context(db, org_id)
    await _validate_assignee(body.assigned_to, org_id, db)

    case = Case(
        org_id=uuid.UUID(org_id),
        title=body.title,
        priority=body.priority,
        assigned_to=body.assigned_to,
        external_key=body.external_key,
        due_at=body.due_at,
    )
    db.add(case)
    await db.flush()

    # Link findings
    for fid in body.finding_ids:
        finding = await db.get(Finding, fid)
        if finding and str(finding.org_id) == org_id:
            db.add(CaseFinding(org_id=uuid.UUID(org_id), case_id=case.id, finding_id=fid))

    # Record creation event & audit
    db.add(CaseEvent(
        org_id=uuid.UUID(org_id),
        case_id=case.id,
        event_type="created",
        payload={"title": body.title, "priority": body.priority},
    ))
    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="case",
        entity_id=case.id,
        action="case_created",
        after_state={"title": body.title, "priority": body.priority, "assigned_to": str(body.assigned_to) if body.assigned_to else None},
    ))

    await db.commit()
    await db.refresh(case)

    background_tasks.add_task(redis.invalidate_prefix, f"cases:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")

    enriched = await _enrich_cases([case], db)
    return enriched[0]


@router.patch("/{case_id}", response_model=CaseOut, summary="Update case")
async def update_case(
    case_id: str,
    body: CaseUpdate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> CaseOut:
    await set_org_context(db, org_id)
    row = await _require_case(case_id, org_id, db)
    update_data = body.model_dump(exclude_unset=True)
    if "assigned_to" in update_data:
        await _validate_assignee(update_data["assigned_to"], org_id, db)

    old_status = row.status
    old_assignee = row.assigned_to
    for field, value in update_data.items():
        setattr(row, field, value)

    now = datetime.now(tz=timezone.utc)
    if row.status == "resolved" and row.resolved_at is None:
        row.resolved_at = now
    if row.status in ("closed", "cancelled") and row.closed_at is None:
        row.closed_at = now

    # Audit & case events
    if old_assignee != row.assigned_to:
        db.add(CaseEvent(
            org_id=uuid.UUID(org_id),
            case_id=row.id,
            event_type="assigned",
            payload={"from": str(old_assignee) if old_assignee else None, "to": str(row.assigned_to) if row.assigned_to else None},
        ))
        db.add(AuditEvent(
            org_id=uuid.UUID(org_id),
            entity_type="case",
            entity_id=row.id,
            action="case_assigned",
            after_state={"assigned_to": str(row.assigned_to) if row.assigned_to else None},
        ))

    if old_status != row.status:
        db.add(CaseEvent(
            org_id=uuid.UUID(org_id),
            case_id=row.id,
            event_type="status_changed",
            payload={"from": old_status, "to": row.status},
        ))
        db.add(AuditEvent(
            org_id=uuid.UUID(org_id),
            entity_type="case",
            entity_id=row.id,
            action="status_changed",
            before_state={"status": old_status},
            after_state={"status": row.status, "resolution_notes": row.resolution_notes},
        ))

    # Cascade resolution to linked findings when case is marked resolved
    if row.status == "resolved":
        cf_q = select(Finding).join(CaseFinding, CaseFinding.finding_id == Finding.id).where(CaseFinding.case_id == row.id)
        linked_findings = (await db.scalars(cf_q)).all()
        for fnd in linked_findings:
            if fnd.status != "resolved":
                fnd.status = "resolved"
                if fnd.resolved_at is None:
                    fnd.resolved_at = now
                db.add(AuditEvent(
                    org_id=uuid.UUID(org_id),
                    entity_type="finding",
                    entity_id=fnd.id,
                    action="resolved_by_case",
                    after_state={"case_id": str(row.id), "status": "resolved"},
                ))

    await db.commit()
    await db.refresh(row)

    background_tasks.add_task(redis.invalidate_prefix, f"cases:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"findings:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
    background_tasks.add_task(redis.invalidate_prefix, f"reports:{org_id}")

    enriched = await _enrich_cases([row], db)
    return enriched[0]


@router.post("/{case_id}/findings/{finding_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Link finding to case")
async def link_finding(
    case_id: str,
    finding_id: uuid.UUID,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> None:
    await set_org_context(db, org_id)
    case = await _require_case(case_id, org_id, db)
    finding = await db.get(Finding, finding_id)
    if finding is None or str(finding.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Finding not found")
    existing = await db.get(CaseFinding, (case.id, finding_id))
    if existing is None:
        db.add(CaseFinding(org_id=uuid.UUID(org_id), case_id=case.id, finding_id=finding_id))
        db.add(AuditEvent(
            org_id=uuid.UUID(org_id),
            entity_type="case",
            entity_id=case.id,
            action="finding_linked",
            after_state={"finding_id": str(finding_id)},
        ))
        await db.commit()
        background_tasks.add_task(redis.invalidate_prefix, f"cases:{org_id}")
        background_tasks.add_task(redis.invalidate_prefix, f"assets:{org_id}")
