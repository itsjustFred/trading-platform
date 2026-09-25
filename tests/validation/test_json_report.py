"""Tests for validation.reporting.json_report gate wiring and stat comparison.

Covers:
- DV-B33: ``TradeComparisonConfig`` must be importable from the module's runtime
  annotations (public signatures name it; a missing import is a latent NameError).
- DV-B34: infinity handling must be symmetric for ``+inf`` and ``-inf``.
- DV-B35: stat gates must require every computed tolerance to hold (AND, not OR).
"""

from __future__ import annotations

import json
import typing

import pytest

from tests.validation.data_factory import (
    make_passing_risk_evidence,
    make_reference,
    make_trade,
)
from validation.reporting import json_report
from validation.reporting.json_report import (
    StatDelta,
    TradeStatistics,
    _compare_stats,
    _stats_from_values,
    generate_json_report,
)


def _delta(deltas: list[StatDelta], field: str) -> StatDelta:
    return next(d for d in deltas if d.field == field)


class TestTradeComparisonConfigWiring:
    def test_public_signatures_resolve_trade_comparison_config(self) -> None:
        """DV-B33: the annotation must resolve at runtime (no latent NameError)."""
        hints = typing.get_type_hints(json_report.generate_json_report)
        assert "trade_comparison_config" in hints
        assert hints["trade_comparison_config"] is not None

        md_hints = typing.get_type_hints(json_report.generate_markdown_report)
        assert "trade_comparison_config" in md_hints

    def test_builder_signature_resolves_trade_comparison_config(self) -> None:
        hints = typing.get_type_hints(json_report._build_trade_delta_report)
        assert "trade_comparison_config" in hints


class TestInfinitySymmetry:
    def test_negative_infinity_matches_itself(self) -> None:
        """DV-B34: two identical -inf values must not be reported as a mismatch."""
        expected = TradeStatistics(profit_factor=float("-inf"))
        actual = TradeStatistics(profit_factor=float("-inf"))

        deltas = _compare_stats(expected, actual)
        pf = _delta(deltas, "profit_factor")

        assert pf.passed is True
        assert pf.absolute_diff == 0.0
        assert pf.relative_diff == 0.0

    def test_negative_infinity_recovery_factor_matches_itself(self) -> None:
        expected = TradeStatistics(recovery_factor=float("-inf"))
        actual = TradeStatistics(recovery_factor=float("-inf"))

        pf = _delta(_compare_stats(expected, actual), "recovery_factor")
        assert pf.passed is True

    def test_positive_and_negative_infinity_mismatch(self) -> None:
        expected = TradeStatistics(profit_factor=float("inf"))
        actual = TradeStatistics(profit_factor=float("-inf"))

        pf = _delta(_compare_stats(expected, actual), "profit_factor")
        assert pf.passed is False
        assert pf.explanation != ""

    def test_negative_infinity_vs_finite_mismatch(self) -> None:
        expected = TradeStatistics(profit_factor=float("-inf"))
        actual = TradeStatistics(profit_factor=1.5)

        pf = _delta(_compare_stats(expected, actual), "profit_factor")
        assert pf.passed is False


class TestAndToleranceGate:
    def test_pct_diff_of_one_is_not_rescued_by_absolute_tolerance(self) -> None:
        """DV-B35: abs tolerance must not absorb a completely wrong value."""
        expected = TradeStatistics(net_profit=0.0)
        actual = TradeStatistics(net_profit=0.001)

        net = _delta(_compare_stats(expected, actual), "net_profit")
        assert net.passed is False

    def test_gate_requires_all_tolerances_to_hold(self) -> None:
        """A value inside rel tolerance but outside abs tolerance must fail."""
        expected = TradeStatistics(net_profit=1_000_000.0)
        actual = TradeStatistics(net_profit=1_000_000.5)
        # abs diff 0.5 > 0.01 abs tol, rel diff 5e-7 < 1e-3 rel tol
        net = _delta(_compare_stats(expected, actual), "net_profit")
        assert net.passed is False

    def test_gate_passes_when_all_tolerances_hold(self) -> None:
        expected = TradeStatistics(net_profit=100.0)
        actual = TradeStatistics(net_profit=100.005)
        # abs diff 0.005 <= 0.01 AND rel diff 5e-5 <= 1e-3
        net = _delta(_compare_stats(expected, actual), "net_profit")
        assert net.passed is True

    def test_win_rate_scale_free_gate_not_weakened(self) -> None:
        expected = TradeStatistics(win_rate=0.5)
        actual = TradeStatistics(win_rate=0.75)

        wr = _delta(_compare_stats(expected, actual), "win_rate")
        assert wr.passed is False


