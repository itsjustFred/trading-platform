"""Tests for validation.comparison.delta_report."""

from __future__ import annotations

import pytest

from shared.models import BacktestMeta, BacktestResult, Trade, TradeExit
from validation.comparison.anomalies import AnomalyDetector, AnomalyType, TradeAnomaly
from validation.comparison.comparison import (
    FieldComparisonResult,
    MatchedTrade,
    TradeComparisonConfig,
    TradeComparisonResult,
    TradeComparator,
)
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    FieldDelta,
    TradeDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import MatchKey
from validation.reference import ValidationReference


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 1.1000,
    exit_price: float = 1.1050,
    quantity: float = 1000.0,
    position_size: float | None = None,
    pnl: float = 50.0,
    stop_loss: float | None = None,
    take_profit: float | None = None,
) -> Trade:
    meta: dict[str, object] = {}
    if stop_loss is not None:
        meta["stop_loss"] = stop_loss
    if take_profit is not None:
        meta["take_profit"] = take_profit
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=quantity)],
        pnl=pnl,
        position_size=position_size,
        meta=meta,
    )


def _make_matched(
    ref: Trade | None = None,
    actual: Trade | None = None,
) -> MatchedTrade:
    r = ref or _make_trade()
    a = actual or _make_trade()
    key = MatchKey(
        entry_time=r.entry_time,
        exit_time=r.last_exit_time,
        symbol=r.symbol,
        direction=r.direction,
    )
    return MatchedTrade(reference=r, actual=a, key=key)


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


def _make_platform_result(trades: list[Trade]) -> BacktestResult:
    meta = BacktestMeta(
        id="platform_test",
        created_at="2024-01-01T00:00:00Z",
        engine="test",
        strategies=["test"],
    )
    return BacktestResult(meta=meta, trades=trades, stats={})


def _make_field_result(
    field: str = "entry_price",
    expected: Any = 1.1000,
    actual: Any = 1.1050,
    tolerance: Any = 0.0001,
    passed: bool = False,
    message: str = "mismatch",
) -> FieldComparisonResult:
    return FieldComparisonResult(
        field=field,
        expected=expected,
        actual=actual,
        tolerance=tolerance,
        passed=passed,
        message=message,
    )


class TestEmptyReport:
    def test_empty_report_from_builder(self):
        ref = _make_reference([])
        platform = _make_platform_result([])
        report = DeltaReportBuilder.build(ref, platform, [], [])
        assert isinstance(report, TradeDeltaReport)
        assert report.reference_id == "unknown_EURUSD_1h"
        assert report.platform_id == "platform_test"
        assert report.matched_trades == []
        assert report.missing_trades == []
        assert report.extra_trades == []
        assert report.passed is True
        assert report.summary["total_expected"] == 0
        assert report.summary["total_actual"] == 0
        assert report.summary["total_matched"] == 0
        assert report.summary["severity_counts"] == {}
        assert report.explanation != ""
        assert "passed" in report.explanation.lower()


class TestFieldDelta:
    def test_field_delta_creation(self):
        fd = FieldDelta(
            field="entry_price",
            expected=1.1000,
            actual=1.1050,
            absolute_diff=0.005,
            relative_diff=0.004524,
            tolerance=0.0001,
            passed=False,
            explanation="entry_price differs by 0.45%",
        )
        assert fd.field == "entry_price"
        assert fd.expected == 1.1000
        assert fd.actual == 1.1050
        assert fd.absolute_diff == 0.005
        assert fd.passed is False
        assert "differs" in fd.explanation

    def test_field_delta_explanation_for_match(self):
        fd = FieldDelta(
            field="direction",
            expected="LONG",
            actual="LONG",
            passed=True,
            explanation="direction matches (expected 'LONG', actual 'LONG')",
        )
        assert fd.passed is True
        assert "matches" in fd.explanation

    def test_field_delta_numeric_match(self):
        fd = FieldDelta(
            field="entry_price",
            expected=1.1000,
            actual=1.10001,
            absolute_diff=0.00001,
            relative_diff=0.000009,
            tolerance=0.0001,
            passed=True,
            explanation="",
        )
        assert fd.relative_diff is not None
        assert fd.relative_diff < fd.tolerance


