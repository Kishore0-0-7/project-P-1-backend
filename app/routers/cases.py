"""Cases router – full CRUD with case-finding linking and event trail."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.db import get_db, set_org_context
from app.models import Case, CaseEvent, CaseFinding, Finding
from app.schemas import CaseCreate, CaseOut, CaseUpdate

router = APIRouter(prefix="/cases", tags=["cases"])


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
    return [CaseOut.model_validate(r) for r in rows]


@router.get("/{case_id}", response_model=CaseOut, summary="Get case")
async def get_case(case_id: str, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> CaseOut:
    await set_org_context(db, org_id)
    return CaseOut.model_validate(await _require_case(case_id, org_id, db))



@router.post("", response_model=CaseOut, status_code=status.HTTP_201_CREATED, summary="Create case")
async def create_case(
    body: CaseCreate,
    org_id: str = Query(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
) -> CaseOut:
    await set_org_context(db, org_id)

    case = Case(
        org_id=uuid.UUID(org_id),
        title=body.title,
        priority=body.priority,
        assigned_to=body.assigned_to,
        external_key=body.external_key,
        due_at=body.due_at,
    )
    db.add(case)
    await db.flush()  # get case.id without committing

    # Link findings
    for fid in body.finding_ids:
        finding = await db.get(Finding, fid)
        if finding and str(finding.org_id) == org_id:
            db.add(CaseFinding(org_id=uuid.UUID(org_id), case_id=case.id, finding_id=fid))

    # Record creation event
    db.add(CaseEvent(
        org_id=uuid.UUID(org_id),
        case_id=case.id,
        event_type="created",
        payload={"title": body.title, "priority": body.priority},
    ))

    await db.commit()
    await db.refresh(case)
    background_tasks.add_task(redis.invalidate_prefix, f"cases:{org_id}")
    return CaseOut.model_validate(case)


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
    update_data = body.model_dump(exclude_none=True)

    old_status = row.status
    for field, value in update_data.items():
        setattr(row, field, value)

    now = datetime.now(tz=timezone.utc)
    if row.status == "resolved" and row.resolved_at is None:
        row.resolved_at = now
    if row.status in ("closed", "cancelled") and row.closed_at is None:
        row.closed_at = now

    if old_status != row.status:
        db.add(CaseEvent(
            org_id=uuid.UUID(org_id),
            case_id=row.id,
            event_type="status_changed",
            payload={"from": old_status, "to": row.status},
        ))

    await db.commit()
    await db.refresh(row)
    background_tasks.add_task(redis.invalidate_prefix, f"cases:{org_id}")
    return CaseOut.model_validate(row)


@router.post("/{case_id}/findings/{finding_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Link finding to case")
async def link_finding(
    case_id: str,
    finding_id: uuid.UUID,
    org_id: str = Query(...),
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
        await db.commit()

