"""
Reports router – aggregated analytics with Redis caching (5 min TTL).

Queries run directly against PostgreSQL using raw SQL for performance
on large time ranges (avoids ORM overhead for GROUP BY aggregations).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.config import settings
from app.db import get_db, set_org_context
from app.models import Asset, Case, Finding
from app.schemas import (
    OperationalSummaryOut,
    PostureComponentOut,
    PostureReportOut,
    ReportOut,
    SlaBreachRow,
    TopAnomalousAsset,
)

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("", response_model=ReportOut, summary="SLA + anomaly report")
async def get_report(
    org_id: str = Query(...),
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
) -> ReportOut:
    cache_key = f"reports:{org_id}:days={days}"
    cached = await redis.cache_get(cache_key)
    if cached:
        return ReportOut.model_validate(cached)

    await set_org_context(db, org_id)

    bind = db.bind
    dialect_name = bind.dialect.name if bind else ""
    sla_rows: list[SlaBreachRow] = []
    top_assets: list[TopAnomalousAsset] = []

    if dialect_name == "sqlite":
        sla_result = await db.execute(
            text("""
                SELECT
                    priority,
                    COUNT(*) AS total_incidents,
                    SUM(CASE WHEN made_sla = 0 THEN 1 ELSE 0 END) AS breached
                FROM incidents
                WHERE org_id = :org_id
                  AND closed_at IS NOT NULL
                GROUP BY priority
                ORDER BY priority
            """),
            {"org_id": org_id},
        )
        for row in sla_result.fetchall():
            total = row[1] or 0
            breached = row[2] or 0
            sla_rows.append(SlaBreachRow(
                priority=row[0],
                total_incidents=total,
                breached=breached,
                breach_rate_pct=round(100 * breached / total, 2) if total > 0 else 0.0,
                p95_resolution_minutes=None,
            ))

        anomaly_result = await db.execute(
            text("""
                SELECT
                    f.asset_id,
                    COUNT(*) AS high_severity_alerts
                FROM finding_occurrences fo
                JOIN findings f ON f.id = fo.finding_id
                WHERE f.org_id = :org_id
                  AND f.severity IN ('critical', 'high')
                  AND f.asset_id IS NOT NULL
                GROUP BY f.asset_id
                ORDER BY high_severity_alerts DESC
                LIMIT 10
            """),
            {"org_id": org_id},
        )
        for row in anomaly_result.fetchall():
            if row[0]:
                top_assets.append(TopAnomalousAsset(
                    asset_id=uuid.UUID(str(row[0])),
                    high_severity_alerts=row[1],
                    metrics=[],
                ))
    else:
        schema = f"{settings.db_schema}." if settings.db_schema else ""
        # ── SLA breach summary ───────────────────────────────────────────────────
        sla_result = await db.execute(
            text(f"""
                SELECT
                    priority,
                    COUNT(*) AS total_incidents,
                    SUM(CASE WHEN made_sla = false THEN 1 ELSE 0 END) AS breached,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (
                      ORDER BY EXTRACT(EPOCH FROM (closed_at - opened_at)) / 60.0
                    ) AS p95_resolution_minutes
                FROM {schema}incidents
                WHERE org_id = :org_id
                  AND opened_at >= now() - (:days * INTERVAL '1 day')
                  AND closed_at IS NOT NULL
                GROUP BY priority
                ORDER BY priority
            """),
            {"org_id": org_id, "days": days},
        )
        for row in sla_result.fetchall():
            total = row[1] or 0
            breached = row[2] or 0
            sla_rows.append(SlaBreachRow(
                priority=row[0],
                total_incidents=total,
                breached=breached,
                breach_rate_pct=round(100 * breached / total, 2) if total > 0 else 0.0,
                p95_resolution_minutes=round(row[3], 1) if row[3] is not None else None,
            ))

        # ── Top anomalous assets ─────────────────────────────────────────────────
        anomaly_result = await db.execute(
            text(f"""
                SELECT
                    asset_id,
                    COUNT(*) AS high_severity_alerts,
                    ARRAY_AGG(DISTINCT metric) FILTER (WHERE metric IS NOT NULL) AS metrics
                FROM {schema}finding_occurrences fo
                JOIN {schema}findings f ON f.id = fo.finding_id
                WHERE f.org_id = :org_id
                  AND f.severity IN ('critical', 'high')
                  AND fo.observed_at >= now() - (:days * INTERVAL '1 day')
                  AND f.asset_id IS NOT NULL
                GROUP BY asset_id
                ORDER BY high_severity_alerts DESC
                LIMIT 10
            """),
            {"org_id": org_id, "days": days},
        )
        for row in anomaly_result.fetchall():
            if row[0]:
                top_assets.append(TopAnomalousAsset(
                    asset_id=row[0],
                    high_severity_alerts=row[1],
                    metrics=row[2] or [],
                ))


    report = ReportOut(
        generated_at=datetime.now(tz=timezone.utc),
        sla_summary=sla_rows,
        top_anomalous_assets=top_assets,
    )
    await redis.cache_set(cache_key, report.model_dump(mode="json"), ttl=settings.redis_reports_ttl)
    return report


@router.get("/operational-summary", response_model=OperationalSummaryOut, summary="Unified source of truth operational KPIs")
@router.get("/summary", response_model=OperationalSummaryOut, summary="Unified source of truth operational KPIs (alias)")
async def get_operational_summary(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> OperationalSummaryOut:
    await set_org_context(db, org_id)
    org_uuid = uuid.UUID(org_id)
    now = datetime.now(timezone.utc)

    # 1. Cases
    c_q = select(Case).where(Case.org_id == org_uuid)
    all_cases = (await db.scalars(c_q)).all()

    total_cases = len(all_cases)
    active_cases_list = [c for c in all_cases if c.status not in ("resolved", "closed", "cancelled")]
    active_cases = len(active_cases_list)
    unassigned_cases = sum(1 for c in active_cases_list if not c.assigned_to)

    active_on_track = 0
    active_at_risk = 0
    active_overdue = 0
    active_no_sla = 0

    for c in active_cases_list:
        if c.due_at:
            due = c.due_at if c.due_at.tzinfo else c.due_at.replace(tzinfo=timezone.utc)
            rem_sec = (due - now).total_seconds()
            if due < now:
                active_overdue += 1
            elif rem_sec <= 7200:
                active_at_risk += 1
            else:
                active_on_track += 1
        else:
            active_no_sla += 1

    completed_met = 0
    completed_breached = 0
    completed_no_sla = 0
    resolved_cases_list = [c for c in all_cases if c.status in ("resolved", "closed")]
    resolved_cases = len(resolved_cases_list)

    for c in resolved_cases_list:
        if c.due_at:
            due = c.due_at if c.due_at.tzinfo else c.due_at.replace(tzinfo=timezone.utc)
            res_time = c.resolved_at or c.closed_at
            if res_time:
                res_time = res_time if res_time.tzinfo else res_time.replace(tzinfo=timezone.utc)
                if res_time <= due:
                    completed_met += 1
                else:
                    completed_breached += 1
            else:
                completed_met += 1
        else:
            completed_no_sla += 1

    active_with_sla = active_on_track + active_at_risk + active_overdue
    active_sla_compliance_pct = (
        round(((active_on_track + active_at_risk) / active_with_sla) * 100.0, 1)
        if active_with_sla > 0
        else 100.0
    )

    completed_with_sla = completed_met + completed_breached
    completed_sla_compliance_pct = (
        round((completed_met / completed_with_sla) * 100.0, 1)
        if completed_with_sla > 0
        else 100.0
    )

    # Legacy fields
    overdue_cases = active_overdue
    at_risk_cases = active_at_risk
    compliant_cases = active_on_track
    breached_cases = completed_breached
    sla_compliance_pct = active_sla_compliance_pct

    # 2. Findings / Signals
    f_q = select(Finding).where(Finding.org_id == org_uuid, Finding.status.in_(["open", "acknowledged"]))
    active_findings = (await db.scalars(f_q)).all()
    open_signals_count = len(active_findings)
    critical_signals_count = sum(1 for f in active_findings if f.severity == "critical")
    high_signals_count = sum(1 for f in active_findings if f.severity == "high")

    # 3. Assets
    a_q = select(Asset).where(Asset.org_id == org_uuid, Asset.status == "active")
    assets = (await db.scalars(a_q)).all()
    total_assets = len(assets)

    # At-risk assets: assets having active findings with priority <= 2
    at_risk_asset_ids = {f.asset_id for f in active_findings if f.asset_id and f.priority <= 2}
    at_risk_assets = len(at_risk_asset_ids)

    return OperationalSummaryOut(
        generated_at=now,
        org_id=org_uuid,
        total_cases=total_cases,
        active_cases=active_cases,
        unassigned_cases=unassigned_cases,
        overdue_cases=overdue_cases,
        at_risk_cases=at_risk_cases,
        compliant_cases=compliant_cases,
        breached_cases=breached_cases,
        resolved_cases=resolved_cases,
        sla_compliance_pct=sla_compliance_pct,
        active_on_track=active_on_track,
        active_at_risk=active_at_risk,
        active_overdue=active_overdue,
        active_no_sla=active_no_sla,
        completed_met=completed_met,
        completed_breached=completed_breached,
        completed_no_sla=completed_no_sla,
        active_sla_compliance_pct=active_sla_compliance_pct,
        completed_sla_compliance_pct=completed_sla_compliance_pct,
        open_signals_count=open_signals_count,
        critical_signals_count=critical_signals_count,
        high_signals_count=high_signals_count,
        total_assets=total_assets,
        at_risk_assets=at_risk_assets,
    )


@router.get("/posture", response_model=PostureReportOut, summary="System posture score and components")
async def get_system_posture(
    org_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> PostureReportOut:
    await set_org_context(db, org_id)
    org_uuid = uuid.UUID(org_id)
    now = datetime.now(timezone.utc)

    # 1. Query active cases with SLA
    c_q = select(Case).where(
        Case.org_id == org_uuid,
        Case.status.not_in(["resolved", "closed", "cancelled"]),
    )
    active_cases = (await db.scalars(c_q)).all()

    active_overdue = 0
    active_at_risk = 0
    active_with_sla = 0

    for c in active_cases:
        if c.due_at:
            active_with_sla += 1
            due = c.due_at if c.due_at.tzinfo else c.due_at.replace(tzinfo=timezone.utc)
            rem_sec = (due - now).total_seconds()
            if due < now:
                active_overdue += 1
            elif rem_sec <= 7200:
                active_at_risk += 1

    # 2. Query active findings
    f_q = select(Finding).where(
        Finding.org_id == org_uuid,
        Finding.status.in_(["open", "acknowledged"]),
    )
    active_findings = (await db.scalars(f_q)).all()
    critical_signals_count = sum(1 for f in active_findings if f.severity == "critical")
    high_signals_count = sum(1 for f in active_findings if f.severity == "high")

    overdue_ratio = (active_overdue / active_with_sla) if active_with_sla > 0 else 0.0
    overdue_penalty = round(40.0 * overdue_ratio, 1)

    at_risk_ratio = (active_at_risk / active_with_sla) if active_with_sla > 0 else 0.0
    at_risk_penalty = round(25.0 * at_risk_ratio, 1)

    critical_penalty = min(20.0, float(critical_signals_count * 5.0))
    high_penalty = min(15.0, float(high_signals_count * 2.0))

    total_penalty = overdue_penalty + at_risk_penalty + critical_penalty + high_penalty
    score = max(0, min(100, int(round(100.0 - total_penalty))))

    if score >= 90:
        grade = "A - Excellent"
    elif score >= 80:
        grade = "B - Good"
    elif score >= 70:
        grade = "C - Needs Attention"
    elif score >= 60:
        grade = "D - Degraded"
    else:
        grade = "F - Critical Risk"

    components = [
        PostureComponentOut(
            name="Overdue Cases",
            weight=40.0,
            raw_value=float(active_overdue),
            penalty=overdue_penalty,
            description=f"{active_overdue} active case(s) past SLA deadline ({round(overdue_ratio * 100, 1)}% of SLA-tracked cases)",
        ),
        PostureComponentOut(
            name="At-Risk Cases",
            weight=25.0,
            raw_value=float(active_at_risk),
            penalty=at_risk_penalty,
            description=f"{active_at_risk} active case(s) within 2 hours of SLA deadline ({round(at_risk_ratio * 100, 1)}% of SLA-tracked cases)",
        ),
        PostureComponentOut(
            name="Critical Signals Density",
            weight=20.0,
            raw_value=float(critical_signals_count),
            penalty=critical_penalty,
            description=f"{critical_signals_count} unhandled critical operational finding(s)",
        ),
        PostureComponentOut(
            name="High Signals Density",
            weight=15.0,
            raw_value=float(high_signals_count),
            penalty=high_penalty,
            description=f"{high_signals_count} unhandled high severity operational finding(s)",
        ),
    ]

    return PostureReportOut(
        score=score,
        grade=grade,
        definition="System posture is an operational health index (0-100, higher is better) measuring active SLA compliance risk and active critical/high severity signals across monitored assets.",
        components=components,
        calculated_at=now,
    )


@router.get("/timeline", summary="Signal intake volume timeline")
async def get_intake_timeline(
    org_id: str = Query(...),
    hours: int = Query(12, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    await set_org_context(db, org_id)
    bind = db.bind
    dialect_name = bind.dialect.name if bind else ""
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=hours)).isoformat()
    window_end = now.isoformat()
    schema = f"{settings.db_schema}." if settings.db_schema else ""

    latest_event_time: str | None = None
    try:
        latest_q = (
            text(f"SELECT MAX(observed_at) FROM {schema}finding_occurrences WHERE org_id = :org_id")
            if dialect_name != "sqlite"
            else text("SELECT MAX(observed_at) FROM finding_occurrences WHERE org_id = :org_id")
        )
        l_res = await db.execute(latest_q, {"org_id": org_id})
        l_row = l_res.fetchone()
        if l_row and l_row[0]:
            latest_event_time = l_row[0].isoformat() if hasattr(l_row[0], "isoformat") else str(l_row[0])
    except Exception:
        pass

    # Generate continuous hourly slots
    slots: dict[str, dict[str, Any]] = {}
    for i in range(hours - 1, -1, -1):
        slot_time = now - timedelta(hours=i)
        key = slot_time.strftime("%Y-%m-%d %H:00")
        label = slot_time.strftime("%H:00")
        slots[key] = {
            "timestamp": slot_time.isoformat(),
            "label": label,
            "count": 0,
            "window_start": window_start,
            "window_end": window_end,
            "last_event_at": latest_event_time,
        }
    if dialect_name == "sqlite":
        res = await db.execute(
            text("""
                SELECT
                    strftime('%Y-%m-%d %H:00', observed_at) AS bucket,
                    COUNT(*) AS cnt
                FROM finding_occurrences
                WHERE org_id = :org_id
                  AND observed_at >= datetime('now', '-' || :hours || ' hours')
                GROUP BY bucket
            """),
            {"org_id": org_id, "hours": hours},
        )
    else:
        res = await db.execute(
            text(f"""
                SELECT
                    to_char(date_trunc('hour', observed_at), 'YYYY-MM-DD HH24:00') AS bucket,
                    COUNT(*) AS cnt
                FROM {schema}finding_occurrences
                WHERE org_id = :org_id
                  AND observed_at >= now() - (:hours * INTERVAL '1 hour')
                GROUP BY bucket
            """),
            {"org_id": org_id, "hours": hours},
        )

    found_any = False
    for row in res.fetchall():
        b_key = str(row[0]).strip()
        cnt = int(row[1] or 0)
        if b_key in slots:
            slots[b_key]["count"] = cnt
            found_any = True
        else:
            for s_key in slots:
                if s_key[:13] == b_key[:13]:
                    slots[s_key]["count"] += cnt
                    found_any = True
                    break

    if not found_any:
        if dialect_name == "sqlite":
            ev_res = await db.execute(
                text("""
                    SELECT
                        strftime('%Y-%m-%d %H:00', occurred_at) AS bucket,
                        COUNT(*) AS cnt
                    FROM raw_events
                    WHERE org_id = :org_id
                      AND occurred_at >= datetime('now', '-' || :hours || ' hours')
                    GROUP BY bucket
                """),
                {"org_id": org_id, "hours": hours},
            )
        else:
            ev_res = await db.execute(
                text(f"""
                    SELECT
                        to_char(date_trunc('hour', occurred_at), 'YYYY-MM-DD HH24:00') AS bucket,
                        COUNT(*) AS cnt
                    FROM {schema}raw_events
                    WHERE org_id = :org_id
                      AND occurred_at >= now() - (:hours * INTERVAL '1 hour')
                    GROUP BY bucket
                """),
                {"org_id": org_id, "hours": hours},
            )
        for row in ev_res.fetchall():
            b_key = str(row[0]).strip()
            cnt = int(row[1] or 0)
            if b_key in slots:
                slots[b_key]["count"] = cnt
            else:
                for s_key in slots:
                    if s_key[:13] == b_key[:13]:
                        slots[s_key]["count"] += cnt
                        break

    return list(slots.values())