class TestTradeDelta:
    def test_trade_delta_creation(self):
        td = TradeDelta(
            trade_id="1_10",
            expected_trade_id="1",
            actual_trade_id="10",
            anomaly_type="mismatch",
            field_deltas=[
                FieldDelta(field="entry_price", expected=1.1000, actual=1.1050)
            ],
            severity="warning",
            explanation="Trade has mismatches",
        )
        assert td.trade_id == "1_10"
        assert td.severity == "warning"
        assert len(td.field_deltas) == 1


class TestReportWithMissingExtra:
    def test_missing_trade_in_report(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])
        anomalies = AnomalyDetector.detect_missing([ref_trade], [])
        report = DeltaReportBuilder.build(ref, platform, [], anomalies)
        assert len(report.missing_trades) == 1
        assert report.missing_trades[0].anomaly_type == "missing"
        assert report.missing_trades[0].expected_trade_id == "1"
        assert report.missing_trades[0].actual_trade_id is None
        assert report.passed is False
        assert len(report.extra_trades) == 0

    def test_extra_trade_in_report(self):
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([])
        platform = _make_platform_result([act_trade])
        anomalies = AnomalyDetector.detect_extra([], [act_trade])
        report = DeltaReportBuilder.build(ref, platform, [], anomalies)
        assert len(report.extra_trades) == 1
        assert report.extra_trades[0].anomaly_type == "extra"
        assert report.extra_trades[0].actual_trade_id == "10"
        assert report.extra_trades[0].expected_trade_id is None
        assert report.passed is False

    def test_missing_and_extra_trades_together(self):
        # Use different times so they don't get matched by the matcher
        ref_trade = _make_trade(trade_id=1, entry_time="2024-01-01T10:00:00Z")
        act_trade = _make_trade(trade_id=10, entry_time="2024-01-02T10:00:00Z")
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([act_trade])
        all_anomalies = (
            AnomalyDetector.detect_missing([ref_trade], [act_trade])
            + AnomalyDetector.detect_extra([ref_trade], [act_trade])
        )
        report = DeltaReportBuilder.build(ref, platform, [], all_anomalies)
        assert len(report.missing_trades) == 1
        assert len(report.extra_trades) == 1
        assert report.passed is False
        assert "missing" in report.explanation
        assert "extra" in report.explanation


