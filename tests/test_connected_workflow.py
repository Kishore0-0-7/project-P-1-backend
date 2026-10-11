"""
End-to-end integration tests for the connected OpsTriage operational workflow:
1. Telemetry ingestion -> Schema validation -> Error on invalid input (422)
2. Rule condition inspection & testing (/api/v1/rules/{id}/test)
3. Rule evaluation & Finding creation with explainable priority
4. Traceability: Finding -> Triggering Raw Event (/api/v1/findings/{id}/source-event)
5. Case creation -> Assignment -> Cascading Resolution -> Asset Risk Reduction & Audit Trail
6. Integration delivery tracking & health checks (/api/v1/integrations)
7. Persisted benchmark reproducibility (/api/v1/ai/benchmarks)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

WORKFLOW_ORG_ID = str(uuid.uuid4())



@pytest.mark.asyncio
class TestConnectedOperationalWorkflow:
    async def test_invalid_telemetry_returns_actionable_error_and_does_not_inflate(
        self, client: AsyncClient
    ):
        """Invalid telemetry payloads must return 422 with clear details and not be recorded."""
        # 1. Empty source
        resp1 = await client.post(
            "/api/v1/ingest/events",
            params={"org_id": WORKFLOW_ORG_ID},
            json={"source": "   ", "payload": {"temp": 25}},
        )
        assert resp1.status_code == 422
        assert "source" in str(resp1.json()["detail"]).lower()

        # 2. Empty payload
        resp2 = await client.post(
            "/api/v1/ingest/events",
            params={"org_id": WORKFLOW_ORG_ID},
            json={"source": "sensor-1", "payload": {}},
        )
        assert resp2.status_code == 422
        assert "payload" in str(resp2.json()["detail"]).lower()

    async def test_inspect_and_test_rule_condition(self, client: AsyncClient):
        """Rule conditions must be inspectable and testable against sample payloads."""
        # Create rule
        rule_res = await client.post(
            "/api/v1/rules",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "key": "test.temp-surge",
                "name": "Temperature surge guardrail",
                "description": "Fires when temperature >= 30.0C",
            },
        )
        assert rule_res.status_code == 201
        rule_id = rule_res.json()["id"]

        # Publish version
        ver_res = await client.post(
            f"/api/v1/rules/{rule_id}/versions",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "engine": "threshold",
                "definition": {"metric": "temp_c", "threshold": 30.0, "operator": ">="},
                "default_severity": "critical",
                "default_priority": 1,
            },
        )
        assert ver_res.status_code == 201

        # Test with matching payload
        test_matching = await client.post(
            f"/api/v1/rules/{rule_id}/test",
            params={"org_id": WORKFLOW_ORG_ID},
            json={"temp_c": 32.5},
        )
        assert test_matching.status_code == 200
        match_data = test_matching.json()
        assert match_data["matched"] is True
        assert match_data["actual_value"] == 32.5
        assert match_data["threshold_value"] == 30.0
        assert match_data["expected_severity"] == "critical"

        # Test with non-matching payload
        test_non_matching = await client.post(
            f"/api/v1/rules/{rule_id}/test",
            params={"org_id": WORKFLOW_ORG_ID},
            json={"temp_c": 22.0},
        )
        assert test_non_matching.status_code == 200
        assert test_non_matching.json()["matched"] is False

    async def test_full_connected_lifecycle_event_to_case_to_resolution(
        self, client: AsyncClient
    ):
        """
        Connected operational workflow acceptance test:
        1. Create an asset
        2. Create a rule for that asset's metric
        3. Ingest raw telemetry violating the threshold
        4. Verify a finding was created, linked to the raw event, and asset risk increased
        5. Trace finding back to raw event (/findings/{id}/source-event)
        6. Open a case linking the finding
        7. Assign the case to an engineer
        8. Resolve the case
        9. Verify linked finding is resolved and asset health/risk is restored
        """
        # 1. Create asset
        asset_res = await client.post(
            "/api/v1/assets",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "name": "AHU-TEST-99",
                "asset_type": "AHU",
                "criticality": 5,
                "metadata": {"location": "Test Lab"},
            },
        )
        assert asset_res.status_code == 201
        asset_id = asset_res.json()["id"]

        # 2. Create rule
        rule_res = await client.post(
            "/api/v1/rules",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "key": "ahu.overheat",
                "name": "AHU overheat protector",
                "description": "Detects thermal runaway on AHU coils",
            },
        )
        assert rule_res.status_code == 201
        rule_id = rule_res.json()["id"]

        await client.post(
            f"/api/v1/rules/{rule_id}/versions",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "engine": "threshold",
                "definition": {"metric": "supply_temp", "threshold": 29.0},
                "default_severity": "critical",
                "default_priority": 1,
            },
        )

        # 3. Ingest raw telemetry violating rule threshold
        event_payload = {
            "source": "ahu-test-sensor",
            "event_type": "thermal",
            "asset_id": asset_id,
            "payload": {
                "asset_id": "AHU-TEST-99",
                "supply_temp": 31.4,
                "compressor_amps": 17.5,
            },
        }
        ingest_res = await client.post(
            "/api/v1/ingest/events",
            params={"org_id": WORKFLOW_ORG_ID},
            json=event_payload,
        )
        assert ingest_res.status_code == 202
        raw_event_id = ingest_res.json()["id"]

        # 4. Check findings: Finding should exist and be correlated
        findings_res = await client.get(
            "/api/v1/findings",
            params={"org_id": WORKFLOW_ORG_ID, "asset_id": asset_id},
        )
        assert findings_res.status_code == 200
        findings = findings_res.json()
        assert len(findings) >= 1
        finding = findings[0]
        finding_id = finding["id"]
        assert finding["status"] == "open"
        assert finding["priority"] in (1, 2)
        assert finding["priority_score"] > 0
        assert "trigger_condition" in finding["priority_explanation"]

        # Check asset risk increased
        assets_res = await client.get(
            "/api/v1/assets",
            params={"org_id": WORKFLOW_ORG_ID},
        )
        assert assets_res.status_code == 200
        target_asset = next(a for a in assets_res.json() if a["id"] == asset_id)
        assert target_asset["open_findings"] >= 1
        assert target_asset["risk"] > 0
        assert target_asset["health"] in ("critical", "at_risk")

        # 5. Traceability: Verify source-event endpoint links back to raw event
        src_res = await client.get(
            f"/api/v1/findings/{finding_id}/source-event",
            params={"org_id": WORKFLOW_ORG_ID},
        )
        assert src_res.status_code == 200
        src_data = src_res.json()
        assert src_data["raw_event_id"] == raw_event_id
        assert src_data["source"] == "ahu-test-sensor"
        assert src_data["payload"]["supply_temp"] == 31.4

        # 6. Open a case linked to the finding
        case_res = await client.post(
            "/api/v1/cases",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "title": "Mitigate AHU-TEST-99 thermal deviation",
                "priority": 1,
                "finding_ids": [finding_id],
            },
        )
        assert case_res.status_code == 201
        case_id = case_res.json()["id"]
        assert finding_id in case_res.json()["finding_ids"]

        # 7. Assign the case
        test_engineer_id = str(uuid.uuid4())
        assign_res = await client.patch(
            f"/api/v1/cases/{case_id}",
            params={"org_id": WORKFLOW_ORG_ID},
            json={"assigned_to": test_engineer_id},
        )
        assert assign_res.status_code == 200
        assert assign_res.json()["assigned_to"] == test_engineer_id

        # 8. Resolve the case
        resolve_res = await client.patch(
            f"/api/v1/cases/{case_id}",
            params={"org_id": WORKFLOW_ORG_ID},
            json={
                "status": "resolved",
                "resolution_notes": "Cleaned air intake filter and reset expansion valve actuator.",
            },
        )
        assert resolve_res.status_code == 200
        assert resolve_res.json()["status"] == "resolved"
        assert resolve_res.json()["resolved_at"] is not None

        # 9. Verify finding was cascaded to resolved
        finding_check = await client.get(
            f"/api/v1/findings/{finding_id}",
            params={"org_id": WORKFLOW_ORG_ID},
        )
        assert finding_check.status_code == 200
        assert finding_check.json()["status"] == "resolved"
        assert finding_check.json()["resolved_at"] is not None

        # Verify asset health is restored to healthy and risk is 0
        assets_res_after = await client.get(
            "/api/v1/assets",
            params={"org_id": WORKFLOW_ORG_ID},
        )
        target_asset_after = next(a for a in assets_res_after.json() if a["id"] == asset_id)
        assert target_asset_after["open_findings"] == 0
        assert target_asset_after["risk"] == 0
        assert target_asset_after["health"] == "healthy"

    async def test_integration_bridges_and_health_probes(self, client: AsyncClient):
        """Integration bridges report actual delivery metrics and can be tested."""
        # List integrations
        list_res = await client.get("/api/v1/integrations", params={"org_id": WORKFLOW_ORG_ID})
        assert list_res.status_code == 200
        data = list_res.json()
        assert len(data) >= 10
        prom = next(i for i in data if i["id"] == "prometheus")
        assert "status_text" in prom
        assert "events_count" in prom

        # Test integration probe
        test_res = await client.post(
            "/api/v1/integrations/prometheus/test",
            params={"org_id": WORKFLOW_ORG_ID},
        )
        assert test_res.status_code == 200
        assert test_res.json()["status"] == "delivered"
        assert "latency_ms" in test_res.json()

    async def test_persisted_benchmark_runs_and_reproducibility(
        self, client: AsyncClient
    ):
        """Benchmark runs are persisted with explicit definitions and produce consistent results."""
        # Execute SLA simulation
        sim_res = await client.post(
            "/api/v1/ai/simulate-sla",
            json={"concurrency_capacity": 3, "incident_count": 150},
        )
        assert sim_res.status_code == 200
        sim_data = sim_res.json()
        assert "run_id" in sim_data
        run_id = sim_data["run_id"]
        assert sim_data["metric_definitions"]["f1_score"] is not None
        assert sim_data["breach_reduction_pct"] > 0

        # Retrieve persisted run
        get_res = await client.get(f"/api/v1/ai/benchmarks/{run_id}")
        assert get_res.status_code == 200
        run_record = get_res.json()
        assert run_record["run_id"] == run_id
        assert run_record["inputs"]["incident_count"] == 150

        # Re-running with same inputs produces identical reproducible outputs
        sim_res2 = await client.post(
            "/api/v1/ai/simulate-sla",
            json={"concurrency_capacity": 3, "incident_count": 150},
        )
        assert sim_res2.status_code == 200
        sim_data2 = sim_res2.json()
        assert sim_data2["baseline_breaches"] == sim_data["baseline_breaches"]
        assert sim_data2["ai_breaches"] == sim_data["ai_breaches"]
        assert sim_data2["breach_reduction_count"] == sim_data["breach_reduction_count"]
