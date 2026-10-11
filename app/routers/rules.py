"""Rules router – rule management, validation, version lifecycle, dry-run, and AI proposal."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db, set_org_context
from app.models import AuditEvent, RawEvent, Rule, RuleVersion
from app.schemas import (
    RuleCreate,
    RuleDryRunMatchItem,
    RuleDryRunRequest,
    RuleDryRunResponse,
    RuleOut,
    RuleProposalRequest,
    RuleProposalResponse,
    RuleVersionCreate,
    RuleVersionOut,
)

logger = logging.getLogger("opstriage.rules")

router = APIRouter(prefix="/rules", tags=["rules"])


def validate_rule_definition(engine: str, definition: dict[str, Any]) -> None:
    """Validates definition structure against the engine's schema."""
    if not isinstance(definition, dict):
        raise HTTPException(status_code=422, detail="Rule definition must be a JSON dictionary.")

    if engine == "threshold":
        metric = definition.get("metric") or definition.get("field")
        if not metric or not isinstance(metric, str):
            raise HTTPException(
                status_code=422,
                detail="Threshold engine requires a valid 'metric' or 'field' string.",
            )
        op = definition.get("operator") or definition.get("op", ">=")
        valid_ops = (">", ">=", "<", "<=", "==", "!=", "gt", "gte", "lt", "lte", "eq", "neq")
        if op not in valid_ops:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported operator '{op}'. Supported operators: >, >=, <, <=, ==, !=",
            )
        thresh = definition.get("threshold", definition.get("delta"))
        if thresh is None or not isinstance(thresh, (int, float)):
            raise HTTPException(
                status_code=422,
                detail="Threshold engine requires a numeric 'threshold' or 'delta' value.",
            )

    elif engine == "rolling_window":
        metric = definition.get("metric") or definition.get("field")
        if not metric or not isinstance(metric, str):
            raise HTTPException(
                status_code=422,
                detail="Rolling window engine requires a valid 'metric' or 'field' string.",
            )
        win = definition.get("window_minutes") or definition.get("window_seconds") or definition.get("window")
        if win is None or not isinstance(win, (int, float)) or win <= 0:
            raise HTTPException(
                status_code=422,
                detail="Rolling window engine requires positive 'window_minutes' or 'window_seconds'.",
            )

    elif engine == "expression":
        expr = definition.get("expression")
        if not expr or not isinstance(expr, str) or not expr.strip():
            raise HTTPException(
                status_code=422,
                detail="Expression engine requires a non-empty 'expression' string.",
            )

    elif engine == "model":
        if not definition.get("model_name") and not definition.get("features") and not definition.get("threshold_rms"):
            raise HTTPException(
                status_code=422,
                detail="Model engine requires 'model_name', 'features', or 'threshold_rms'.",
            )


@router.get("", response_model=list[RuleOut], summary="List rules")
async def list_rules(
    org_id: str = Query(...),
    enabled: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[RuleOut]:
    await set_org_context(db, org_id)
    q = select(Rule).where(Rule.org_id == uuid.UUID(org_id))
    if enabled is not None:
        q = q.where(Rule.enabled == enabled)
    q = q.order_by(Rule.name).limit(limit)
    rows = (await db.scalars(q)).all()
    return [RuleOut.model_validate(r) for r in rows]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED, summary="Create rule")
