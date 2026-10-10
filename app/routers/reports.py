"""
Reports router – aggregated analytics with Redis caching (5 min TTL).

Queries run directly against PostgreSQL using raw SQL for performance
on large time ranges (avoids ORM overhead for GROUP BY aggregations).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import cache as redis
from app.config import settings
from app.db import get_db, set_org_context
from app.schemas import ReportOut, SlaBreachRow, TopAnomalousAsset

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
