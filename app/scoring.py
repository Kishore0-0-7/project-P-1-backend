"""
Priority scoring engine.

Rules are always opaque to callers – they return a (priority, score, explanation)
triple that is persisted alongside the entity.

Formula (deterministic, no ML yet):
  score = (impact_weight * impact) + (urgency_weight * urgency) +
          (criticality_weight * asset_criticality)
  Normalised to 0-1000 range then mapped to priority band 1-5.

The explanation dict is stored as JSONB so the frontend can render
a rationale breakdown (like the `rationale` array in types.ts).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Band thresholds (inclusive lower bound)
_BANDS = [
    (800, 1),   # critical   → priority 1
    (600, 2),   # high       → priority 2
    (400, 3),   # medium     → priority 3
    (200, 4),   # low        → priority 4
    (0,   5),   # info       → priority 5
]

_SEVERITY_BOOST = {
    "critical": 650,
    "high": 400,
    "medium": 200,
    "low": 50,
    "info": 0,
}


@dataclass(frozen=True)
class ScoreResult:
    priority: int
    score: float
    explanation: dict[str, Any]


def score_incident(
    *,
    impact: int | None,
    urgency: int | None,
    asset_criticality: int = 3,
) -> ScoreResult:
    """
    Compute priority for an Incident.
    impact: 1 (low) – 5 (critical)
    urgency: 1 (low) – 5 (critical)
    asset_criticality: 1-5
    """
    imp = impact or 3
    urg = urgency or 3
    crit = asset_criticality

    raw = (imp * 120) + (urg * 100) + (crit * 60)
    raw = min(raw, 1000)

    priority = _band(raw)
    return ScoreResult(
        priority=priority,
        score=round(raw, 3),
        explanation={
            "factors": [
                {"label": "Impact", "value": imp, "weight": 120, "contribution": imp * 120},
                {"label": "Urgency", "value": urg, "weight": 100, "contribution": urg * 100},
                {"label": "Asset criticality", "value": crit, "weight": 60, "contribution": crit * 60},
            ],
            "raw_score": raw,
            "priority_band": priority,
        },
    )


def score_finding(
    *,
    severity: str,
    asset_criticality: int = 3,
    occurrence_count: int = 1,
) -> ScoreResult:
    """
    Compute priority for a Finding.
    severity: critical / high / medium / low / info
    occurrence_count: how many times this fingerprint has fired
    """
    base = _SEVERITY_BOOST.get(severity, 0)
    recurrence = min(occurrence_count * 20, 200)
    crit_boost = (asset_criticality - 1) * 40
    raw = base + recurrence + crit_boost
    raw = min(raw, 1000)

    priority = _band(raw)
    return ScoreResult(
        priority=priority,
        score=round(raw, 3),
        explanation={
            "factors": [
                {"label": "Severity", "value": severity, "weight": None, "contribution": base},
                {"label": "Recurrence", "value": occurrence_count, "weight": 20, "contribution": recurrence},
                {"label": "Asset criticality", "value": asset_criticality, "weight": 40, "contribution": crit_boost},
            ],
            "raw_score": raw,
            "priority_band": priority,
        },
    )


def _band(score: float) -> int:
    for threshold, priority in _BANDS:
        if score >= threshold:
            return priority
    return 5
