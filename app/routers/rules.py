"""Rules router – rule management and version publishing."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_org_context
from app.models import Rule, RuleVersion
from app.schemas import RuleCreate, RuleOut, RuleVersionCreate, RuleVersionOut

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("", response_model=list[RuleOut], summary="List rules")
async def list_rules(
    org_id: str = Query(...),
    enabled: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[RuleOut]:
    await set_org_context(db, org_id)
    q = select(Rule).where(Rule.org_id == uuid.UUID(org_id))
    if enabled is not None:
        q = q.where(Rule.enabled == enabled)
    q = q.order_by(Rule.name).limit(limit)
    rows = (await db.scalars(q)).all()
    return [RuleOut.model_validate(r) for r in rows]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED, summary="Create rule")
async def create_rule(body: RuleCreate, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> RuleOut:
    await set_org_context(db, org_id)
    rule = Rule(org_id=uuid.UUID(org_id), key=body.key, name=body.name, description=body.description, enabled=body.enabled)
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.patch("/{rule_id}/toggle", response_model=RuleOut, summary="Toggle rule enabled/disabled")
async def toggle_rule(rule_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> RuleOut:
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    rule.enabled = not rule.enabled
    await db.commit()
    await db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.post("/{rule_id}/versions", response_model=RuleVersionOut, status_code=status.HTTP_201_CREATED, summary="Publish a new rule version")
async def create_rule_version(
    rule_id: uuid.UUID,
    body: RuleVersionCreate,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RuleVersionOut:
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    # Get next version number
    result = await db.execute(
        select(RuleVersion.version)
        .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
        .order_by(RuleVersion.version.desc())
        .limit(1)
    )
    last_version = result.scalar_one_or_none() or 0

    version = RuleVersion(
        org_id=uuid.UUID(org_id),
        rule_id=rule_id,
        version=last_version + 1,
        engine=body.engine,
        definition=body.definition,
        default_severity=body.default_severity,
        default_priority=body.default_priority,
        published_at=datetime.now(tz=timezone.utc),
    )
    db.add(version)
    await db.flush()

    # Set as active version
    rule.active_version_id = version.id
    await db.commit()
    await db.refresh(version)
    return RuleVersionOut.model_validate(version)


@router.get("/{rule_id}/versions", response_model=list[RuleVersionOut], summary="List rule versions")
async def list_rule_versions(rule_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> list[RuleVersionOut]:
    await set_org_context(db, org_id)
    q = (
        select(RuleVersion)
        .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
        .order_by(RuleVersion.version.desc())
    )
    rows = (await db.scalars(q)).all()
    return [RuleVersionOut.model_validate(r) for r in rows]