async def create_rule(body: RuleCreate, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> RuleOut:
    await set_org_context(db, org_id)

    if body.definition is not None:
        validate_rule_definition(body.engine or "threshold", body.definition)

    rule = Rule(
        org_id=uuid.UUID(org_id),
        key=body.key,
        name=body.name,
        description=body.description,
        enabled=body.enabled if body.stage != "draft" else False,
        stage=body.stage,
    )
    db.add(rule)
    await db.flush()

    if body.definition is not None:
        v1 = RuleVersion(
            org_id=uuid.UUID(org_id),
            rule_id=rule.id,
            version=1,
            engine=body.engine or "threshold",
            definition=body.definition,
            default_severity=body.default_severity,
            default_priority=body.default_priority,
            published_at=datetime.now(tz=timezone.utc) if body.stage == "active" else None,
        )
        db.add(v1)
        await db.flush()
        if body.stage == "active":
            rule.active_version_id = v1.id

    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="rule",
        entity_id=rule.id,
        action="rule_created",
        before_state={},
        after_state={"key": rule.key, "name": rule.name, "stage": rule.stage},
    ))

    await db.commit()
    await db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.patch("/{rule_id}/toggle", response_model=RuleOut, summary="Toggle rule enabled/disabled")
async def toggle_rule(rule_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> RuleOut:
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    prev_state = rule.enabled
    new_state = not prev_state

    if new_state and not rule.active_version_id:
        result = await db.execute(
            select(RuleVersion)
            .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
            .order_by(RuleVersion.version.desc())
            .limit(1)
        )
        version = result.scalar_one_or_none()
        if not version:
            version = RuleVersion(
                org_id=uuid.UUID(org_id),
                rule_id=rule.id,
                version=1,
                engine="threshold",
                definition={"metric": "error_rate_pct", "operator": ">=", "threshold": 5.0, "unit": "%", "window_minutes": 5},
                default_severity="medium",
                default_priority=3,
                published_at=datetime.now(tz=timezone.utc),
            )
            db.add(version)
            await db.flush()
        rule.active_version_id = version.id

    rule.enabled = new_state
    rule.stage = "active" if rule.enabled else "paused"

    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="rule",
        entity_id=rule.id,
        action="rule_toggled",
        before_state={"enabled": prev_state, "stage": "paused" if prev_state else "active"},
        after_state={"enabled": rule.enabled, "stage": rule.stage},
    ))
    await db.commit()
    await db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.post("/{rule_id}/versions", response_model=RuleVersionOut, status_code=status.HTTP_201_CREATED, summary="Publish a new rule version")
