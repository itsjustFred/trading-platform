"""Tests for validation.risk.comparison."""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.comparison.delta_report import TradeDeltaReport
from validation.reference import ValidationReference
from validation.risk.comparison import (
    RiskComparator,
    RiskComparisonConfig,
    RiskComparisonReport,
    RiskComparisonResult,
    RiskReportBuilder,
)
from validation.risk.fixed_risk import FixedRiskConfig
from validation.risk.models import SizingModel, TradeRiskProfile


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 100.0,
    stop_loss: float | None = 90.0,
    quantity: float = 1.0,
    pnl: float = 0.0,
) -> Trade:
    meta: dict[str, object] = {}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=entry_price, quantity=quantity)],
        pnl=pnl,
        meta=meta,
    )


def _make_reference(trades: list[Trade]) -> ValidationReference:
    return ValidationReference(
        strategy_name="test",
        source_framework="unknown",
        symbol="EURUSD",
        timeframe="1h",
        settings={},
        trades=trades,
        stats={},
        raw_metadata={},
        allow_empty=True,
    )


def _make_config(**kwargs: object) -> RiskComparisonConfig:
    defaults: dict[str, object] = {
        "account_balance": 10000.0,
        "sizing_model": SizingModel.FIXED_RISK,
        "model_config": FixedRiskConfig(risk_amount=100.0, max_position_size=5.0),
        "stop_distance_pct": 0.01,
        "risk_per_trade_pct": 0.01,
        "position_size_pct": 0.01,
        "time_tolerance_seconds": 0,
        "match_symbol": True,
    }
    defaults.update(kwargs)
    return RiskComparisonConfig(**defaults)


class TestPerfectMatch:
    def test_identical_trades_match(self) -> None:
        t = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        ref1 = _make_reference([t])
        ref2 = _make_reference([_make_trade(trade_id=2, entry_price=100.0, stop_loss=90.0)])
        config = _make_config()
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is True
        assert len(report.trade_results) == 1
        result = report.trade_results[0]
        assert result.passed is True
        assert result.severity == "info"
        assert result.field_results["stop_distance"].passed is True
        assert result.field_results["risk_per_trade"].passed is True
        assert result.field_results["position_size"].passed is True


