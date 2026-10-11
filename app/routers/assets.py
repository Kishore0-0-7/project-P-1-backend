"""Assets router – CRUD."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_org_context
from app.models import Asset, Case, CaseFinding, Finding
from app.schemas import AssetCreate, AssetOut, AssetUpdate

router = APIRouter(prefix="/assets", tags=["assets"])


async def _require_asset(asset_id: uuid.UUID, org_id: str, db: AsyncSession) -> Asset:
    row = await db.get(Asset, asset_id)
    if row is None or str(row.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    return row


async def _enrich_assets(rows: list[Asset], org_id: str, db: AsyncSession) -> list[AssetOut]:
    if not rows:
        return []

    f_q = (
        select(
            Finding.asset_id,
            func.count(Finding.id).label("open_count"),
            func.max(Finding.priority_score).label("max_risk"),
            func.min(Finding.priority).label("min_prio"),
        )
        .where(
            Finding.org_id == uuid.UUID(org_id),
            Finding.status.in_(["open", "acknowledged"]),
            Finding.asset_id.isnot(None),
        )
        .group_by(Finding.asset_id)
    )
    f_stats_rows = (await db.execute(f_q)).all()
    f_stats = {r.asset_id: r for r in f_stats_rows}

    c_q = (
        select(
            Finding.asset_id,
            func.count(func.distinct(CaseFinding.case_id)).label("case_count"),
        )
        .join(CaseFinding, CaseFinding.finding_id == Finding.id)
        .join(Case, Case.id == CaseFinding.case_id)
        .where(
            Case.org_id == uuid.UUID(org_id),
            Case.status.in_(["open", "in_progress", "waiting"]),
            Finding.asset_id.isnot(None),
        )
        .group_by(Finding.asset_id)
    )
    c_stats_rows = (await db.execute(c_q)).all()
    c_stats = {r.asset_id: r.case_count for r in c_stats_rows}

    out: list[AssetOut] = []
    for r in rows:
        f_info = f_stats.get(r.id)
        c_count = c_stats.get(r.id, 0)
        open_count = int(f_info.open_count) if f_info else 0
        raw_max_risk = float(f_info.max_risk) if f_info and f_info.max_risk is not None else 0.0
        # Normalize priority score (0-1000) to 0-100 percentage risk
        risk_score = int(round(min(raw_max_risk, 1000.0) / 10.0))

        if r.status == "retired":
            health = "inactive"
        elif risk_score >= 80:
            health = "critical"
        elif risk_score >= 60:
            health = "at_risk"
        elif risk_score >= 40:
            health = "watch"
        else:
            health = "healthy"

        data = AssetOut.model_validate(r)
        data.open_findings = open_count
        data.active_cases = c_count
        data.risk = risk_score
        data.health = health
        out.append(data)

    out.sort(key=lambda a: (a.risk, a.criticality), reverse=True)
    return out


@router.get("", response_model=list[AssetOut], summary="List assets")
async def list_assets(
    org_id: str = Query(...),
    asset_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[AssetOut]:
    await set_org_context(db, org_id)
    q = select(Asset).where(Asset.org_id == uuid.UUID(org_id))
    if asset_type:
        q = q.where(Asset.asset_type == asset_type)
    if status_filter:
        q = q.where(Asset.status == status_filter)
    q = q.offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    return await _enrich_assets(list(rows), org_id, db)


@router.get("/{asset_id}", response_model=AssetOut, summary="Get asset")
async def get_asset(asset_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> AssetOut:
    await set_org_context(db, org_id)
    asset = await _require_asset(asset_id, org_id, db)
    enriched = await _enrich_assets([asset], org_id, db)
    return enriched[0]


@router.post("", response_model=AssetOut, status_code=status.HTTP_201_CREATED, summary="Create asset")
async def create_asset(body: AssetCreate, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> AssetOut:
    await set_org_context(db, org_id)
    asset = Asset(
        org_id=uuid.UUID(org_id),
        name=body.name,
        asset_type=body.asset_type,
        criticality=body.criticality,
        external_key=body.external_key,
        metadata_=body.metadata,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return AssetOut.model_validate(asset)


@router.patch("/{asset_id}", response_model=AssetOut, summary="Update asset")
async def update_asset(
    asset_id: uuid.UUID,
    body: AssetUpdate,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> AssetOut:
    await set_org_context(db, org_id)
    row = await _require_asset(asset_id, org_id, db)
    update_data = body.model_dump(exclude_none=True)
    if "metadata" in update_data:
        update_data["metadata_"] = update_data.pop("metadata")
    for field, value in update_data.items():
        setattr(row, field, value)
    await db.commit()
    await db.refresh(row)
    return AssetOut.model_validate(row)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Retire asset")
async def retire_asset(asset_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> None:
    await set_org_context(db, org_id)
    row = await _require_asset(asset_id, org_id, db)
    row.status = "retired"
    await db.commit()
