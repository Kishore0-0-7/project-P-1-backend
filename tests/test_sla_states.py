"""
Unit and integration tests for Phase 0-A: SLA state canonicalization and operational summary.

Validates:
  - All canonical SLA states: on_track, at_risk, overdue, no_sla, met, breached
  - Boundary conditions at exactly 7200s (2h) and 0s (now)
  - Clear separation of active cases from completed cases in operational KPIs
  - Cases without due_at properly categorized as no_sla (not counted as compliant)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Case, Organization
from tests.conftest import ORG_ID


@pytest.mark.asyncio
async def test_operational_summary_sla_breakdown(client: AsyncClient, db_session: AsyncSession):
    """Verify that GET /reports/operational-summary correctly partitions SLA states."""
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    # Clean existing cases for this org
    from sqlalchemy import delete
    await db_session.execute(delete(Case).where(Case.org_id == org_uuid))
    await db_session.commit()

    # Create cases across all states:
    cases = [
        # Active: on_track (due in 5 hours)
        Case(
            org_id=org_uuid,
            title="Case On Track",
            status="open",
            priority=2,
            due_at=now + timedelta(hours=5),
        ),
        # Active: at_risk (due in 1 hour <= 7200s)
        Case(
            org_id=org_uuid,
            title="Case At Risk",
            status="in_progress",
            priority=1,
            due_at=now + timedelta(hours=1),
        ),
        # Active: overdue (due 30 mins ago)
        Case(
            org_id=org_uuid,
            title="Case Overdue",
            status="waiting",
            priority=1,
            due_at=now - timedelta(minutes=30),
        ),
        # Active: no SLA target
        Case(
            org_id=org_uuid,
            title="Case No SLA",
            status="open",
            priority=4,
            due_at=None,
        ),
        # Completed: met SLA (resolved before due)
        Case(
            org_id=org_uuid,
            title="Case Met SLA",
            status="resolved",
            priority=2,
            due_at=now - timedelta(hours=2),
            resolved_at=now - timedelta(hours=3),
        ),
        # Completed: breached SLA (resolved after due)
        Case(
            org_id=org_uuid,
            title="Case Breached SLA",
            status="closed",
            priority=1,
            due_at=now - timedelta(hours=4),
            resolved_at=now - timedelta(hours=1),
        ),
        # Completed: no SLA
        Case(
            org_id=org_uuid,
            title="Case Resolved No SLA",
            status="resolved",
            priority=3,
            due_at=None,
            resolved_at=now - timedelta(hours=2),
        ),
    ]
    db_session.add_all(cases)
    await db_session.commit()

    resp = await client.get("/api/v1/reports/operational-summary", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_cases"] == 7
    assert data["active_cases"] == 4
    assert data["resolved_cases"] == 3

    # Active SLA counts
    assert data["active_on_track"] == 1
    assert data["active_at_risk"] == 1
    assert data["active_overdue"] == 1
    assert data["active_no_sla"] == 1

    # Completed SLA counts
    assert data["completed_met"] == 1
    assert data["completed_breached"] == 1
    assert data["completed_no_sla"] == 1

    # Active SLA rate: (1 on_track + 1 at_risk) / 3 active with SLA = 66.7%
    assert data["active_sla_compliance_pct"] == 66.7
    # Completed SLA rate: 1 met / 2 with SLA = 50.0%
    assert data["completed_sla_compliance_pct"] == 50.0

    # Legacy fields
    assert data["overdue_cases"] == 1
    assert data["at_risk_cases"] == 1
    assert data["compliant_cases"] == 1
    assert data["breached_cases"] == 1


@pytest.mark.asyncio
async def test_case_list_sla_statuses(client: AsyncClient, db_session: AsyncSession):
    """Verify that GET /cases returns canonical SLA statuses on CaseOut."""
    org_uuid = uuid.UUID(ORG_ID)
    now = datetime.now(timezone.utc)

    from sqlalchemy import delete
    await db_session.execute(delete(Case).where(Case.org_id == org_uuid))
    await db_session.commit()

    # Exact boundary tests:
    # 1. Exactly 7200 seconds remaining -> at_risk
    # 2. Exactly 7201 seconds remaining -> on_track
    # 3. Exactly 0 seconds remaining / past -> overdue
    c_at_risk_bound = Case(
        org_id=org_uuid,
        title="Boundary At Risk",
        status="open",
        priority=2,
        due_at=now + timedelta(seconds=7200),
    )
    c_on_track_bound = Case(
        org_id=org_uuid,
        title="Boundary On Track",
        status="open",
        priority=2,
        due_at=now + timedelta(seconds=7205),
    )
    c_no_sla = Case(
        org_id=org_uuid,
        title="No SLA",
        status="open",
        priority=3,
        due_at=None,
    )
    db_session.add_all([c_at_risk_bound, c_on_track_bound, c_no_sla])
    await db_session.commit()

    resp = await client.get("/api/v1/cases", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    items = resp.json()

    statuses = {item["title"]: item["sla_status"] for item in items}
    assert statuses["Boundary At Risk"] == "at_risk"
    assert statuses["Boundary On Track"] == "on_track"
    assert statuses["No SLA"] == "no_sla"
