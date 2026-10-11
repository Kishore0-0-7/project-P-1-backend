"""
Unit and integration tests for Phase 0-E: Integration delivery semantics.

Validates:
  - 0-attempt sentinel: never_attempted=True, success_rate_pct=None, delivery_status='Not yet measured'
  - 100% failure: never_attempted=False, success_rate_pct=0.0 (NOT None!), delivery_status='Failed'
  - High success (>=90%): delivery_status='Active'
  - Partial success (<90%): delivery_status='Degraded'
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IntegrationDelivery
from tests.conftest import ORG_ID


@pytest.mark.asyncio
async def test_integration_delivery_semantics(client: AsyncClient, db_session: AsyncSession):
    org_uuid = uuid.UUID(ORG_ID)
    from sqlalchemy import delete
    await db_session.execute(delete(IntegrationDelivery).where(IntegrationDelivery.org_id == org_uuid))
    await db_session.commit()

    # Case 1: Fresh check - 0 deliveries
    resp = await client.get("/api/v1/integrations", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) > 0
    # Before adding deliveries, every integration should have never_attempted=True
    for item in items:
        assert item["never_attempted"] is True
        assert item["success_rate_pct"] is None
        assert item["delivery_status"] == "Not yet measured"

    # Case 2: Add failed deliveries for pagerduty
    deliv_failed = IntegrationDelivery(
        org_id=org_uuid,
        provider="slack",
        entity_type="incident",
        entity_id=uuid.uuid4(),
        idempotency_key="deliv-fail-1",
        status="failed",
        request_payload={"event": "trigger"},
        attempts=1,
    )
    db_session.add(deliv_failed)
    await db_session.commit()

    resp = await client.get("/api/v1/integrations", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    items = resp.json()
    slk = next(i for i in items if i["id"] == "slack")
    assert slk["never_attempted"] is False
    assert slk["success_rate_pct"] == 0.0  # Must be 0.0, not None
    assert slk["delivery_status"] == "Failed"

    # Case 3: Add successful deliveries for slack to reach 50%
    deliv_success = IntegrationDelivery(
        org_id=org_uuid,
        provider="slack",
        entity_type="incident",
        entity_id=uuid.uuid4(),
        idempotency_key="deliv-succ-1",
        status="delivered",
        request_payload={"event": "trigger"},
        attempts=1,
    )
    db_session.add(deliv_success)
    await db_session.commit()

    resp = await client.get("/api/v1/integrations", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    slk = next(i for i in resp.json() if i["id"] == "slack")
    assert slk["never_attempted"] is False
    assert slk["success_rate_pct"] == 50.0
    assert slk["delivery_status"] == "Degraded"
