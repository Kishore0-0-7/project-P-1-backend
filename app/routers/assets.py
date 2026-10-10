"""Assets router – CRUD."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, set_org_context
from app.models import Asset
from app.schemas import AssetCreate, AssetOut, AssetUpdate

router = APIRouter(prefix="/assets", tags=["assets"])


async def _require_asset(asset_id: uuid.UUID, org_id: str, db: AsyncSession) -> Asset:
    row = await db.get(Asset, asset_id)
    if row is None or str(row.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    return row


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
    q = q.order_by(Asset.criticality, Asset.name).offset(offset).limit(limit)
    rows = (await db.scalars(q)).all()
    return [AssetOut.model_validate(r) for r in rows]


@router.get("/{asset_id}", response_model=AssetOut, summary="Get asset")
async def get_asset(asset_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> AssetOut:
    await set_org_context(db, org_id)
    return AssetOut.model_validate(await _require_asset(asset_id, org_id, db))


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