class TestReportWithMismatch:
    def test_entry_price_mismatch_report(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert len(report.matched_trades) == 1
        trade_delta = report.matched_trades[0]
        assert trade_delta.anomaly_type == "mismatch"
        assert trade_delta.severity == "warning"
        # Check field deltas contain entry_price failure
        entry_fd = next(
            (fd for fd in trade_delta.field_deltas if fd.field == "entry_price"), None
        )
        assert entry_fd is not None
        assert entry_fd.passed is False
        assert entry_fd.expected == 1.1000
        assert entry_fd.actual == 1.1050
        assert entry_fd.absolute_diff is not None
        assert entry_fd.relative_diff is not None
        assert "differs" in entry_fd.explanation
        assert report.passed is True  # warning-only mismatch allows passed=True

    def test_direction_mismatch_severity_error_and_not_passed(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert len(report.matched_trades) == 1
        assert report.matched_trades[0].severity == "error"
        assert report.matched_trades[0].anomaly_type == "mismatch"
        assert report.passed is False
        dir_fd = next(
            (fd for fd in report.matched_trades[0].field_deltas if fd.field == "direction"),
            None,
        )
        assert dir_fd is not None
        assert dir_fd.passed is False
        assert "mismatch" in dir_fd.explanation.lower()

    def test_pnl_mismatch_severity_error(self):
        ref = _make_trade(pnl=50.0)
        act = _make_trade(pnl=60.0)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.matched_trades[0].severity == "error"
        assert report.passed is False

    def test_perfect_match_no_anomaly(self):
        ref = _make_trade()
        act = _make_trade()
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert len(report.matched_trades) == 1
        assert report.matched_trades[0].severity == "info"
        assert report.matched_trades[0].anomaly_type is None
        assert report.passed is True
        assert report.missing_trades == []
        assert report.extra_trades == []


class TestPassedLogic:
    def test_passed_true_warning_only(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.passed is True  # entry_price mismatch is warning severity

    def test_passed_false_due_to_error(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.passed is False

    def test_passed_false_due_to_missing(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])
        anomalies = AnomalyDetector.detect_missing([ref_trade], [])
        report = DeltaReportBuilder.build(ref, platform, [], anomalies)
        assert report.passed is False

    def test_passed_false_due_to_extra(self):
        act_trade = _make_trade(trade_id=10)
        ref = _make_reference([])
        platform = _make_platform_result([act_trade])
        anomalies = AnomalyDetector.detect_extra([], [act_trade])
        report = DeltaReportBuilder.build(ref, platform, [], anomalies)
        assert report.passed is False

    def test_passed_true_no_errors_no_missing_no_extra(self):
        ref = _make_trade()
        act = _make_trade()
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.passed is True


class TestExplanation:
    def test_explanation_not_empty_empty_report(self):
        ref = _make_reference([])
        platform = _make_platform_result([])
        report = DeltaReportBuilder.build(ref, platform, [], [])
        assert report.explanation != ""
        assert len(report.explanation) > 0

    def test_explanation_not_empty_with_mismatch(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.explanation != ""
        assert "passed" in report.explanation.lower() or "matched" in report.explanation.lower()

    def test_explanation_not_empty_with_error(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], [])
        assert report.explanation != ""
        assert "failed" in report.explanation.lower()

    def test_explanation_not_empty_with_missing(self):
        ref_trade = _make_trade(trade_id=1)
        ref = _make_reference([ref_trade])
        platform = _make_platform_result([])
        anomalies = AnomalyDetector.detect_missing([ref_trade], [])
        report = DeltaReportBuilder.build(ref, platform, [], anomalies)
        assert report.explanation != ""
        assert "failed" in report.explanation.lower()
        assert "missing" in report.explanation.lower()


class TestSummary:
    def test_summary_counts_correct(self):
        ref1 = _make_trade(trade_id=1)
        ref2 = _make_trade(trade_id=2, entry_time="2024-01-02T10:00:00Z")
        act1 = _make_trade(trade_id=10)
        act2 = _make_trade(trade_id=20, entry_time="2024-01-02T10:00:00Z")
        ref = _make_reference([ref1, ref2])
        platform = _make_platform_result([act1, act2])
        matched = _make_matched(ref1, act1)
        comp = TradeComparator().compare(matched)
        report = DeltaReportBuilder.build(ref, platform, [comp], [])
        assert report.summary["total_expected"] == 2
        assert report.summary["total_actual"] == 2
        assert report.summary["total_matched"] == 1
        assert "severity_counts" in report.summary
        assert "anomaly_counts" in report.summary


class TestFieldDeltaExplanation:
    def test_numeric_field_explanation_format(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        fd = DeltaReportBuilder._field_result_to_delta(comp.field_results["entry_price"])
        assert "entry_price" in fd.explanation
        assert "differs" in fd.explanation
        assert "expected" in fd.explanation
        assert "actual" in fd.explanation
        assert "tolerance" in fd.explanation

    def test_string_field_explanation_format(self):
        ref = _make_trade(direction="LONG")
        act = _make_trade(direction="SHORT")
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        fd = DeltaReportBuilder._field_result_to_delta(comp.field_results["direction"])
        assert "direction" in fd.explanation
        assert "mismatch" in fd.explanation.lower()
        assert "LONG" in fd.explanation
        assert "SHORT" in fd.explanation

    def test_time_field_explanation_format(self):
        ref = _make_trade(entry_time="2024-01-01T10:00:00Z")
        act = _make_trade(entry_time="2024-01-01T10:02:00Z")
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        fd = DeltaReportBuilder._field_result_to_delta(comp.field_results["entry_time"])
        assert "entry_time" in fd.explanation
        assert "tolerance" in fd.explanation


class TestAnomalyToDeltaConversion:
    def test_missing_anomaly_conversion(self):
        trade = _make_trade(trade_id=1)
        anomaly = TradeAnomaly(
            anomaly_type=AnomalyType.MISSING_TRADE,
            expected_trade=trade,
            severity="warning",
            message="Trade missing",
        )
        delta = DeltaReportBuilder._anomaly_to_delta(anomaly)
        assert delta.anomaly_type == "missing"
        assert delta.expected_trade_id == "1"
        assert delta.actual_trade_id is None
        assert delta.severity == "warning"
        assert delta.explanation == "Trade missing"

    def test_extra_anomaly_conversion(self):
        trade = _make_trade(trade_id=10)
        anomaly = TradeAnomaly(
            anomaly_type=AnomalyType.EXTRA_TRADE,
            actual_trade=trade,
            severity="info",
            message="Extra trade",
        )
        delta = DeltaReportBuilder._anomaly_to_delta(anomaly)
        assert delta.anomaly_type == "extra"
        assert delta.actual_trade_id == "10"
        assert delta.expected_trade_id is None
        assert delta.severity == "info"

    def test_mismatch_anomaly_conversion(self):
        ref = _make_trade(trade_id=1, entry_price=1.1000)
        act = _make_trade(trade_id=10, entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        anomaly = TradeAnomaly(
            anomaly_type=AnomalyType.MISMATCHED_TRADE,
            matched_trade=matched,
            field_mismatches=[
                comp.field_results["entry_price"],
            ],
            severity="warning",
            message="Mismatch",
        )
        delta = DeltaReportBuilder._anomaly_to_delta(anomaly)
        assert delta.anomaly_type == "mismatch"
        assert delta.expected_trade_id == "1"
        assert delta.actual_trade_id == "10"
        assert len(delta.field_deltas) == 1
        assert delta.field_deltas[0].field == "entry_price"

    def test_duplicate_match_anomaly_conversion(self):
        ref = _make_trade(trade_id=1)
        act = _make_trade(trade_id=10)
        matched = _make_matched(ref, act)
        anomaly = TradeAnomaly(
            anomaly_type=AnomalyType.DUPLICATE_MATCH,
            matched_trade=matched,
            severity="error",
            message="Duplicate match",
        )
        delta = DeltaReportBuilder._anomaly_to_delta(anomaly)
        assert delta.anomaly_type == "duplicate_match"
        assert delta.severity == "error"


class TestBuilderWithAnomalies:
    def test_builder_uses_anomaly_mismatch(self):
        ref = _make_trade(entry_price=1.1000)
        act = _make_trade(entry_price=1.1050)
        matched = _make_matched(ref, act)
        comp = TradeComparator().compare(matched)
        ref_model = _make_reference([ref])
        platform = _make_platform_result([act])
        # Convert comparison result anomalies to TradeAnomaly list
        anomalies = AnomalyDetector.detect_mismatches([comp])
        report = DeltaReportBuilder.build(ref_model, platform, [comp], anomalies)
        # The comparison result still produces a matched trade delta
        assert len(report.matched_trades) == 1
        # Anomaly mismatches are not separately bucketed into missing/extra
        assert len(report.missing_trades) == 0
        assert len(report.extra_trades) == 0
