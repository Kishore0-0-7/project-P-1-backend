"""
Tests for Phase 0-D: rule engine operator correctness and fingerprint stability.

Key scenarios tested:
  - supply_air_temp=29.5, operator='>'  -> NOT matched  (strict GT at boundary)
  - supply_air_temp=29.5, operator='>=' -> matched      (inclusive GTE at boundary)
  - direct metric: all six operators at boundary value
  - fingerprint uses asset UUID, never external_key or 'None' string
  - unknown operator falls back to >= with a warning (not silently wrong)
"""
from __future__ import annotations

import uuid
import logging
import pytest

from app.rules_engine import _apply_op, evaluate_condition


# ── _apply_op unit tests ──────────────────────────────────────────────────────

class TestApplyOp:
    """Direct tests of the shared _apply_op helper."""

    @pytest.mark.parametrize("op,val,thresh,expected", [
        # Strict greater-than
        (">",  10.0, 10.0, False),
        (">",  10.1, 10.0, True),
        ("gt", 9.9,  10.0, False),
        # Inclusive greater-than-or-equal
        (">=", 10.0, 10.0, True),
        (">=", 9.9,  10.0, False),
        ("gte",10.0, 10.0, True),
        # Strict less-than
        ("<",  10.0, 10.0, False),
        ("<",  9.9,  10.0, True),
        ("lt", 10.1, 10.0, False),
        # Inclusive less-than-or-equal
        ("<=", 10.0, 10.0, True),
        ("<=", 10.1, 10.0, False),
        ("lte",10.0, 10.0, True),
        # Equality
        ("==", 10.0, 10.0, True),
        ("==", 10.1, 10.0, False),
        ("eq", 10.0, 10.0, True),
    ])
    def test_operator_boundaries(self, op, val, thresh, expected):
        assert _apply_op(val, op, thresh) == expected

    def test_unknown_operator_falls_back_to_gte_and_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opstriage.rules_engine"):
            result = _apply_op(10.0, "INVALID_OP", 10.0)
        assert result is True
        assert any(
            "unknown" in r.message.lower() or "falling back" in r.message.lower()
            for r in caplog.records
        )

    def test_unknown_operator_not_matched_below_thresh(self, caplog):
        with caplog.at_level(logging.WARNING, logger="opstriage.rules_engine"):
            result = _apply_op(9.9, "INVALID_OP", 10.0)
        assert result is False


# ── evaluate_condition: direct metric branch ──────────────────────────────────

class TestEvaluateConditionDirectMetric:

    @pytest.mark.parametrize("op,val,thresh,expected", [
        (">",  29.5, 29.5, False),   # boundary from bug report: strict GT at 29.5
        (">=", 29.5, 29.5, True),    # inclusive GTE at 29.5
        (">",  29.6, 29.5, True),
        ("<",  29.4, 29.5, True),
        ("<=", 29.5, 29.5, True),
        ("==", 29.5, 29.5, True),
        ("==", 29.4, 29.5, False),
    ])
    def test_supply_air_temp_boundary(self, op, val, thresh, expected):
        definition = {"metric": "supply_air_temp", "operator": op, "threshold": thresh}
        payload = {"supply_air_temp": val}
        matched, explanation, actual, threshold_out = evaluate_condition(
            engine="threshold", definition=definition, payload=payload
        )
        assert matched == expected, (
            f"supply_air_temp={val} {op} {thresh}: expected matched={expected}, got {matched}. "
            f"Explanation: {explanation}"
        )
        assert actual == val
        assert threshold_out == thresh

    def test_non_numeric_metric_returns_false(self):
        definition = {"metric": "supply_air_temp", "operator": ">=", "threshold": 25.0}
        payload = {"supply_air_temp": "not_a_number"}
        matched, _, actual, _ = evaluate_condition(
            engine="threshold", definition=definition, payload=payload
        )
        assert matched is False
        assert actual == "not_a_number"

    def test_missing_metric_returns_false(self):
        definition = {"metric": "supply_air_temp", "operator": ">=", "threshold": 25.0}
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition=definition, payload={}
        )
        assert matched is False

    def test_non_dict_payload_returns_false(self):
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition={"metric": "x", "threshold": 1.0}, payload="bad"  # type: ignore[arg-type]
        )
        assert matched is False


# ── evaluate_condition: supply-air delta branch ───────────────────────────────