class TestCanonicalStats:
    def test_mt5_percentage_fields_are_normalized_to_fractions(self) -> None:
        stats = _stats_from_values(
            {"win_rate": 45.12, "max_drawdown_pct": 5.52},
            source_framework="mt5_xlsx",
        )
        assert stats.win_rate == pytest.approx(0.4512)
        assert stats.max_drawdown_pct == pytest.approx(0.0552)


class TestRiskEvidenceGate:
    def test_empty_statistics_evidence_fails_report(self) -> None:
        reference = make_reference([])
        data = json.loads(
            json_report.generate_json_report(
                reference,
                reference,
                risk_delta_report=make_passing_risk_evidence(),
            )
        )
        assert data["stat_passed"] is False
        assert any(
            delta["field"] == "statistics_evidence"
            for delta in data["stat_deltas"]
        )

    def test_missing_risk_evidence_fails_report(self) -> None:
        reference = make_reference([make_trade()])
        data = json.loads(json_report.generate_json_report(reference, reference))
        assert data["passed"] is False
        assert data["risk_passed"] is False
        assert data["risk_evidence_status"] == "missing"

    def test_empty_risk_evidence_fails_report(self) -> None:
        reference = make_reference([make_trade()])
        data = json.loads(
            json_report.generate_json_report(reference, reference, risk_delta_report=[])
        )
        assert data["passed"] is False
        assert data["risk_evidence_status"] == "empty"

    def test_failed_risk_evidence_fails_report(self) -> None:
        reference = make_reference([make_trade()])
        risk = make_passing_risk_evidence()
        risk[0].passed = False
        data = json.loads(
            json_report.generate_json_report(
                reference, reference, risk_delta_report=risk
            )
        )
        assert data["passed"] is False
        assert data["risk_evidence_status"] == "failed"


class TestStatDeltaDefaults:
    def test_infinite_match_explanation_names_the_sign(self) -> None:
        expected = TradeStatistics(profit_factor=float("-inf"))
        actual = TradeStatistics(profit_factor=float("-inf"))

        pf = _delta(_compare_stats(expected, actual), "profit_factor")
        assert "both infinite" in pf.explanation

    @pytest.mark.parametrize("field", ["win_rate", "net_profit", "sharpe_ratio"])
    def test_missing_fields_still_compared(self, field: str) -> None:
        deltas = _compare_stats(TradeStatistics(), TradeStatistics())
        assert field in {d.field for d in deltas}


class TestCanonicalStatsPath:
    def test_stored_platform_stats_never_preferred(self) -> None:
        """C4: single canonical computation path — recompute from trades.

        A platform result with empty trades but curated stored stats must
        compare recomputed zeros, never the stored numbers.
        """
        import json

        from shared.models import Trade, TradeExit
        from tests.validation.data_factory import make_reference
        from validation.reference import ValidationReference

        winning = Trade(
            id=1,
            symbol="EURUSD",
            direction="LONG",
            entry_time="2024-01-01T10:00:00Z",
            entry_price=1.1000,
            quantity=1000.0,
            exits=[
                TradeExit(
                    time="2024-01-01T11:00:00Z",
                    price=1.1050,
                    quantity=1000.0,
                    pnl=50.0,
                )
            ],
            pnl=50.0,
        )
        ref = make_reference([winning])
        platform = ValidationReference(
            strategy_name="test_strategy",
            source_framework="unknown",
            symbol="EURUSD",
            timeframe="1h",
            settings={},
            trades=[],
            stats={"net_profit": 999999.0},
            raw_metadata={},
            allow_empty=True,
        )
        data = json.loads(generate_json_report(ref, platform))
        net = _delta(
            [StatDelta(**d) for d in data["stat_deltas"]],
            "net_profit",
        )
        assert net.actual == 0.0
        assert net.passed is False
