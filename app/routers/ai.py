"""
AI Router – Triage recommendations, counterfactual SLA simulation, and AFDD evaluation.
"""
from __future__ import annotations

from fastapi import APIRouter, status

from app.ai_triage import (
    analyze_with_ai,
    evaluate_hvac_afdd,
    simulate_sla_counterfactual,
)
from app.schemas import (
    AITriageRequest,
    AITriageResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    FactorOut,
    HvacEvalResponse,
)

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post(
    "/triage",
    response_model=AITriageResponse,
    status_code=status.HTTP_200_OK,
    summary="AI triage analysis & SLA risk prediction",
)
async def ai_triage(body: AITriageRequest) -> AITriageResponse:
    res = analyze_with_ai(
        title=body.title,
        severity=body.severity,
        asset_type=body.asset_type,
        asset_criticality=body.asset_criticality,
        evidence=body.evidence,
        occurrence_count=body.occurrence_count,
        impact=body.impact,
        urgency=body.urgency,
    )
    return AITriageResponse(
        predicted_priority=res.predicted_priority,
        priority_score=res.priority_score,
        sla_breach_probability=res.sla_breach_probability,
        confidence_score=res.confidence_score,
        root_cause_summary=res.root_cause_summary,
        recommended_actions=res.recommended_actions,
        factors=[
            FactorOut(label=f.label, detail=f.detail, contribution=f.contribution)
            for f in res.factors
        ],
        metadata=res.metadata,
    )


@router.post(
    "/simulate-sla",
    response_model=CounterfactualResponse,
    status_code=status.HTTP_200_OK,
    summary="Counterfactual SLA simulation (baseline vs AI triage)",
)
async def simulate_sla(body: CounterfactualRequest) -> CounterfactualResponse:
    res = simulate_sla_counterfactual(
        concurrency_capacity=body.concurrency_capacity,
        count=body.incident_count,
    )

    return CounterfactualResponse(
        total_incidents=res.total_incidents,
        baseline_breaches=res.baseline_breaches,
        baseline_breach_rate_pct=res.baseline_breach_rate_pct,
        baseline_p95_resolution_minutes=res.baseline_p95_resolution_minutes,
        ai_breaches=res.ai_breaches,
        ai_breach_rate_pct=res.ai_breach_rate_pct,
        ai_p95_resolution_minutes=res.ai_p95_resolution_minutes,
        breach_reduction_count=res.breach_reduction_count,
        breach_reduction_pct=res.breach_reduction_pct,
        p95_improvement_minutes=res.p95_improvement_minutes,
        high_priority_sla_compliance_pct=res.high_priority_sla_compliance_pct,
    )


@router.post(
    "/hvac-eval",
    response_model=HvacEvalResponse,
    status_code=status.HTTP_200_OK,
    summary="HVAC Automated Fault Detection and Diagnostics (AFDD) evaluation",
)
async def hvac_eval() -> HvacEvalResponse:
    res = evaluate_hvac_afdd()
    return HvacEvalResponse(
        total_samples=res.total_samples,
        ground_truth_faults=res.ground_truth_faults,
        detected_faults=res.detected_faults,
        true_positives=res.true_positives,
        false_positives=res.false_positives,
        false_negatives=res.false_negatives,
        precision=res.precision,
        recall=res.recall,
        f1_score=res.f1_score,
        early_detection_lead_minutes=res.early_detection_lead_minutes,
    )
