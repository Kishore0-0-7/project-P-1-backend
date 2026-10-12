"""
Integration tests for Cases, Incidents, Assets, Ingest, and Health APIs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import ORG_ID
from app.models import Case, Organization, OrganizationMembership, User


# ═══════════════════════════════════════════════════════════════════════════════
# Cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestCases:
    @pytest.mark.asyncio
    async def test_assign_and_unassign_case_owner(self, client: AsyncClient, db_session: AsyncSession):
        org_id = uuid.uuid4()
        org_id_value = str(org_id)
        user_id = uuid.uuid4()
        db_session.add(Organization(id=org_id, slug=f"test-{org_id.hex[:8]}", name="Test Org", settings={}))
        db_session.add(User(id=user_id, external_subject=f"local|{user_id}", email="owner@example.test", display_name="Case Owner"))
        db_session.add(OrganizationMembership(org_id=org_id, user_id=user_id, role="operator"))
        await db_session.flush()

        created = await client.post("/api/v1/cases", params={"org_id": org_id_value}, json={"title": "Assign me", "priority": 2})
        case_id = created.json()["id"]
        assigned = await client.patch(
            f"/api/v1/cases/{case_id}", params={"org_id": org_id_value}, json={"assigned_to": str(user_id)}
        )
        assert assigned.status_code == 200
        assert assigned.json()["assigned_to"] == str(user_id)
        assert assigned.json()["assignee_name"] == "Case Owner"

        unassigned = await client.patch(
            f"/api/v1/cases/{case_id}", params={"org_id": org_id_value}, json={"assigned_to": None}
        )
        assert unassigned.status_code == 200
        assert unassigned.json()["assigned_to"] is None

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
        assert data["processing_status"] == "processed"
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
        assert data["processed"] == 10
        assert data["failed"] == 0

    @pytest.mark.asyncio
    async def test_ingest_batch_validates_all_events_before_persisting(self, client: AsyncClient):
        before = await client.get("/api/v1/ingest/metrics", params={"org_id": ORG_ID})
        assert before.status_code == 200
        existing_count = before.json()["total_events"]
        resp = await client.post(
            "/api/v1/ingest/events/batch", params={"org_id": ORG_ID},
            json={"events": [self._event(), self._event(payload={})]},
        )
        assert resp.status_code == 422
        metrics = await client.get("/api/v1/ingest/metrics", params={"org_id": ORG_ID})
        assert metrics.status_code == 200
        assert metrics.json()["total_events"] == existing_count

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
    async def test_rule_list_returns_measured_resolution_rate_and_version(self, client: AsyncClient):
        key = f"rule-metric-{uuid.uuid4().hex[:8]}"
        rule_res = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": key,
                "name": "Measured Resolution Rule",
                "engine": "threshold",
                "definition": {"metric": "temp_c", "operator": ">=", "threshold": 30},
            },
        )
        assert rule_res.status_code == 201
        rule = rule_res.json()
        assert rule["engine"] == "threshold"
        assert rule["version"] == 1

        finding_ids = []
        for index in range(2):
            finding_res = await client.post(
                "/api/v1/findings",
                params={"org_id": ORG_ID},
                json={
                    "fingerprint": f"{key}-{index}",
                    "title": f"Measured finding {index}",
                    "severity": "high",
                    "priority": 3,
                    "priority_score": 500,
                    "rule_version_id": rule["active_version_id"],
                },
            )
            assert finding_res.status_code == 201
            finding_ids.append(finding_res.json()["id"])

        resolved_res = await client.patch(
            f"/api/v1/findings/{finding_ids[0]}",
            params={"org_id": ORG_ID},
            json={"status": "resolved"},
        )
        assert resolved_res.status_code == 200

        listed = await client.get("/api/v1/rules", params={"org_id": ORG_ID})
        assert listed.status_code == 200
        measured = next(item for item in listed.json() if item["id"] == rule["id"])
        assert measured["findings"] == 2
        assert measured["resolved_findings"] == 1
        assert measured["effectiveness"] == 50.0
        assert measured["engine"] == "threshold"
        assert measured["version"] == 1

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

