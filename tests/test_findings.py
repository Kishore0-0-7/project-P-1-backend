"""
Integration tests for the Findings API.

Each test uses the HTTPX async client backed by SQLite in-memory.
Tests are independent – state doesn't leak between them due to
per-test session rollback in conftest.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import ORG_ID


# ── helpers ───────────────────────────────────────────────────────────────────

def _finding_payload(**overrides) -> dict:
    base = {
        "fingerprint": f"fp-{uuid.uuid4()}",
        "title": "CPU spike detected",
        "severity": "high",
        "priority": 2,
        "priority_score": 350.0,
        "evidence": {"metric": "cpu_util", "value": 95.3},
    }
    return {**base, **overrides}


# ── list ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_findings_empty(client: AsyncClient):
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_findings_returns_created(client: AsyncClient):
    payload = _finding_payload()
    await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=payload)
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["fingerprint"] == payload["fingerprint"]


@pytest.mark.asyncio
async def test_list_findings_filter_by_severity(client: AsyncClient):
    await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload(severity="critical"))
    await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload(severity="low"))
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID, "severity": "critical"})
    assert resp.status_code == 200
    data = resp.json()
    assert all(f["severity"] == "critical" for f in data)


@pytest.mark.asyncio
async def test_list_findings_pagination(client: AsyncClient):
    for _ in range(5):
        await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID, "limit": 3, "offset": 0})
    assert len(resp.json()) == 3


# ── create ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_finding_success(client: AsyncClient):
    payload = _finding_payload()
    resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["fingerprint"] == payload["fingerprint"]
    assert data["title"] == payload["title"]
    assert "id" in data
    assert "priority_score" in data
    assert "priority_explanation" in data


@pytest.mark.asyncio
async def test_create_finding_auto_rescores_priority(client: AsyncClient):
    """Creating a finding should trigger re-scoring regardless of client-provided priority."""
    payload = _finding_payload(severity="critical", priority=5, priority_score=0)
    resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=payload)
    assert resp.status_code == 201
    # Engine should have bumped priority up from 5
    data = resp.json()
    assert data["priority"] <= 3  # critical should score higher than 5


@pytest.mark.asyncio
async def test_create_finding_missing_required_fields(client: AsyncClient):
    resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json={"title": "Missing fields"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_finding_invalid_severity(client: AsyncClient):
    payload = _finding_payload(severity="ultra_critical")
    resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_finding_priority_out_of_range(client: AsyncClient):
    payload = _finding_payload(priority=0)
    resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=payload)
    assert resp.status_code == 422


# ── detail ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_finding_success(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    resp = await client.get(f"/api/v1/findings/{finding_id}", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    assert resp.json()["id"] == finding_id


@pytest.mark.asyncio
async def test_get_finding_not_found(client: AsyncClient):
    resp = await client.get(f"/api/v1/findings/{uuid.uuid4()}", params={"org_id": ORG_ID})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_finding_wrong_org(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    # Access with a different org_id
    resp = await client.get(f"/api/v1/findings/{finding_id}", params={"org_id": str(uuid.uuid4())})
    assert resp.status_code == 404


# ── update ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_finding_status(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    resp = await client.patch(
        f"/api/v1/findings/{finding_id}",
        params={"org_id": ORG_ID},
        json={"status": "acknowledged"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "acknowledged"
    assert data["acknowledged_at"] is not None


@pytest.mark.asyncio
async def test_update_finding_resolve_sets_resolved_at(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    resp = await client.patch(
        f"/api/v1/findings/{finding_id}",
        params={"org_id": ORG_ID},
        json={"status": "resolved"},
    )
    assert resp.status_code == 200
    assert resp.json()["resolved_at"] is not None


@pytest.mark.asyncio
async def test_update_finding_invalid_status(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    resp = await client.patch(
        f"/api/v1/findings/{finding_id}",
        params={"org_id": ORG_ID},
        json={"status": "nuked"},
    )
    assert resp.status_code == 422


# ── occurrences ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_occurrence_success(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    resp = await client.post(
        f"/api/v1/findings/{finding_id}/occurrences",
        params={"org_id": ORG_ID},
        json={
            "observed_at": datetime.now(tz=timezone.utc).isoformat(),
            "metric": "cpu_util",
            "value": 98.5,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["metric"] == "cpu_util"
    assert data["value"] == 98.5


@pytest.mark.asyncio
async def test_list_occurrences(client: AsyncClient):
    create_resp = await client.post("/api/v1/findings", params={"org_id": ORG_ID}, json=_finding_payload())
    finding_id = create_resp.json()["id"]
    for i in range(3):
        await client.post(
            f"/api/v1/findings/{finding_id}/occurrences",
            params={"org_id": ORG_ID},
            json={"observed_at": datetime.now(tz=timezone.utc).isoformat(), "value": float(i)},
        )
    resp = await client.get(f"/api/v1/findings/{finding_id}/occurrences", params={"org_id": ORG_ID})
    assert resp.status_code == 200
    assert len(resp.json()) == 3


# ── response headers ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_response_has_request_id_header(client: AsyncClient):
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID})
    assert "x-request-id" in resp.headers


@pytest.mark.asyncio
async def test_response_has_process_time_header(client: AsyncClient):
    resp = await client.get("/api/v1/findings", params={"org_id": ORG_ID})
    assert "x-process-time-ms" in resp.headers
    assert float(resp.headers["x-process-time-ms"]) >= 0