class TestStopDistanceMismatch:
    def test_different_stop_distance_fails(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        t2 = _make_trade(trade_id=2, entry_price=100.0, stop_loss=50.0)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config(
            stop_distance_pct=0.5,
            risk_per_trade_pct=1.0,
            position_size_pct=1.0,
        )
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        result = report.trade_results[0]
        assert result.field_results["stop_distance"].passed is False
        assert result.field_results["risk_per_trade"].passed is True
        assert result.field_results["position_size"].passed is True
        # C4: failed pairs flip the risk gate — error.
        assert result.severity == "error"


class TestPositionSizeMismatch:
    def test_different_position_size_fails(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        t2 = _make_trade(trade_id=2, entry_price=100.0, stop_loss=50.0)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config(
            # #128: 10 -> 50 is 400% drift against the reference; this test
            # targets position size, so keep stop distance inside tolerance.
            stop_distance_pct=4.0,
            risk_per_trade_pct=1.0,
            position_size_pct=0.5,
        )
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        result = report.trade_results[0]
        assert result.field_results["stop_distance"].passed is True
        assert result.field_results["risk_per_trade"].passed is True
        assert result.field_results["position_size"].passed is False
        # C4: failed pairs flip the risk gate — error.
        assert result.severity == "error"


class TestRiskPerTradeMismatch:
    def test_different_risk_per_trade_fails(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        t2 = _make_trade(trade_id=2, entry_price=100.0, stop_loss=50.0)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config(
            # #128: 10 -> 50 is 400% drift against the reference; this test
            # targets risk per trade, so keep stop distance inside tolerance.
            stop_distance_pct=4.0,
            risk_per_trade_pct=0.3,
            position_size_pct=1.0,
        )
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        result = report.trade_results[0]
        assert result.field_results["stop_distance"].passed is True
        assert result.field_results["risk_per_trade"].passed is False
        assert result.field_results["position_size"].passed is True
        # C4: failed pairs flip the risk gate — error.
        assert result.severity == "error"


class TestMissingTrade:
    def test_missing_trade_is_error(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([])
        config = _make_config()
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        assert len(report.trade_results) == 1
        result = report.trade_results[0]
        # C4: missing flips the risk gate — error.
        assert result.severity == "error"
        assert result.passed is False
        assert result.expected_trade is not None
        assert result.actual_trade is None
        assert result.expected_risk_profile is not None
        assert result.actual_risk_profile is None


class TestExtraTrade:
    def test_extra_trade_fails_by_default(self) -> None:
        """#85: a phantom actual trade (no expected counterpart) must FAIL the
        comparison — the old `passed=True/info` certified agreement that was
        never checked."""
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        ref1 = _make_reference([])
        ref2 = _make_reference([t1])
        config = _make_config()
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        assert len(report.trade_results) == 1
        result = report.trade_results[0]
        assert result.severity == "error"
        assert result.passed is False
        assert result.expected_trade is None
        assert result.actual_trade is not None
        assert result.expected_risk_profile is None
        assert result.actual_risk_profile is not None

    def test_extra_trade_passes_only_with_explicit_threshold(self) -> None:
        """#85: a strict count threshold is explicit opt-in (mirrors
        ReadinessConfig.max_extra_trades, default 0)."""
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        ref1 = _make_reference([])
        ref2 = _make_reference([t1])
        config = _make_config(max_extra_trades=1)
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is True
        result = report.trade_results[0]
        assert result.severity == "info"
        assert result.passed is True

    def test_extra_trades_above_threshold_fail(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        t2 = _make_trade(trade_id=2, entry_price=100.0, stop_loss=90.0)
        ref1 = _make_reference([])
        ref2 = _make_reference([t1, t2])
        config = _make_config(max_extra_trades=1)
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        assert report.passed is False
        assert all(result.passed is False for result in report.trade_results)


class TestReportSummary:
    def test_summary_counts_are_correct(self) -> None:
        t1 = _make_trade(
            trade_id=1, entry_time="2024-01-01T10:00:00Z", entry_price=100.0, stop_loss=90.0
        )
        t2 = _make_trade(
            trade_id=2, entry_time="2024-01-01T11:00:00Z", entry_price=100.0, stop_loss=90.0
        )
        t3 = _make_trade(
            trade_id=3, entry_time="2024-01-01T12:00:00Z", entry_price=100.0, stop_loss=90.0
        )
        ref1 = _make_reference([t1, t2])
        ref2 = _make_reference([t2, t3])
        config = _make_config(
            stop_distance_pct=1.0,
            risk_per_trade_pct=1.0,
            position_size_pct=1.0,
        )
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)

        summary = report.summary
        assert summary["total_trades"] == 3
        # t3 (the extra/phantom trade) fails by default since #85.
        assert summary["passed"] == 1
        assert summary["failed"] == 2
        assert summary["severity_counts"]["info"] == 1
        # C4: missing + phantom both flip the gate — error, not warning.
        assert summary["severity_counts"].get("warning", 0) == 0
        assert summary["severity_counts"]["error"] == 2
        assert report.passed is False


class TestRiskReportBuilderDelta:
    def test_converts_to_delta_report(self) -> None:
        t1 = _make_trade(trade_id=1, entry_price=100.0, stop_loss=90.0)
        t2 = _make_trade(trade_id=2, entry_price=100.0, stop_loss=50.0)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config(
            stop_distance_pct=0.5,
            risk_per_trade_pct=1.0,
            position_size_pct=1.0,
        )
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)
        delta = RiskReportBuilder.to_delta_report(
            report, reference_id="ref", platform_id="plat"
        )

        assert isinstance(delta, TradeDeltaReport)
        assert delta.reference_id == "ref"
        assert delta.platform_id == "plat"
        assert len(delta.matched_trades) == 1
        assert len(delta.missing_trades) == 0
        assert len(delta.extra_trades) == 0
        assert delta.passed is False

        matched = delta.matched_trades[0]
        # C4: failed pairs flip the risk gate — error.
        assert matched.severity == "error"
        assert matched.anomaly_type == "mismatch"
        assert len(matched.field_deltas) == 3
        assert matched.field_deltas[0].field == "stop_distance"
        assert matched.field_deltas[0].passed is False
        assert matched.explanation != ""

    def test_missing_and_extra_in_delta_report(self) -> None:
        t1 = _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z")
        t2 = _make_trade(trade_id=2, entry_time="2024-01-01T11:00:00Z")
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config()
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)
        delta = RiskReportBuilder.to_delta_report(report)

        assert len(delta.missing_trades) == 1
        assert delta.missing_trades[0].anomaly_type == "missing"
        # C4: missing flips the risk gate — error.
        assert delta.missing_trades[0].severity == "error"
        assert len(delta.extra_trades) == 1
        assert delta.extra_trades[0].anomaly_type == "extra"
        # #85: phantom trades are errors, not informational.
        assert delta.extra_trades[0].severity == "error"
        assert len(delta.matched_trades) == 0

    def test_perfect_match_delta(self) -> None:
        t1 = _make_trade(trade_id=1)
        t2 = _make_trade(trade_id=2)
        ref1 = _make_reference([t1])
        ref2 = _make_reference([t2])
        config = _make_config()
        comparator = RiskComparator(config)
        report = comparator.compare(ref1, ref2)
        delta = RiskReportBuilder.to_delta_report(report)

        assert delta.passed is True
        assert len(delta.matched_trades) == 1
        assert delta.matched_trades[0].anomaly_type is None
        assert delta.matched_trades[0].severity == "info"
        assert all(fd.passed for fd in delta.matched_trades[0].field_deltas)
