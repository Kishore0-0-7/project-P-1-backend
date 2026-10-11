"""
Tests for Phase 2: Rule Builder Lifecycle, Schema Validation, Dry-Run, and AI Proposals.
"""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

ORG_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
class TestRuleLifecycleAndValidation:
    """Test rule schema validation, draft vs active lifecycle, and dry runs."""

    async def test_definition_validation_rejects_invalid_operator(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": key,
                "name": "Invalid Op Rule",
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": "BOGUS_OP", "threshold": 25.0},
            },
        )
        assert resp.status_code == 422
        assert "Unsupported operator" in resp.json()["detail"]

    async def test_definition_validation_rejects_missing_threshold(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": key,
                "name": "Missing Thresh Rule",
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": ">"},
            },
        )
        assert resp.status_code == 422
        assert "requires a numeric 'threshold'" in resp.json()["detail"]

    async def test_definition_validation_rejects_non_numeric_threshold(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": key,
                "name": "String Thresh Rule",
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": ">", "threshold": "HIGH"},
            },
        )
        assert resp.status_code == 422

    async def test_create_rule_as_draft(self, client: AsyncClient):
        key = f"rule-{uuid.uuid4().hex[:8]}"
        resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": key,
                "name": "Draft Staged Rule",
                "stage": "draft",
                "engine": "threshold",
                "definition": {"metric": "supply_air_temp", "operator": ">=", "threshold": 28.5},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["stage"] == "draft"
        assert data["enabled"] is False
        assert data["active_version_id"] is None
        rule_id = data["id"]

        # Versions list contains version 1 as unpublished draft
        resp_v = await client.get(f"/api/v1/rules/{rule_id}/versions", params={"org_id": ORG_ID})
        assert resp_v.status_code == 200
        versions = resp_v.json()
        assert len(versions) == 1
        assert versions[0]["published_at"] is None

        # Activate the version
        v_id = versions[0]["id"]
        resp_act = await client.post(
            f"/api/v1/rules/{rule_id}/activate",
            params={"org_id": ORG_ID, "version_id": v_id},
        )
        assert resp_act.status_code == 200
        updated = resp_act.json()
        assert updated["stage"] == "active"
        assert updated["enabled"] is True
        assert updated["active_version_id"] == v_id

    async def test_dry_run_evaluation(self, client: AsyncClient):
        # Ingest a sample event first
        ingest_resp = await client.post(
            "/api/v1/ingest/events",
            params={"org_id": ORG_ID},
            json={
                "source": "bacnet-gateway",
                "event_type": "telemetry",
                "payload": {"supply_air_temp": 30.2, "fan_speed": 100},
            },
        )
        assert ingest_resp.status_code in (200, 202)

        # Dry-run a matching condition
        resp_dry = await client.post(
            "/api/v1/rules/dry-run",
            params={"org_id": ORG_ID},
            json={
                "engine": "threshold",
                "definition": {"metric": "supply_air_temp", "operator": ">=", "threshold": 28.0},
                "sample_limit": 10,
            },
        )
        assert resp_dry.status_code == 200
        data = resp_dry.json()
        assert data["total_events_evaluated"] >= 1
        assert data["matched_count"] >= 1
        assert data["match_rate_pct"] > 0
        assert any(m["matched"] and "supply_air_temp=30.2" in m["explanation"] for m in data["sample_matches"])

    async def test_ai_propose_rule_heuristic(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/rules/propose",
            params={"org_id": ORG_ID},
            json={"prompt": "Alert when cooling supply air temperature exceeds 29.5 degrees Celsius"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "temp" in data["key"] or "hvac" in data["key"]
        assert data["engine"] == "threshold"
        assert "temp" in data["definition"]["metric"]
        assert data["definition"]["threshold"] == 29.5
        assert data["default_severity"] in ("high", "critical", "medium")
        assert len(data["explanation"]) > 0

    async def test_boundary_condition_rolling_window_invalid_duration(self, client: AsyncClient):
        """Rolling window engine requires positive window minutes."""
        for invalid_val in (0, -10):
            resp = await client.post(
                "/api/v1/rules",
                params={"org_id": ORG_ID},
                json={
                    "key": f"rw-{uuid.uuid4().hex[:6]}",
                    "name": "Invalid Rolling Window",
                    "engine": "rolling_window",
                    "definition": {"metric": "temp", "window_minutes": invalid_val},
                },
            )
            assert resp.status_code == 422
            assert "positive 'window_minutes'" in resp.json()["detail"]

    async def test_boundary_condition_expression_empty_or_whitespace(self, client: AsyncClient):
        """Expression engine rejects empty or whitespace-only expressions."""
        for expr in ("", "   ", None):
            resp = await client.post(
                "/api/v1/rules",
                params={"org_id": ORG_ID},
                json={
                    "key": f"expr-{uuid.uuid4().hex[:6]}",
                    "name": "Invalid Expression Rule",
                    "engine": "expression",
                    "definition": {"expression": expr} if expr is not None else {},
                },
            )
            assert resp.status_code == 422
            assert "requires a non-empty 'expression'" in resp.json()["detail"]

    async def test_dry_run_zero_matches_behavior(self, client: AsyncClient):
        """Dry-run returns 0 matches when condition is not satisfied by any recent event."""
        resp = await client.post(
            "/api/v1/rules/dry-run",
            params={"org_id": ORG_ID},
            json={
                "engine": "threshold",
                "definition": {"metric": "non_existent_metric", "operator": ">=", "threshold": 99999.0},
                "sample_limit": 10,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_count"] == 0
        assert data["match_rate_pct"] == 0.0

    async def test_rule_toggle_lifecycle_and_audit(self, client: AsyncClient):
        """Toggling a rule alternates between active and paused and updates stage."""
        rule_resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": f"toggle-{uuid.uuid4().hex[:6]}",
                "name": "Toggle Lifecycle Test",
                "stage": "active",
                "engine": "threshold",
                "definition": {"metric": "cpu", "operator": ">", "threshold": 90.0},
            },
        )
        assert rule_resp.status_code == 201
        rule_id = rule_resp.json()["id"]
        assert rule_resp.json()["enabled"] is True

        # Toggle to paused
        t1 = await client.patch(f"/api/v1/rules/{rule_id}/toggle", params={"org_id": ORG_ID})
        assert t1.status_code == 200
        assert t1.json()["enabled"] is False
        assert t1.json()["stage"] == "paused"

        # Toggle back to active
        t2 = await client.patch(f"/api/v1/rules/{rule_id}/toggle", params={"org_id": ORG_ID})
        assert t2.status_code == 200
        assert t2.json()["enabled"] is True
        assert t2.json()["stage"] == "active"

    async def test_version_history_monotonic_increment(self, client: AsyncClient):
        """Creating new versions increments version counter monotonically and retains history."""
        rule_resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": f"ver-{uuid.uuid4().hex[:6]}",
                "name": "Versioning Rule",
                "stage": "active",
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": ">", "threshold": 25.0},
            },
        )
        rule_id = rule_resp.json()["id"]

        # Publish version 2
        v2_resp = await client.post(
            f"/api/v1/rules/{rule_id}/versions",
            params={"org_id": ORG_ID, "activate": True},
            json={
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": ">", "threshold": 28.0},
                "default_severity": "critical",
                "default_priority": 1,
            },
        )
        assert v2_resp.status_code == 201
        assert v2_resp.json()["version"] == 2
        v2_id = v2_resp.json()["id"]

        # Verify versions list ordering (descending)
        v_list_resp = await client.get(f"/api/v1/rules/{rule_id}/versions", params={"org_id": ORG_ID})
        assert v_list_resp.status_code == 200
        versions = v_list_resp.json()
        assert len(versions) == 2
        assert versions[0]["version"] == 2
        assert versions[1]["version"] == 1

        # Activate version 1 explicitly
        v1_id = versions[1]["id"]
        act_v1 = await client.post(
            f"/api/v1/rules/{rule_id}/activate",
            params={"org_id": ORG_ID, "version_id": v1_id},
        )
        assert act_v1.status_code == 200
        assert act_v1.json()["active_version_id"] == v1_id

    async def test_cross_tenant_isolation_and_permissions(self, client: AsyncClient):
        """Cross-tenant activation or toggle attempts are strictly rejected."""
        other_org = "b0000000-0000-0000-0000-000000000002"

        # Create rule in ORG_ID
        rule_resp = await client.post(
            "/api/v1/rules",
            params={"org_id": ORG_ID},
            json={
                "key": f"sec-{uuid.uuid4().hex[:6]}",
                "name": "Security Isolation Rule",
                "stage": "active",
                "engine": "threshold",
                "definition": {"metric": "temp", "operator": ">", "threshold": 30.0},
            },
        )
        rule_id = rule_resp.json()["id"]

        # Attempt to toggle from other_org
        toggle_other = await client.patch(f"/api/v1/rules/{rule_id}/toggle", params={"org_id": other_org})
        assert toggle_other.status_code == 404

        # Attempt to activate from other_org
        act_other = await client.post(f"/api/v1/rules/{rule_id}/activate", params={"org_id": other_org})
        assert act_other.status_code == 404

        # Attempt to activate with non-existent version ID
        bogus_v = str(uuid.uuid4())
        act_bogus = await client.post(
            f"/api/v1/rules/{rule_id}/activate",
            params={"org_id": ORG_ID, "version_id": bogus_v},
        )
        assert act_bogus.status_code == 404
