"""
Unit and integration tests for Phase 0-B: Asset risk normalization and documented health bands.

Validates:
  - Asset risk normalization to 0-100 range (from finding priority_score 0-1000)
  - Documented score bands:
      80-100 -> critical
      60-79  -> at_risk
      40-59  -> watch
      0-39   -> healthy
  - Retired assets explicitly given 'inactive' health status (never 'critical')
  - Critical severity finding on tier-5 asset maps to Priority 1 (P0-13 fix)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, Finding
from app.scoring import score_finding
from tests.conftest import ORG_ID


def test_score_finding_critical_tier_5_is_p1():
    """Verify that a critical finding on a tier-5 asset scores >= 800 (Priority 1)."""
    res = score_finding(severity="critical", asset_criticality=5, occurrence_count=1)
    assert res.priority == 1
    assert res.score >= 800.0


@pytest.mark.asyncio
async def test_asset_risk_bands_and_retired_status(client: AsyncClient, db_session: AsyncSession):
    """Verify that GET /assets normalizes risk to 0-100 and assigns health bands."""
    org_uuid = uuid.UUID(ORG_ID)
    from sqlalchemy import delete
    await db_session.execute(delete(Finding).where(Finding.org_id == org_uuid))
    await db_session.execute(delete(Asset).where(Asset.org_id == org_uuid))
    await db_session.commit()

    # Create 5 assets to test each band and retired status:
    # Asset 1: Critical (score ~ 850 -> risk 85)
    a_crit = Asset(org_id=org_uuid, name="Critical Asset", asset_type="server", criticality=5, status="active")
    # Asset 2: At Risk (score ~ 650 -> risk 65)
    a_at_risk = Asset(org_id=org_uuid, name="At Risk Asset", asset_type="db", criticality=4, status="active")
    # Asset 3: Watch (score ~ 450 -> risk 45)
    a_watch = Asset(org_id=org_uuid, name="Watch Asset", asset_type="switch", criticality=3, status="active")
    # Asset 4: Healthy (score 0 -> risk 0)
    a_healthy = Asset(org_id=org_uuid, name="Healthy Asset", asset_type="iot", criticality=2, status="active")
    # Asset 5: Retired
    a_retired = Asset(org_id=org_uuid, name="Retired Asset", asset_type="legacy", criticality=1, status="retired")

    db_session.add_all([a_crit, a_at_risk, a_watch, a_healthy, a_retired])
    await db_session.flush()

    # Create findings with known priority scores
    f_crit = Finding(
        org_id=org_uuid,
        asset_id=a_crit.id,
        fingerprint="fp-crit",
        title="Critical finding",
        severity="critical",
        priority=1,
        priority_score=850.0,
        status="open",
    )
    f_at_risk = Finding(
        org_id=org_uuid,
        asset_id=a_at_risk.id,
        fingerprint="fp-at-risk",
        title="High finding",
        severity="high",
        priority=2,
        priority_score=650.0,
        status="open",
    )
    f_watch = Finding(
        org_id=org_uuid,
        asset_id=a_watch.id,
        fingerprint="fp-watch",
        title="Medium finding",
        severity="medium",
        priority=3,
        priority_score=450.0,
        status="open",
    )
    db_session.add_all([f_crit, f_at_risk, f_watch])
    await db_session.commit()

    resp = await client.get("/api/v1/assets", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    assets = resp.json()

    asset_map = {a["name"]: a for a in assets}

    # Verify risk normalization (0-100) and health label
    assert asset_map["Critical Asset"]["risk"] == 85
    assert asset_map["Critical Asset"]["health"] == "critical"

    assert asset_map["At Risk Asset"]["risk"] == 65
    assert asset_map["At Risk Asset"]["health"] == "at_risk"

    assert asset_map["Watch Asset"]["risk"] == 45
    assert asset_map["Watch Asset"]["health"] == "watch"

    assert asset_map["Healthy Asset"]["risk"] == 0
    assert asset_map["Healthy Asset"]["health"] == "healthy"

    # Retired asset must be inactive, NOT critical
    assert asset_map["Retired Asset"]["health"] == "inactive"
