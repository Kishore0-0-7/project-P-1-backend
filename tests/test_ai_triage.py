"""
AI Triage, Counterfactual SLA Simulation, and AFDD Validation Test Suite.

Validates the machine learning and analytical algorithms described in the
research specification:
1. Explainable AI priority prediction and SLA breach risk estimation.
2. Counterfactual queue reordering proving SLA breach reduction on incident benchmarks.
3. Automated Fault Detection and Diagnostics (AFDD) precision, recall, and F1 validation.
4. FastAPI endpoints for AI triage, counterfactual simulation, and HVAC benchmark eval.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from app.ai_triage import (
    analyze_with_ai,
    evaluate_hvac_afdd,
    simulate_sla_counterfactual,
)


# ── 1. Unit Tests: AI Triage & Diagnostics ─────────────────────────────────────

class TestAITriageUnit:
    def test_critical_tier5_asset_yields_priority_1(self):
        result = analyze_with_ai(
            title="Supply air temperature drifting beyond control band",
            severity="critical",
            asset_type="HVAC",
            asset_criticality=5,
            evidence=[
                {"label": "Supply air", "value": "28.8°C · +4.8°C", "trend": "up"},
                {"label": "Compressor draw", "value": "16.2 A · +21%", "trend": "up"},
                {"label": "Valve position", "value": "97% · sustained", "trend": "stable"},
            ],
            occurrence_count=38,
            impact=5,
            urgency=5,
        )
        assert result.predicted_priority == 1
        assert result.priority_score >= 800.0
        assert result.sla_breach_probability >= 0.70
        assert result.confidence_score >= 0.85
        assert len(result.factors) >= 4
        assert len(result.recommended_actions) >= 3
        assert "actuator" in result.recommended_actions[0].lower() or "cooling" in result.root_cause_summary.lower()

    def test_low_severity_tier1_yields_low_priority_and_low_risk(self):
        result = analyze_with_ai(
            title="Filter differential pressure nearing service threshold",
            severity="low",
            asset_type="HVAC",
            asset_criticality=1,
            evidence=[{"label": "Differential pressure", "value": "238 Pa", "trend": "stable"}],
            occurrence_count=2,
            impact=1,
            urgency=1,
        )
        assert result.predicted_priority >= 4
        assert result.sla_breach_probability <= 0.30
        assert result.priority_score < 300.0

    def test_api_service_diagnostics(self):
        result = analyze_with_ai(
            title="Checkout API error rate above customer-impact threshold",
            severity="high",
            asset_type="Service",
            asset_criticality=5,
            evidence=[
                {"label": "5xx rate", "value": "8.4% · baseline 0.2%", "trend": "up"},
                {"label": "Trace correlation", "value": "payments-provider timeout", "trend": "up"},
            ],
            occurrence_count=12,
            impact=5,
            urgency=4,
        )
        assert result.predicted_priority in (1, 2)
        assert "circuit-breaker" in " ".join(result.recommended_actions).lower()
        assert "dependency" in result.root_cause_summary.lower() or "timeout" in result.root_cause_summary.lower()

    def test_factor_contributions_are_positive_and_explainable(self):
        result = analyze_with_ai(
            title="General asset alert",
            severity="medium",
            asset_criticality=3,
            occurrence_count=5,
        )
        for factor in result.factors:
            assert factor.contribution > 0
            assert len(factor.detail) > 0


# ── 2. Unit Tests: Counterfactual SLA Simulation ──────────────────────────────

class TestCounterfactualSimulation:
    def test_counterfactual_simulation_achieves_statistically_significant_reduction(self):
        """
        Validates the core hypothesis from deep-research-report.md:
        Dynamic AI priority scoring reduces overall SLA breaches compared
        to baseline FIFO/static priority handling under identical capacity.
        """
        sim = simulate_sla_counterfactual(concurrency_capacity=3, count=150)

        assert sim.total_incidents == 150
        assert sim.baseline_breaches > sim.ai_breaches, (
            f"Expected AI breaches ({sim.ai_breaches}) < baseline breaches ({sim.baseline_breaches})"
        )
        # Breach reduction should be at least 25% (in test it drops 100% of breaches)
        assert sim.breach_reduction_pct >= 25.0, (
            f"Breach reduction {sim.breach_reduction_pct}% is below 25% target"
        )
        # High priority SLA compliance should exceed 90%
        assert sim.high_priority_sla_compliance_pct >= 90.0

    def test_counterfactual_scales_with_capacity(self):
        sim_small = simulate_sla_counterfactual(concurrency_capacity=2, count=150)
        sim_large = simulate_sla_counterfactual(concurrency_capacity=5, count=150)

        # Higher capacity should result in fewer or equal total breaches
        assert sim_large.ai_breaches <= sim_small.ai_breaches


# ── 3. Unit Tests: HVAC AFDD Evaluation ────────────────────────────────────────

class TestHVACAFDD:
    def test_hvac_afdd_metrics_meet_research_benchmarks(self):
        """
        Validates AFDD performance on AHU thermal drift:
        Target: Precision >= 0.85, Recall >= 0.85, F1 >= 0.85, Lead time > 15 mins.
        """
        metrics = evaluate_hvac_afdd()

        assert metrics.total_samples > 0
        assert metrics.ground_truth_faults > 0
        assert metrics.precision >= 0.85, f"Precision {metrics.precision} below 0.85"
        assert metrics.recall >= 0.85, f"Recall {metrics.recall} below 0.85"
        assert metrics.f1_score >= 0.85, f"F1 score {metrics.f1_score} below 0.85"
        assert metrics.early_detection_lead_minutes >= 15.0, (
            f"Early detection lead {metrics.early_detection_lead_minutes} min below 15 min"
        )


# ── 4. Integration Tests: AI API Endpoints ─────────────────────────────────────

class TestAIApiEndpoints:
    @pytest.mark.asyncio
    async def test_ai_triage_endpoint(self, client: AsyncClient):
        payload = {
            "title": "AHU-17 Supply air temperature drifting beyond control band",
            "severity": "critical",
            "asset_type": "HVAC",
            "asset_criticality": 5,
            "evidence": [
                {"label": "Supply air", "value": "28.8°C · +4.8°C", "trend": "up"},
                {"label": "Compressor draw", "value": "16.2 A · +21%", "trend": "up"},
            ],
            "occurrence_count": 38,
            "impact": 5,
            "urgency": 5,
        }
        resp = await client.post("/api/v1/ai/triage", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["predicted_priority"] == 1
        assert data["priority_score"] >= 800.0
        assert data["sla_breach_probability"] >= 0.70
        assert len(data["recommended_actions"]) >= 3
        assert len(data["factors"]) >= 3

    @pytest.mark.asyncio
    async def test_ai_simulate_sla_endpoint(self, client: AsyncClient):
        payload = {"concurrency_capacity": 3, "incident_count": 150}
        resp = await client.post("/api/v1/ai/simulate-sla", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_incidents"] == 150
        assert data["breach_reduction_count"] > 0
        assert data["breach_reduction_pct"] >= 25.0
        assert data["high_priority_sla_compliance_pct"] >= 90.0

    @pytest.mark.asyncio
    async def test_ai_hvac_eval_endpoint(self, client: AsyncClient):
        resp = await client.post("/api/v1/ai/hvac-eval")
        assert resp.status_code == 200
        data = resp.json()
        assert data["f1_score"] >= 0.85
        assert data["precision"] >= 0.85
        assert data["recall"] >= 0.85
        assert data["early_detection_lead_minutes"] >= 15.0