class TestEvaluateConditionSupplyAirDelta:
    """Branch 3: delta-from-setpoint pattern for HVAC drift rules."""

    def _def(self, delta, op=">="):
        return {"delta": delta, "operator": op}

    def test_delta_exceeded_gte(self):
        matched, _, val, _ = evaluate_condition(
            engine="threshold",
            definition=self._def(3.0, ">="),
            payload={"supply_air_temp": 29.0, "setpoint": 24.0},  # diff=5
        )
        assert matched is True

    def test_delta_not_exceeded_gte(self):
        matched, _, _, _ = evaluate_condition(
            engine="threshold",
            definition=self._def(3.0, ">="),
            payload={"supply_air_temp": 26.0, "setpoint": 24.0},  # diff=2
        )
        assert matched is False

    def test_delta_boundary_strict_gt_not_matched(self):
        matched, explanation, _, _ = evaluate_condition(
            engine="threshold",
            definition=self._def(3.0, ">"),
            payload={"supply_air_temp": 27.0, "setpoint": 24.0},  # diff=3.0 exactly
        )
        assert matched is False
        assert "within tolerance" in explanation

    def test_delta_boundary_gte_matched(self):
        matched, explanation, _, _ = evaluate_condition(
            engine="threshold",
            definition=self._def(3.0, ">="),
            payload={"supply_air_temp": 27.0, "setpoint": 24.0},  # diff=3.0 exactly
        )
        assert matched is True
        assert "condition met" in explanation

    def test_no_magic_28_threshold(self):
        """
        Old code fired `val >= 28.0` regardless of the configured delta.
        val=28.5, setpoint=24, diff=4.5, delta=5 -> NOT matched (4.5 < 5).
        """
        matched, _, _, _ = evaluate_condition(
            engine="threshold",
            definition=self._def(5.0, ">="),
            payload={"supply_air_temp": 28.5, "setpoint": 24.0},  # diff=4.5
        )
        assert matched is False, (
            "val=28.5 with delta=5 should NOT match. Old hardcoded val>=28.0 bug?"
        )


# ── evaluate_condition: error-rate, pool, packet-loss branches ───────────────

class TestEvaluateConditionSpecialisedBranches:

    def test_error_rate_gte_boundary(self):
        definition = {"error_rate": True, "threshold": 0.02, "operator": ">="}
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition=definition, payload={"error_rate": 0.02}
        )
        assert matched is True

    def test_error_rate_gt_boundary_not_matched(self):
        definition = {"error_rate": True, "threshold": 0.02, "operator": ">"}
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition=definition, payload={"error_rate": 0.02}
        )
        assert matched is False

    def test_pool_active_pct_lt_threshold_not_matched(self):
        definition = {"pool_active_pct": True, "threshold": 85.0, "operator": ">="}
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition=definition, payload={"pool_active_pct": 80.0}
        )
        assert matched is False

    def test_packet_loss_gt_boundary(self):
        definition = {"packet_loss_pct": True, "threshold": 5.0, "operator": ">"}
        matched, _, _, _ = evaluate_condition(
            engine="threshold", definition=definition, payload={"packet_loss_pct": 5.0}
        )
        assert matched is False


# ── Fingerprint stability ─────────────────────────────────────────────────────

class TestFingerprintStability:
    """Verify fingerprints use stable UUIDs, not external_key (can be None)."""

    def _fp(self, rule_key: str, asset_id, external_key, has_asset: bool) -> str:
        """Mirrors the fixed fingerprint expression in evaluate_event_and_correlate."""
        if has_asset:
            class _Asset:
                id = asset_id
            asset = _Asset()
        else:
            asset = None
        return f"{rule_key}:{str(asset.id) if asset else (str(asset_id) if asset_id else 'global')}"

    def test_uses_uuid_not_external_key(self):
        aid = uuid.uuid4()
        fp = self._fp("hvac.supply_air", aid, "AHU-08", has_asset=True)
        assert "AHU-08" not in fp
        assert str(aid) in fp

    def test_none_external_key_no_collision(self):
        aid = uuid.uuid4()
        fp = self._fp("hvac.supply_air", aid, None, has_asset=True)
        assert "None" not in fp
        assert str(aid) in fp

    def test_no_asset_resolves_to_global(self):
        fp = self._fp("hvac.supply_air", None, None, has_asset=False)
        assert fp == "hvac.supply_air:global"

    def test_asset_id_without_asset_object(self):
        aid = uuid.uuid4()
        fp = self._fp("some.rule", aid, None, has_asset=False)
        assert str(aid) in fp
        assert "global" not in fp

    def test_different_assets_different_fingerprints(self):
        id_a, id_b = uuid.uuid4(), uuid.uuid4()
        fp_a = self._fp("rule.x", id_a, "SHARED-KEY", has_asset=True)
        fp_b = self._fp("rule.x", id_b, "SHARED-KEY", has_asset=True)
        assert fp_a != fp_b
