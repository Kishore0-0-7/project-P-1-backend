"""
Integration tests for Cases, Incidents, Assets, Ingest, and Health APIs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from tests.conftest import ORG_ID


# ═══════════════════════════════════════════════════════════════════════════════
# Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestCases:
    @pytest.mark.asyncio
    async def test_list_cases_empty(self, client: AsyncClient):
        resp = await client.get("/api/v1/cases", params={"org_id": ORG_ID})
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_create_case(self, client: AsyncClient):
        payload = {"title": "Investigate HVAC anomaly", "priority": 2}
        resp = await client.post("/api/v1/cases", params={"org_id": ORG_ID}, json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == payload["title"]
        assert data["status"] == "open"
        assert data["priority"] == 2

    @pytest.mark.asyncio
    async def test_create_case_invalid_priority(self, client: AsyncClient):
        resp = await client.post("/api/v1/cases", params={"org_id": ORG_ID}, json={"title": "Bad", "priority": 0})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_get_case_not_found(self, client: AsyncClient):
        resp = await client.get(f"/api/v1/cases/{uuid.uuid4()}", params={"org_id": ORG_ID})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_update_case_status_to_resolved(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/cases", params={"org_id": ORG_ID},
            json={"title": "Test case", "priority": 3}
        )
        case_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/cases/{case_id}",
            params={"org_id": ORG_ID},
            json={"status": "resolved", "resolution_notes": "Fixed by rebooting."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "resolved"
        assert data["resolved_at"] is not None
        assert data["resolution_notes"] == "Fixed by rebooting."

    @pytest.mark.asyncio
    async def test_update_case_closed_sets_closed_at(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/cases", params={"org_id": ORG_ID}, json={"title": "Close me", "priority": 4}
        )
        case_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/cases/{case_id}", params={"org_id": ORG_ID}, json={"status": "closed"}
        )
        assert resp.status_code == 200
        assert resp.json()["closed_at"] is not None

    @pytest.mark.asyncio
    async def test_filter_cases_by_status(self, client: AsyncClient):
        await client.post("/api/v1/cases", params={"org_id": ORG_ID}, json={"title": "Open case", "priority": 2})
        create_resp = await client.post(
            "/api/v1/cases", params={"org_id": ORG_ID}, json={"title": "Will be in_progress", "priority": 3}
        )
        case_id = create_resp.json()["id"]
        await client.patch(f"/api/v1/cases/{case_id}", params={"org_id": ORG_ID}, json={"status": "in_progress"})
        resp = await client.get("/api/v1/cases", params={"org_id": ORG_ID, "status": "in_progress"})
        assert resp.status_code == 200
        assert all(c["status"] == "in_progress" for c in resp.json())


# ═══════════════════════════════════════════════════════════════════════════════
# Incidents
# ═══════════════════════════════════════════════════════════════════════════════

class TestIncidents:
    def _payload(self, **overrides) -> dict:
        base = {"title": "Server high CPU", "priority": 2, "impact": 3, "urgency": 3}
        return {**base, **overrides}

    @pytest.mark.asyncio
    async def test_create_incident_success(self, client: AsyncClient):
        resp = await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json=self._payload())
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "open"
        assert "priority_score" in data
        assert "priority_explanation" in data

    @pytest.mark.asyncio
    async def test_incident_priority_auto_computed(self, client: AsyncClient):
        """High impact+urgency should auto-compute a higher priority than a low one."""
        high_resp = await client.post(
            "/api/v1/incidents", params={"org_id": ORG_ID},
            json=self._payload(impact=5, urgency=5)
        )
        low_resp = await client.post(
            "/api/v1/incidents", params={"org_id": ORG_ID},
            json=self._payload(impact=1, urgency=1)
        )
        assert high_resp.json()["priority_score"] > low_resp.json()["priority_score"]

    @pytest.mark.asyncio
    async def test_list_incidents_by_priority(self, client: AsyncClient):
        await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json=self._payload(priority=1, impact=5, urgency=5))
        await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json=self._payload(priority=4, impact=1, urgency=1))
        resp = await client.get("/api/v1/incidents", params={"org_id": ORG_ID, "priority": 1})
        assert resp.status_code == 200
        data = resp.json()
        # At least our priority-1 incident exists (others may also score as 1)
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_update_incident_acknowledged(self, client: AsyncClient):
        create_resp = await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json=self._payload())
        incident_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/incidents/{incident_id}", params={"org_id": ORG_ID}, json={"status": "acknowledged"}
        )
        assert resp.status_code == 200
        assert resp.json()["acknowledged_at"] is not None

    @pytest.mark.asyncio
    async def test_update_incident_rescores_on_impact_change(self, client: AsyncClient):
        create_resp = await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json=self._payload(impact=1))
        incident_id = create_resp.json()["id"]
        original_score = create_resp.json()["priority_score"]
        resp = await client.patch(
            f"/api/v1/incidents/{incident_id}", params={"org_id": ORG_ID}, json={"impact": 5}
        )
        assert resp.json()["priority_score"] > original_score

    @pytest.mark.asyncio
    async def test_missing_required_title(self, client: AsyncClient):
        resp = await client.post("/api/v1/incidents", params={"org_id": ORG_ID}, json={"priority": 2})
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# Assets
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssets:
    @pytest.mark.asyncio
    async def test_create_asset(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/assets", params={"org_id": ORG_ID},
            json={"name": "AC Unit 01", "asset_type": "hvac", "criticality": 4}
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "AC Unit 01"
        assert data["criticality"] == 4

    @pytest.mark.asyncio
    async def test_get_asset_not_found(self, client: AsyncClient):
        resp = await client.get(f"/api/v1/assets/{uuid.uuid4()}", params={"org_id": ORG_ID})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_retire_asset(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/assets", params={"org_id": ORG_ID}, json={"name": "Old Unit"}
        )
        asset_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/assets/{asset_id}", params={"org_id": ORG_ID})
        assert resp.status_code == 204
        get_resp = await client.get(f"/api/v1/assets/{asset_id}", params={"org_id": ORG_ID})
        assert get_resp.json()["status"] == "retired"

    @pytest.mark.asyncio
    async def test_update_asset_criticality(self, client: AsyncClient):
        create_resp = await client.post(
            "/api/v1/assets", params={"org_id": ORG_ID},
            json={"name": "Server Room", "criticality": 2}
        )
        asset_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/assets/{asset_id}", params={"org_id": ORG_ID}, json={"criticality": 5}
        )
        assert resp.status_code == 200
        assert resp.json()["criticality"] == 5

    @pytest.mark.asyncio
    async def test_list_assets_filter_by_type(self, client: AsyncClient):
        await client.post("/api/v1/assets", params={"org_id": ORG_ID}, json={"name": "AC-1", "asset_type": "hvac"})
        await client.post("/api/v1/assets", params={"org_id": ORG_ID}, json={"name": "DB-1", "asset_type": "database"})
        resp = await client.get("/api/v1/assets", params={"org_id": ORG_ID, "asset_type": "hvac"})
        assert resp.status_code == 200
        assert all(a["asset_type"] == "hvac" for a in resp.json())


# ═══════════════════════════════════════════════════════════════════════════════
# Ingest
# ═══════════════════════════════════════════════════════════════════════════════

class TestIngest:
    def _event(self, **overrides) -> dict:
        base = {
            "source": "hvac-sensor",
            "event_type": "telemetry",
            "occurred_at": datetime.now(tz=timezone.utc).isoformat(),
            "payload": {"temp_c": 32.1, "pressure_pa": 101325},
        }
        return {**base, **overrides}

    @pytest.mark.asyncio
    async def test_ingest_single_event(self, client: AsyncClient):
        resp = await client.post("/api/v1/ingest/events", params={"org_id": ORG_ID}, json=self._event())
        assert resp.status_code == 202
        data = resp.json()
        assert data["processing_status"] == "pending"
        assert data["source"] == "hvac-sensor"

    @pytest.mark.asyncio
    async def test_ingest_batch(self, client: AsyncClient):
        events = [self._event() for _ in range(10)]
        resp = await client.post("/api/v1/ingest/events/batch", params={"org_id": ORG_ID}, json={"events": events})
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 10
        assert data["rejected"] == 0
        assert len(data["ids"]) == 10

    @pytest.mark.asyncio
    async def test_ingest_batch_too_many_events(self, client: AsyncClient):
        events = [self._event() for _ in range(501)]
        resp = await client.post("/api/v1/ingest/events/batch", params={"org_id": ORG_ID}, json={"events": events})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ingest_batch_empty(self, client: AsyncClient):
        resp = await client.post("/api/v1/ingest/events/batch", params={"org_id": ORG_ID}, json={"events": []})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ingest_event_missing_source(self, client: AsyncClient):
        event = self._event()
        del event["source"]
        resp = await client.post("/api/v1/ingest/events", params={"org_id": ORG_ID}, json=event)
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════════
# Health
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_endpoint_returns_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)
        data = resp.json()
        assert "status" in data
        assert "database" in data
        assert "cache" in data
        assert "version" in data

    @pytest.mark.asyncio
    async def test_health_endpoint_head_method(self, client: AsyncClient):
        resp = await client.head("/health")
        assert resp.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "name" in resp.json()
        assert resp.json()["docs"] == "/docs"

    @pytest.mark.asyncio
    async def test_root_endpoint_head_method(self, client: AsyncClient):
        resp = await client.head("/")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_swagger_ui_endpoint(self, client: AsyncClient):
        resp = await client.get("/docs")
        assert resp.status_code == 200
        assert "swagger-ui" in resp.text.lower() or "html" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_openapi_schema_endpoint(self, client: AsyncClient):
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "openapi" in data
        assert "paths" in data
        assert "/api/v1/findings" in data["paths"]
        assert "/api/v1/ai/triage" in data["paths"]
        assert "/api/v1/ai/simulate-sla" in data["paths"]

    @pytest.mark.asyncio
    async def test_redoc_endpoint(self, client: AsyncClient):
        resp = await client.get("/redoc")
        assert resp.status_code == 200



# ═══════════════════════════════════════════════════════════════════════════════
# Rules
# ═══════════════════════════════════════════════════════════════════════════════

class TestRules:
    @pytest.mark.asyncio
    async def test_create_and_list_rules(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={"key": key, "name": "Test Temperature Guardrail", "description": "Detects high temp", "enabled": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["key"] == key
        rule_id = data["id"]

        # List rules
        resp_list = await client.get("/api/v1/rules", params={"org_id": ORG_ID})
        assert resp_list.status_code == 200
        assert any(r["id"] == rule_id for r in resp_list.json())

        # Toggle rule
        resp_toggle = await client.patch(f"/api/v1/rules/{rule_id}/toggle", params={"org_id": ORG_ID})
        assert resp_toggle.status_code == 200
        assert resp_toggle.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_create_rule_version(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp_rule = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={"key": key, "name": "Test Version Rule", "enabled": True},
        )
        assert resp_rule.status_code == 201
        rule_id = resp_rule.json()["id"]

        resp_ver = await client.post(
            f"/api/v1/rules/{rule_id}/versions",
            params={"org_id": ORG_ID},
            json={
                "engine": "threshold",
                "definition": {"metric": "temp", "op": ">", "threshold": 25.0},
                "default_severity": "critical",
                "default_priority": 1,
            },
        )
        assert resp_ver.status_code == 201
        data = resp_ver.json()
        assert data["version"] == 1
        assert data["engine"] == "threshold"


# ═══════════════════════════════════════════════════════════════════════════════
# Reports
# ═══════════════════════════════════════════════════════════════════════════════

class TestReports:
    @pytest.mark.asyncio
    async def test_get_report_empty(self, client: AsyncClient):
        resp = await client.get("/api/v1/reports", params={"org_id": ORG_ID, "days": 7})
        assert resp.status_code == 200
        data = resp.json()
        assert "generated_at" in data
        assert "sla_summary" in data
        assert "top_anomalous_assets" in data

    @pytest.mark.asyncio
    async def test_get_timeline_endpoint(self, client: AsyncClient):
        resp = await client.get("/api/v1/reports/timeline", params={"org_id": ORG_ID, "hours": 12})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 12
        assert "label" in data[0]
        assert "count" in data[0]

