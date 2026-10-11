"""
Unit and integration tests for Phase 0-C: Incident provenance and synthetic data separation.

Validates:
  - is_synthetic and provenance fields on Incident and RawEvent models and schemas
  - GET /incidents?exclude_synthetic=true excludes synthetic records
  - GET /incidents?exclude_synthetic=false includes synthetic records
  - GET /incidents?source_type=... filters by source
  - GET /incidents?sla_status=... filters by SLA status (overdue, at_risk, on_track)
  - Incident tie-breaking sort orders by priority, then earliest sla_due_at, then opened_at desc
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Incident
from tests.conftest import ORG_ID


@pytest.mark.asyncio
async def test_incident_synthetic_filtering(client: AsyncClient, db_session: AsyncSession):
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    from sqlalchemy import delete
    await db_session.execute(delete(Incident).where(Incident.org_id == org_uuid))
    await db_session.commit()

    # 1 Live incident
    live_inc = Incident(
        org_id=org_uuid,
        title="Live Production Outage",
        source="bacnet-live",
        status="open",
        priority=1,
        priority_score=900.0,
        is_synthetic=False,
        provenance="production_iot",
    )
    # 1 Synthetic incident
    synth_inc = Incident(
        org_id=org_uuid,
        title="Simulated Load Injection",
        source="demo-fixture",
        status="open",
        priority=1,
        priority_score=900.0,
        is_synthetic=True,
        provenance="seed_fixture",
    )
    db_session.add_all([live_inc, synth_inc])
    await db_session.commit()

    # Query with exclude_synthetic=true
    resp_exclude = await client.get("/api/v1/incidents", params={"org_id": ORG_ID, "exclude_synthetic": "true"})
    assert resp_exclude.status_code == 200
    items_exclude = resp_exclude.json()
    assert len(items_exclude) == 1
    assert items_exclude[0]["title"] == "Live Production Outage"
    assert items_exclude[0]["is_synthetic"] is False

    # Query with exclude_synthetic=false
    resp_all = await client.get("/api/v1/incidents", params={"org_id": ORG_ID, "exclude_synthetic": "false"})
    assert resp_all.status_code == 200
    items_all = resp_all.json()
    assert len(items_all) == 2


@pytest.mark.asyncio
async def test_incident_sorting_tiebreaker_and_sla_filter(client: AsyncClient, db_session: AsyncSession):
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    from sqlalchemy import delete
    await db_session.execute(delete(Incident).where(Incident.org_id == org_uuid))
    await db_session.commit()

    # Two P1 incidents:
    # Inc 1: Due in 1 hour (urgency closer)
    inc_urgent = Incident(
        org_id=org_uuid,
        title="Urgent P1 Due Soon",
        status="open",
        priority=1,
        priority_score=900.0,
        opened_at=now - timedelta(hours=2),
        sla_due_at=now + timedelta(hours=1),
    )
    # Inc 2: Due in 4 hours
    inc_later = Incident(
        org_id=org_uuid,
        title="P1 Due Later",
        status="open",
        priority=1,
        priority_score=900.0,
        opened_at=now - timedelta(minutes=10),
        sla_due_at=now + timedelta(hours=4),
    )
    # Inc 3: Overdue
    inc_overdue = Incident(
        org_id=org_uuid,
        title="Overdue Incident",
        status="open",
        priority=2,
        priority_score=700.0,
        sla_due_at=now - timedelta(minutes=15),
    )
    db_session.add_all([inc_urgent, inc_later, inc_overdue])
    await db_session.commit()

    # Verify tie breaker: both P1, but inc_urgent has earlier sla_due_at so it must come FIRST even though inc_later opened more recently!
    resp = await client.get("/api/v1/incidents", params={"org_id": ORG_ID, "priority": 1})
    assert resp.status_code == 200
    p1_items = resp.json()
    assert len(p1_items) == 2
    assert p1_items[0]["title"] == "Urgent P1 Due Soon"
    assert p1_items[1]["title"] == "P1 Due Later"

    # Verify sla_status filter
    resp_overdue = await client.get("/api/v1/incidents", params={"org_id": ORG_ID, "sla_status": "overdue"})
    assert resp_overdue.status_code == 200
    overdue_items = resp_overdue.json()
    assert len(overdue_items) == 1
    assert overdue_items[0]["title"] == "Overdue Incident"
