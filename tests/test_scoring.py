"""
Unit tests for the priority scoring engine.

These tests are pure Python – no DB, no network, no fixtures needed.
They validate that the scoring formula is correct, deterministic, and
maps to the expected priority bands.
"""
from __future__ import annotations

import pytest

from app.scoring import ScoreResult, score_finding, score_incident, _band


# ── _band ─────────────────────────────────────────────────────────────────────

class TestBand:
    def test_critical_at_800(self):
        assert _band(800) == 1

    def test_high_at_600(self):
        assert _band(600) == 2

    def test_medium_at_400(self):
        assert _band(400) == 3

    def test_low_at_200(self):
        assert _band(200) == 4

    def test_info_at_0(self):
        assert _band(0) == 5

    def test_boundary_just_below_critical(self):
        assert _band(799) == 2

    def test_boundary_max(self):
        assert _band(1000) == 1


# ── score_incident ────────────────────────────────────────────────────────────

class TestScoreIncident:
    def test_returns_score_result(self):
        result = score_incident(impact=5, urgency=5, asset_criticality=5)
        assert isinstance(result, ScoreResult)

    def test_max_values_give_priority_1(self):
        result = score_incident(impact=5, urgency=5, asset_criticality=5)
        assert result.priority == 1
        assert result.score == 1000.0

    def test_min_values_give_low_priority(self):
        result = score_incident(impact=1, urgency=1, asset_criticality=1)
        assert result.priority >= 4

    def test_none_impact_uses_default_3(self):
        result_none = score_incident(impact=None, urgency=3, asset_criticality=3)
        result_3 = score_incident(impact=3, urgency=3, asset_criticality=3)
        assert result_none.score == result_3.score

    def test_explanation_contains_factors(self):
        result = score_incident(impact=3, urgency=3, asset_criticality=3)
        assert "factors" in result.explanation
        assert len(result.explanation["factors"]) == 3

    def test_score_is_capped_at_1000(self):
        result = score_incident(impact=10, urgency=10, asset_criticality=10)
        assert result.score <= 1000

    def test_higher_impact_gives_higher_score(self):
        low = score_incident(impact=1, urgency=3, asset_criticality=3)
        high = score_incident(impact=5, urgency=3, asset_criticality=3)
        assert high.score > low.score

    def test_score_deterministic(self):
        r1 = score_incident(impact=2, urgency=3, asset_criticality=4)
        r2 = score_incident(impact=2, urgency=3, asset_criticality=4)
        assert r1.score == r2.score and r1.priority == r2.priority


# ── score_finding ─────────────────────────────────────────────────────────────

class TestScoreFinding:
    def test_critical_severity_gets_priority_1_or_2(self):
        result = score_finding(severity="critical", asset_criticality=5, occurrence_count=1)
        assert result.priority <= 2

    def test_info_severity_gets_low_priority(self):
        result = score_finding(severity="info", asset_criticality=1, occurrence_count=1)
        assert result.priority >= 4

    def test_recurrence_boosts_priority(self):
        single = score_finding(severity="medium", asset_criticality=3, occurrence_count=1)
        repeated = score_finding(severity="medium", asset_criticality=3, occurrence_count=20)
        assert repeated.score >= single.score

    def test_explanation_has_factors(self):
        result = score_finding(severity="high")
        assert "factors" in result.explanation
        assert any(f["label"] == "Severity" for f in result.explanation["factors"])

    def test_invalid_severity_defaults_to_zero_boost(self):
        # Unknown severity → boost of 0
        result = score_finding(severity="unknown_sev")
        assert result.score >= 0

    @pytest.mark.parametrize("sev,expected_max_priority", [
        ("critical", 2),
        ("high", 3),
        ("medium", 4),
        ("low", 5),
        ("info", 5),
    ])
    def test_severity_priority_bounds(self, sev: str, expected_max_priority: int):
        result = score_finding(severity=sev, asset_criticality=1, occurrence_count=1)
        assert result.priority <= expected_max_priority
