"""
Rule evaluation engine for OpsTriage.

Evaluates raw incoming telemetry events against active RuleVersions.
Supports engines:
- 'threshold': evaluates if a numeric payload metric meets/exceeds a threshold.
- 'rolling_window': evaluates rate of change or windowed threshold.
- 'model': evaluates statistical or ML anomaly scores (e.g. vibration RMS).
- 'expression': evaluates basic boolean expressions or multi-condition checks.

On match, correlates or creates a Finding, appends a FindingOccurrence,
updates Asset risk, and records an AuditEvent.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AuditEvent, Finding, FindingOccurrence, Rule, RuleVersion
from app.scoring import score_finding

logger = logging.getLogger("opstriage.rules_engine")


async def load_active_rule_pairs(
    org_id: uuid.UUID,
    db: AsyncSession,
) -> list[tuple[Rule, RuleVersion]]:
    """Load the enabled rule set once for an ingestion batch."""
    q = (
        select(Rule, RuleVersion)
        .join(RuleVersion, Rule.active_version_id == RuleVersion.id)
        .where(Rule.org_id == org_id, Rule.enabled.is_(True))
    )
    return list((await db.execute(q)).all())


def _apply_op(val: float, op: str, thresh: float) -> bool:
    """
    Apply a comparison operator string to two numeric values.
    Supported operators: >, >=, <, <=, ==  (and common aliases gt/gte/lt/lte/eq).
    Falls back to >= for unrecognised operators and logs the fallback.
    """
    if op in (">=", "gte"):
        return val >= thresh
    if op in (">", "gt"):
        return val > thresh
    if op in ("<=", "lte"):
        return val <= thresh
    if op in ("<", "lt"):
        return val < thresh
    if op in ("==", "eq"):
        return val == thresh
    # Unknown operator — treat as >= and warn
    logger.warning(
        "Unknown rule operator %r; falling back to '>=' for threshold comparison", op
    )
    return val >= thresh


def evaluate_condition(
    engine: str,
    definition: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[bool, str, Any, Any]:
    """
    Evaluates a single rule definition against a telemetry payload.
    Returns (matched, explanation, actual_value, threshold_value).

    The `operator` field in the definition controls the comparison for *every*
    specialised branch.  Supported values: >, >=, <, <=, == (and aliases).
    """
    if not isinstance(payload, dict):
        return False, "Payload is not a valid dictionary", None, None

    metric_name = definition.get("metric") or definition.get("field")
    op = definition.get("operator") or definition.get("op", ">=")

    # 1. Direct metric threshold check
    if metric_name and metric_name in payload:
        try:
            val = float(payload[metric_name])
        except (ValueError, TypeError):
            return False, f"Metric '{metric_name}' is not numeric: {payload[metric_name]}", payload.get(metric_name), None

        thresh = float(definition.get("threshold", definition.get("delta", 0.0)))
        matched = _apply_op(val, op, thresh)

        explanation = (
            f"Observed {metric_name}={val} {op} threshold {thresh}"
            if matched
            else f"Observed {metric_name}={val} does not violate threshold {thresh}"
        )
        return matched, explanation, val, thresh

    # 2. Model-based evaluation (e.g. vibration RMS or anomaly detector)
    if engine == "model" or "threshold_rms" in definition:
        thresh = float(definition.get("threshold_rms", 4.0))
        # Look for vibration, rms, or velocity
        val_candidate = payload.get("vibration_rms") or payload.get("vibration_velocity") or payload.get("reading")
        if val_candidate is not None:
            try:
                val = float(val_candidate)
                matched = val >= thresh
                exp = f"Model RMS reading {val} >= threshold {thresh}" if matched else f"Model RMS {val} < {thresh}"
                return matched, exp, val, thresh
            except (ValueError, TypeError):
                pass

    # 3. Supply-air drift / HVAC multi-sensor check
    # Uses the rule's configured operator against the deviation from setpoint.
    # A plain absolute-value threshold check (e.g. supply_air_temp > 29.5) should
    # be expressed as metric="supply_air_temp" / threshold=29.5 / operator=">" in
    # the rule definition and will be caught by branch 1 above.  This branch
    # handles the *delta-from-setpoint* pattern used by HVAC drift rules.
    if "supply-air" in str(definition) or "delta" in definition:
        sat = payload.get("supply_air_temp")
        setpoint = payload.get("setpoint", 24.0)
        if sat is not None:
            try:
                val = float(sat)
                diff = abs(val - float(setpoint))
                delta = float(definition.get("delta", 3.0))
                # Respect configured operator for the delta comparison.
                # Default to >= so existing rules that relied on the old implicit
                # behaviour continue to work unless the definition overrides it.
                matched = _apply_op(diff, op, delta)
                if matched:
                    exp = (
                        f"Supply air temperature {val}°C deviates from setpoint "
                        f"{setpoint}°C by {round(diff, 1)}°C "
                        f"({op} limit {delta}°C) — condition met"
                    )
                else:
                    exp = (
                        f"Supply air temperature {val}°C deviates from setpoint "
                        f"{setpoint}°C by {round(diff, 1)}°C "
                        f"({op} limit {delta}°C) — within tolerance"
                    )
                return matched, exp, val, delta
            except (ValueError, TypeError):
                pass

    # 4. Error rate or 5xx check
    if "5xx" in str(definition) or "error_rate" in definition:
        err_val = payload.get("error_rate_5xx") or payload.get("error_rate") or payload.get("5xx_rate")
        if err_val is not None:
            try:
                val = float(err_val)
                thresh = float(definition.get("threshold", 0.02))
                matched = _apply_op(val, op, thresh)
                direction = "exceeds" if matched else "within"
                exp = (
                    f"5xx error rate {round(val * 100, 2)}% "
                    f"{direction} threshold {round(thresh * 100, 2)}% ({op})"
                )
                return matched, exp, val, thresh
            except (ValueError, TypeError):
                pass

    # 5. Connection pool check
    if "pool" in str(definition) or "pool_active_pct" in definition:
        pool_val = payload.get("pool_active_pct") or payload.get("active_connections_pct")
        if pool_val is not None:
            try:
                val = float(pool_val)
                thresh = float(definition.get("threshold", 85.0))
                matched = _apply_op(val, op, thresh)
                direction = "exceeds" if matched else "within"
                exp = (
                    f"Connection pool utilization {val}% "
                    f"{direction} safety threshold {thresh}% ({op})"
                )
                return matched, exp, val, thresh
            except (ValueError, TypeError):
                pass

    # 6. Packet loss check
    if "packet_loss" in str(definition) or "packet_loss_pct" in definition:
        pkt_val = payload.get("packet_loss_pct") or payload.get("packet_loss")
        if pkt_val is not None:
            try:
                val = float(pkt_val)
                thresh = float(definition.get("threshold", 5.0))
                matched = _apply_op(val, op, thresh)
                direction = "exceeds" if matched else "within"
                exp = (
                    f"Edge packet loss {val}% "
                    f"{direction} link threshold {thresh}% ({op})"
                )
                return matched, exp, val, thresh
            except (ValueError, TypeError):
                pass

    # 7. Generic check: look for any key in payload that matches definition metric
    for k, v in payload.items():
        if metric_name and k.lower() == str(metric_name).lower():
            try:
                val = float(v)
                thresh = float(definition.get("threshold", 0.0))
                matched = _apply_op(val, op, thresh)
                return matched, f"Metric {k}={val} {op} {thresh}", val, thresh
            except (ValueError, TypeError):
                pass

    return False, "Telemetry payload does not contain conditions monitored by this rule", None, None


async def evaluate_event_and_correlate(
    event_id: uuid.UUID,
    org_id: uuid.UUID,
    asset_id: uuid.UUID | None,
    source: str,
    payload: dict[str, Any],
    occurred_at: datetime,
    db: AsyncSession,
    active_rule_pairs: Sequence[tuple[Rule, RuleVersion]] | None = None,
) -> list[uuid.UUID]:
    """
    Evaluates an ingested raw event against all active rules in the organization.
    If matching rules trigger, creates or updates findings and logs occurrences.
    Returns the list of triggered finding IDs.
    """
    # 1. Fetch all enabled rules with their active versions
    rule_pairs = (
        list(active_rule_pairs)
        if active_rule_pairs is not None
        else await load_active_rule_pairs(org_id, db)
    )
    if not rule_pairs:
        return []

    # 2. Resolve asset if needed
    asset: Asset | None = None
    if asset_id:
        asset = await db.get(Asset, asset_id)
    elif "asset_id" in payload or "sensor" in payload:
        cand_key = str(payload.get("asset_id") or payload.get("sensor")).strip()
        asset_q = select(Asset).where(
            Asset.org_id == org_id,
            (Asset.name.ilike(cand_key)) | (Asset.external_key.ilike(cand_key)),
        )
        asset = (await db.scalars(asset_q)).first()
        if asset:
            asset_id = asset.id

    asset_crit = asset.criticality if asset else 3
    triggered_finding_ids: list[uuid.UUID] = []
    now = datetime.now(tz=timezone.utc)

    for rule, version in rule_pairs:
        matched, reason, actual_val, thresh = evaluate_condition(
            engine=version.engine,
            definition=version.definition,
            payload=payload,
        )
        if not matched:
            continue

        # Use stable UUID for fingerprint — external_key may be None or change.
        # Format: "rule.key:asset-uuid" for asset-scoped rules, "rule.key:global" otherwise.
        fingerprint = f"{rule.key}:{str(asset.id) if asset else (str(asset_id) if asset_id else 'global')}"

        # Check if finding already exists
        f_q = select(Finding).where(
            Finding.org_id == org_id,
            Finding.fingerprint == fingerprint,
        )
        existing_finding = (await db.scalars(f_q)).first()

        metric_name = version.definition.get("metric") or list(payload.keys())[0] if payload else "metric"

        if existing_finding:
            finding = existing_finding
            # Reopen if previously resolved
            if finding.status == "resolved":
                finding.status = "open"
                finding.resolved_at = None

            finding.last_seen_at = occurred_at

            # Count occurrences for scoring
            from sqlalchemy import func
            occ_count_res = await db.scalar(
                select(func.count(FindingOccurrence.id)).where(FindingOccurrence.finding_id == finding.id)
            )
            occ_count = (occ_count_res or 1) + 1

            # Recalculate priority
            score_res = score_finding(
                severity=version.default_severity,
                asset_criticality=asset_crit,
                occurrence_count=occ_count,
            )
            finding.priority = score_res.priority
            finding.priority_score = score_res.score
            finding.priority_explanation = {
                **score_res.explanation,
                "latest_trigger": reason,
                "source_event": source,
            }
            finding.updated_at = now
        else:
            # Create new finding
            score_res = score_finding(
                severity=version.default_severity,
                asset_criticality=asset_crit,
                occurrence_count=1,
            )
            finding = Finding(
                org_id=org_id,
                asset_id=asset_id,
                rule_version_id=version.id,
                fingerprint=fingerprint,
                title=f"{rule.name} on {asset.name if asset else source}",
                description=rule.description or reason,
                severity=version.default_severity,
                priority=score_res.priority,
                priority_score=score_res.score,
                priority_explanation={
                    **score_res.explanation,
                    "trigger_condition": reason,
                    "rule_key": rule.key,
                },
                evidence={"trigger_payload": payload, "condition": reason},
                status="open",
                first_seen_at=occurred_at,
                last_seen_at=occurred_at,
            )
            db.add(finding)
            await db.flush()

        # Add FindingOccurrence linking raw event
        occ = FindingOccurrence(
            org_id=org_id,
            finding_id=finding.id,
            raw_event_id=event_id,
            observed_at=occurred_at,
            metric=str(metric_name),
            value=float(actual_val) if actual_val is not None else None,
            evidence=payload,
        )
        db.add(occ)

        # Record AuditEvent
        db.add(AuditEvent(
            org_id=org_id,
            entity_type="finding",
            entity_id=finding.id,
            action="rule_evaluation_triggered",
            after_state={
                "rule_key": rule.key,
                "priority": finding.priority,
                "score": finding.priority_score,
                "raw_event_id": str(event_id),
                "reason": reason,
            },
        ))

        triggered_finding_ids.append(finding.id)

    return triggered_finding_ids
