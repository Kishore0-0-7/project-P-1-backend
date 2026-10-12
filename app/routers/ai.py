"""
AI Router – Triage recommendations, counterfactual SLA simulation, and AFDD evaluation.
Maintains persisted benchmark runs with explicit metric definitions and reproducible outputs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_triage import (
    analyze_with_ai,
    evaluate_hvac_afdd,
    simulate_sla_counterfactual,
)
from app.db import get_db
from app.dependencies import AuthenticatedUser, get_current_user
from app.schemas import (
    AIAssistanceResponse,
    AITriageRequest,
    AITriageResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    ExplainSignalRequest,
    FactorOut,
    FollowUpRequest,
    FollowUpResponse,
    HvacEvalResponse,
    InvestigateCaseRequest,
    SummarizeEvidenceRequest,
    SupportChatRequest,
    SupportChatResponse,
)
from app.services.ai_assistant import (
    AIAssistanceError,
    explain_signal,
    follow_up_conversation,
    investigate_case,
    summarize_case_evidence,
    support_chat,
)

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/support-chat", response_model=SupportChatResponse, summary="Authenticated product support chat")
async def support_chat_endpoint(
    body: SupportChatRequest,
    _current_user: AuthenticatedUser = Depends(get_current_user),
) -> SupportChatResponse:
    """Provide product help without reading workspace or tenant data."""
    try:
        return await support_chat(body.question, body.conversation_history)
    except AIAssistanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.detail},
        )

# ── Persisted Benchmark Run Registry ──────────────────────────────────────────
# Persists benchmark runs for auditability and reproducibility verification
BENCHMARK_RUNS: dict[str, dict[str, Any]] = {}

METRIC_DEFINITIONS = {
    "precision": "True Positives / (True Positives + False Positives): Proportion of algorithmic detections that are true faults.",
    "recall": "True Positives / (True Positives + False Negatives): Proportion of actual ground-truth faults successfully flagged.",
    "f1_score": "Harmonic Mean: 2 * (Precision * Recall) / (Precision + Recall), balancing precision and recall under class imbalance.",
    "lead_time_minutes": "Minutes of advance warning provided prior to occupied zone thermal runaway setpoint breach.",
    "baseline_breaches": "SLA breaches occurring under unmitigated FIFO arrival ordering.",
    "ai_breaches": "SLA breaches occurring when dynamic priority score reorders worker queue.",
    "breach_reduction_pct": "Percentage reduction in customer-facing SLA breaches (100% means zero breaches).",
    "mttr_saved_minutes": "Average minutes saved on P95 turnaround via high-priority queue preemption.",
}


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

    run_id = f"SIM-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    record = {
        "run_id": run_id,
        "type": "counterfactual_sla",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "concurrency_capacity": body.concurrency_capacity,
            "incident_count": body.incident_count,
        },
        "outputs": {
            "total_incidents": res.total_incidents,
            "baseline_breaches": res.baseline_breaches,
            "baseline_breach_rate_pct": res.baseline_breach_rate_pct,
            "baseline_p95_resolution_minutes": res.baseline_p95_resolution_minutes,
            "ai_breaches": res.ai_breaches,
            "ai_breach_rate_pct": res.ai_breach_rate_pct,
            "ai_p95_resolution_minutes": res.ai_p95_resolution_minutes,
            "breach_reduction_count": res.breach_reduction_count,
            "breach_reduction_pct": res.breach_reduction_pct,
            "p95_improvement_minutes": res.p95_improvement_minutes,
            "high_priority_sla_compliance_pct": res.high_priority_sla_compliance_pct,
            "preempted_count": res.preempted_count,
            "mttr_saved_minutes": res.mttr_saved_minutes,
        },
        "metric_definitions": METRIC_DEFINITIONS,
    }
    BENCHMARK_RUNS[run_id] = record

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
        preempted_count=res.preempted_count,
        mttr_saved_minutes=res.mttr_saved_minutes,
        run_id=run_id,
        metric_definitions=METRIC_DEFINITIONS,
    )


@router.post(
    "/hvac-eval",
    response_model=HvacEvalResponse,
    status_code=status.HTTP_200_OK,
    summary="HVAC Automated Fault Detection and Diagnostics (AFDD) evaluation",
)
async def hvac_eval() -> HvacEvalResponse:
    res = evaluate_hvac_afdd()

    run_id = f"AFDD-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    record = {
        "run_id": run_id,
        "type": "hvac_afdd",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "inputs": {"observations": 120, "fault_onset_minute": 100.0},
        "outputs": {
            "total_samples": res.total_samples,
            "ground_truth_faults": res.ground_truth_faults,
            "detected_faults": res.detected_faults,
            "true_positives": res.true_positives,
            "false_positives": res.false_positives,
            "false_negatives": res.false_negatives,
            "precision": res.precision,
            "recall": res.recall,
            "f1_score": res.f1_score,
            "early_detection_lead_minutes": res.early_detection_lead_minutes,
        },
        "metric_definitions": METRIC_DEFINITIONS,
    }
    BENCHMARK_RUNS[run_id] = record

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
        lead_time_minutes=res.early_detection_lead_minutes,
        run_id=run_id,
        metric_definitions=METRIC_DEFINITIONS,
    )


@router.get("/benchmarks", summary="List historical persisted benchmark runs")
async def list_benchmark_runs() -> list[dict[str, Any]]:
    return list(reversed(list(BENCHMARK_RUNS.values())))


@router.get("/benchmarks/{run_id}", summary="Inspect a specific persisted benchmark run")
async def get_benchmark_run(run_id: str) -> dict[str, Any]:
    run = BENCHMARK_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Benchmark run not found")
    return run


# ── Production Gemini AI Assistant Operations ─────────────────────────────────

@router.post(
    "/explain-signal",
    response_model=AIAssistanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Explain a selected signal finding in plain English",
)
async def explain_signal_endpoint(
    body: ExplainSignalRequest,
    org_id: str = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIAssistanceResponse:
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid org_id parameter format")

    if current_user.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant access forbidden. You cannot access telemetry from another organization.",
        )

    try:
        return await explain_signal(finding_id=body.finding_id, org_id=org_uuid, db=db)
    except AIAssistanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.detail},
        )


@router.post(
    "/investigate-case",
    response_model=AIAssistanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Recommend investigation steps for a selected operational case",
)
async def investigate_case_endpoint(
    body: InvestigateCaseRequest,
    org_id: str = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIAssistanceResponse:
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid org_id parameter format")

    if current_user.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant access forbidden. You cannot access casework from another organization.",
        )

    try:
        return await investigate_case(case_id=body.case_id, org_id=org_uuid, db=db)
    except AIAssistanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.detail},
        )


@router.post(
    "/summarize-evidence",
    response_model=AIAssistanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Summarize evidence associated with a case",
)
async def summarize_evidence_endpoint(
    body: SummarizeEvidenceRequest,
    org_id: str = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIAssistanceResponse:
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid org_id parameter format")

    if current_user.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant access forbidden. You cannot access casework from another organization.",
        )

    try:
        return await summarize_case_evidence(case_id=body.case_id, org_id=org_uuid, db=db)
    except AIAssistanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.detail},
        )


@router.post(
    "/follow-up",
    response_model=FollowUpResponse,
    status_code=status.HTTP_200_OK,
    summary="Context-aware follow-up conversation for signal or case",
)
async def follow_up_endpoint(
    body: FollowUpRequest,
    org_id: str = Query(...),
    current_user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FollowUpResponse:
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid org_id parameter format")

    if current_user.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-tenant access forbidden. You cannot access telemetry or casework from another organization.",
        )

    try:
        return await follow_up_conversation(
            context_type=body.context_type,
            context_id=body.context_id,
            question=body.question,
            conversation_history=body.conversation_history,
            org_id=org_uuid,
            db=db,
        )
    except AIAssistanceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_code": exc.error_code, "message": exc.detail},
        )