async def create_rule_version(
    rule_id: uuid.UUID,
    body: RuleVersionCreate,
    activate: bool = Query(True, description="Whether to activate this version immediately"),
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RuleVersionOut:
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    validate_rule_definition(body.engine, body.definition)

    # Get next version number
    result = await db.execute(
        select(RuleVersion.version)
        .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
        .order_by(RuleVersion.version.desc())
        .limit(1)
    )
    last_version = result.scalar_one_or_none() or 0

    version = RuleVersion(
        org_id=uuid.UUID(org_id),
        rule_id=rule_id,
        version=last_version + 1,
        engine=body.engine,
        definition=body.definition,
        default_severity=body.default_severity,
        default_priority=body.default_priority,
        published_at=datetime.now(tz=timezone.utc) if activate else None,
    )
    db.add(version)
    await db.flush()

    if activate:
        prev_v_id = rule.active_version_id
        rule.active_version_id = version.id
        rule.stage = "active"
        rule.enabled = True
        db.add(AuditEvent(
            org_id=uuid.UUID(org_id),
            entity_type="rule",
            entity_id=rule.id,
            action="rule_version_activated",
            before_state={"active_version_id": str(prev_v_id) if prev_v_id else None},
            after_state={"active_version_id": str(version.id), "version": version.version},
        ))

    await db.commit()
    await db.refresh(version)
    return RuleVersionOut.model_validate(version)


@router.post("/{rule_id}/activate", response_model=RuleOut, summary="Activate an existing rule version")
async def activate_rule_version(
    rule_id: uuid.UUID,
    version_id: uuid.UUID | None = Query(None),
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RuleOut:
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    if version_id is None:
        result = await db.execute(
            select(RuleVersion)
            .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
            .order_by(RuleVersion.version.desc())
            .limit(1)
        )
        version = result.scalar_one_or_none()
        if not version:
            version = RuleVersion(
                org_id=uuid.UUID(org_id),
                rule_id=rule.id,
                version=1,
                engine="threshold",
                definition={"metric": "error_rate_pct", "operator": ">=", "threshold": 5.0, "unit": "%", "window_minutes": 5},
                default_severity="medium",
                default_priority=3,
                published_at=datetime.now(tz=timezone.utc),
            )
            db.add(version)
            await db.flush()
    else:
        version = await db.get(RuleVersion, version_id)
        if version is None or version.rule_id != rule_id or str(version.org_id) != org_id:
            raise HTTPException(status_code=404, detail="Rule version not found")

    prev_v_id = rule.active_version_id
    rule.active_version_id = version.id
    rule.stage = "active"
    rule.enabled = True
    if not version.published_at:
        version.published_at = datetime.now(timezone.utc)

    db.add(AuditEvent(
        org_id=uuid.UUID(org_id),
        entity_type="rule",
        entity_id=rule.id,
        action="rule_version_activated",
        before_state={"active_version_id": str(prev_v_id) if prev_v_id else None},
        after_state={"active_version_id": str(version.id), "version": version.version},
    ))
    await db.commit()
    await db.refresh(rule)
    return RuleOut.model_validate(rule)


@router.get("/{rule_id}/versions", response_model=list[RuleVersionOut], summary="List rule versions")
async def list_rule_versions(rule_id: uuid.UUID, org_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> list[RuleVersionOut]:
    await set_org_context(db, org_id)
    q = (
        select(RuleVersion)
        .where(RuleVersion.org_id == uuid.UUID(org_id), RuleVersion.rule_id == rule_id)
        .order_by(RuleVersion.version.desc())
    )
    rows = (await db.scalars(q)).all()
    return [RuleVersionOut.model_validate(r) for r in rows]


@router.post("/{rule_id}/test", summary="Inspect and test an existing rule against a telemetry payload")
async def test_rule_condition(
    rule_id: uuid.UUID,
    payload: dict,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict:
    from app.rules_engine import evaluate_condition
    await set_org_context(db, org_id)
    rule = await db.get(Rule, rule_id)
    if rule is None or str(rule.org_id) != org_id:
        raise HTTPException(status_code=404, detail="Rule not found")

    if not rule.active_version_id:
        raise HTTPException(status_code=400, detail="Rule has no active version published")

    version = await db.get(RuleVersion, rule.active_version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Active rule version not found")

    matched, explanation, actual_val, thresh = evaluate_condition(
        engine=version.engine,
        definition=version.definition,
        payload=payload,
    )

    return {
        "rule_id": str(rule.id),
        "rule_key": rule.key,
        "rule_name": rule.name,
        "engine": version.engine,
        "definition": version.definition,
        "matched": matched,
        "explanation": explanation,
        "actual_value": actual_val,
        "threshold_value": thresh,
        "expected_severity": version.default_severity,
        "expected_priority": version.default_priority,
    }


@router.post("/test", summary="Test an arbitrary rule condition against a sample payload")
async def test_arbitrary_condition(body: dict) -> dict:
    from app.rules_engine import evaluate_condition
    engine = body.get("engine", "threshold")
    definition = body.get("definition", {})
    payload = body.get("payload", {})

    matched, explanation, actual_val, thresh = evaluate_condition(
        engine=engine,
        definition=definition,
        payload=payload,
    )

    return {
        "engine": engine,
        "definition": definition,
        "matched": matched,
        "explanation": explanation,
        "actual_value": actual_val,
        "threshold_value": thresh,
    }


@router.post("/dry-run", response_model=RuleDryRunResponse, summary="Dry-run a rule condition against recent raw events")
async def dry_run_rule(
    body: RuleDryRunRequest,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RuleDryRunResponse:
    from app.rules_engine import evaluate_condition
    await set_org_context(db, org_id)

    engine = body.engine or "threshold"
    definition = body.definition

    if body.rule_id:
        rule = await db.get(Rule, body.rule_id)
        if rule is None or str(rule.org_id) != org_id:
            raise HTTPException(status_code=404, detail="Rule not found")
        if not definition:
            if not rule.active_version_id:
                raise HTTPException(status_code=400, detail="Rule has no active version and no definition was provided")
            version = await db.get(RuleVersion, rule.active_version_id)
            if not version:
                raise HTTPException(status_code=404, detail="Rule version not found")
            engine = version.engine
            definition = version.definition

    if not definition:
        raise HTTPException(status_code=400, detail="A definition or rule_id with active version must be provided")

    validate_rule_definition(engine, definition)

    q = (
        select(RawEvent)
        .where(RawEvent.org_id == uuid.UUID(org_id))
        .order_by(RawEvent.occurred_at.desc())
        .limit(body.sample_limit)
    )
    events = (await db.scalars(q)).all()

    sample_matches: list[RuleDryRunMatchItem] = []
    matched_count = 0

    for ev in events:
        matched, exp, act, thresh = evaluate_condition(
            engine=engine,
            definition=definition,
            payload=ev.payload,
        )
        if matched:
            matched_count += 1
        sample_matches.append(
            RuleDryRunMatchItem(
                event_id=str(ev.id),
                source=ev.source,
                occurred_at=ev.occurred_at,
                matched=matched,
                explanation=exp,
                actual_value=act,
                threshold_value=thresh,
            )
        )

    total = len(events)
    match_rate = round((matched_count / total * 100.0), 1) if total > 0 else 0.0

    return RuleDryRunResponse(
        total_events_evaluated=total,
        matched_count=matched_count,
        match_rate_pct=match_rate,
        engine=engine,
        definition=definition,
        sample_matches=sample_matches,
    )


@router.post("/propose", response_model=RuleProposalResponse, summary="Propose a structured detection rule from natural language")
async def propose_rule(
    body: RuleProposalRequest,
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> RuleProposalResponse:
    await set_org_context(db, org_id)
    prompt_text = body.prompt.strip()

    # Try Gemini if configured and enabled
    if settings.ai_enabled and settings.gemini_api_key:
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=settings.gemini_api_key)
            system_prompt = (
                "You are an expert Reliability & IoT Detection Rule Engineer for OpsTriage. "
                "The user will describe an operational condition or alert need. "
                "You must propose a structured rule in valid JSON matching this schema:\n"
                "{\n"
                '  "key": "domain.metric-slug (e.g. hvac.supply-temp-high)",\n'
                '  "name": "Human readable rule name",\n'
                '  "category": "Domain Category (e.g. HVAC · Thermal or Services · Availability)",\n'
                '  "engine": "threshold" | "rolling_window" | "expression" | "model",\n'
                '  "definition": {"metric": "field_name", "operator": ">", "threshold": 25.0},\n'
                '  "default_severity": "critical" | "high" | "medium" | "low" | "info",\n'
                '  "default_priority": 1-5,\n'
                '  "explanation": "Why these parameters provide high precision and avoid noise"\n'
                "}\n"
                "Return ONLY raw JSON, no markdown formatting."
            )

            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=prompt_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.2,
                    max_output_tokens=600,
                    response_mime_type="application/json",
                ),
            )

            if response.text:
                parsed = json.loads(response.text)
                return RuleProposalResponse(
                    key=str(parsed.get("key", "custom.alert-rule")).lower()[:60],
                    name=str(parsed.get("name", "Custom Alert Rule"))[:120],
                    category=str(parsed.get("category", "Operations · Guardrail"))[:60],
                    engine=parsed.get("engine", "threshold"),
                    definition=parsed.get("definition", {"metric": "value", "operator": ">=", "threshold": 100.0}),
                    default_severity=parsed.get("default_severity", "medium"),
                    default_priority=int(parsed.get("default_priority", 3)),
                    explanation=str(parsed.get("explanation", "Generated by OpsTriage AI rule architect.")),
                )
        except Exception as err:
            logger.warning("Gemini rule proposal generation failed (%s), falling back to heuristics.", err)

    # Deterministic heuristics fallback
    low = prompt_text.lower()
    if "temp" in low or "thermal" in low or "cooling" in low or "hvac" in low:
        # Extract number if present
        m = re.search(r"(\d+(?:\.\d+)?)", prompt_text)
        thresh = float(m.group(1)) if m else 28.0
        return RuleProposalResponse(
            key="hvac.supply-temp-drift",
            name="HVAC Supply Air Temperature Drift",
            category="HVAC · Thermal",
            engine="threshold",
            definition={"metric": "supply_air_temp", "operator": ">=", "threshold": thresh},
            default_severity="high",
            default_priority=2,
            explanation=f"Monitors supply air temperature against setpoint threshold of {thresh}°C to detect cooling loss.",
        )
    elif "5xx" in low or "error" in low or "fail" in low or "http" in low:
        m = re.search(r"(\d+(?:\.\d+)?)", prompt_text)
        thresh = float(m.group(1)) if m else 5.0
        return RuleProposalResponse(
            key="service.5xx-rate-burst",
            name="HTTP 5xx Error Rate Surge",
            category="Services · Reliability",
            engine="threshold",
            definition={"metric": "error_rate_pct", "operator": ">", "threshold": thresh},
            default_severity="critical",
            default_priority=1,
            explanation=f"Triggers urgent P1 alarm when HTTP error rate exceeds {thresh}% across incoming requests.",
        )
    elif "db" in low or "database" in low or "pool" in low or "connection" in low:
        m = re.search(r"(\d+(?:\.\d+)?)", prompt_text)
        thresh = float(m.group(1)) if m else 85.0
        return RuleProposalResponse(
            key="database.pool-saturation",
            name="Database Connection Pool Watermark Saturation",
            category="Database · Capacity",
            engine="threshold",
            definition={"metric": "pool_saturation_pct", "operator": ">=", "threshold": thresh},
            default_severity="high",
            default_priority=2,
            explanation=f"Protects connection exhaustion by alarming when pool utilization hits {thresh}%.",
        )
    elif "packet" in low or "loss" in low or "network" in low or "latency" in low:
        m = re.search(r"(\d+(?:\.\d+)?)", prompt_text)
        thresh = float(m.group(1)) if m else 2.5
        return RuleProposalResponse(
            key="network.packet-loss-spike",
            name="Edge Gateway Packet Retry Spike",
            category="Network · Edge",
            engine="threshold",
            definition={"metric": "packet_loss_pct", "operator": ">=", "threshold": thresh},
            default_severity="high",
            default_priority=2,
            explanation=f"Monitors edge router packet loss to detect upstream trunk degradation at {thresh}%.",
        )
    elif "vibration" in low or "bearing" in low or "motor" in low:
        m = re.search(r"(\d+(?:\.\d+)?)", prompt_text)
        thresh = float(m.group(1)) if m else 4.0
        return RuleProposalResponse(
            key="mechanical.bearing-vibration-rms",
            name="Bearing Vibration RMS Anomaly",
            category="Mechanical · Assets",
            engine="threshold",
            definition={"metric": "vibration_rms", "operator": ">=", "threshold": thresh},
            default_severity="medium",
            default_priority=3,
            explanation=f"Early mechanical warning when compressor RMS vibration velocity crosses {thresh} mm/s.",
        )

    return RuleProposalResponse(
        key="ops.custom-guardrail",
        name="Custom Operational Telemetry Guardrail",
        category="Operations · Telemetry",
        engine="threshold",
        definition={"metric": "value", "operator": ">=", "threshold": 100.0},
        default_severity="medium",
        default_priority=3,
        explanation="Standard threshold evaluation configured for operational telemetry monitoring.",
    )

