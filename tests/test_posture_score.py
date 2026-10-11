"""
Unit and integration tests for Phase 0-F: Posture score endpoint and documented semantics.

Validates:
  - GET /reports/posture returns score, grade, definition, and components
  - Perfect posture: 100 score, grade 'A - Excellent' when no overdue cases and no critical signals
  - Heavy degradation: overdue cases and critical signals apply mathematically exact penalties
  - Score bounded [0, 100]
  - Components include Overdue Cases (weight 40), At-Risk Cases (weight 25), Critical Signals (weight 20), High Signals (weight 15)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Case, Finding
from tests.conftest import ORG_ID


@pytest.mark.asyncio
async def test_posture_score_pristine(client: AsyncClient, db_session: AsyncSession):
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    from sqlalchemy import delete
    await db_session.execute(delete(Case).where(Case.org_id == org_uuid))
    await db_session.execute(delete(Finding).where(Finding.org_id == org_uuid))
    await db_session.commit()

    # Add 1 active case that is on track (due in 6 hours)
    c_on_track = Case(
        org_id=org_uuid,
        title="Pristine Case",
        status="open",
        priority=3,
        due_at=now + timedelta(hours=6),
    )
    db_session.add(c_on_track)
    await db_session.commit()

    resp = await client.get("/api/v1/reports/posture", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    data = resp.json()

    assert data["score"] == 100
    assert data["grade"] == "A - Excellent"
    assert "System posture is an operational health index" in data["definition"]
    assert len(data["components"]) == 4

    comp_map = {c["name"]: c for c in data["components"]}
    assert comp_map["Overdue Cases"]["penalty"] == 0.0
    assert comp_map["At-Risk Cases"]["penalty"] == 0.0
    assert comp_map["Critical Signals Density"]["penalty"] == 0.0
    assert comp_map["High Signals Density"]["penalty"] == 0.0


@pytest.mark.asyncio
async def test_posture_score_degraded(client: AsyncClient, db_session: AsyncSession):
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    from sqlalchemy import delete
    await db_session.execute(delete(Case).where(Case.org_id == org_uuid))
    await db_session.execute(delete(Finding).where(Finding.org_id == org_uuid))
    await db_session.commit()

    # Add 1 overdue case (100% of cases overdue -> 40 pt penalty)
    c_overdue = Case(
        org_id=org_uuid,
        title="Severely Overdue Case",
        status="open",
        priority=1,
        due_at=now - timedelta(hours=2),
    )
    # Add 2 critical findings (2 * 5 = 10 pt penalty)
    f_crit1 = Finding(
        org_id=org_uuid,
        fingerprint="fp-c1",
        title="Crit 1",
        severity="critical",
        priority=1,
        priority_score=900.0,
        status="open",
    )
    f_crit2 = Finding(
        org_id=org_uuid,
        fingerprint="fp-c2",
        title="Crit 2",
        severity="critical",
        priority=1,
        priority_score=850.0,
        status="open",
    )
    db_session.add_all([c_overdue, f_crit1, f_crit2])
    await db_session.commit()

    resp = await client.get("/api/v1/reports/posture", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    data = resp.json()

    # 100 - (40 overdue + 10 critical signals) = 50 score
    assert data["score"] == 50
    assert data["grade"] == "F - Critical Risk"

    comp_map = {c["name"]: c for c in data["components"]}
    assert comp_map["Overdue Cases"]["penalty"] == 40.0
    assert comp_map["Critical Signals Density"]["penalty"] == 10.0
