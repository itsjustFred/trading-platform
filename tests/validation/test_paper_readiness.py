"""Tests for the paper-readiness validator."""

from __future__ import annotations

import pytest

from shared.models import Trade, TradeExit
from validation.comparison.delta_report import FieldDelta, TradeDelta, TradeDeltaReport
from validation.comparison.comparison import FieldComparisonResult
from validation.readiness.models import (
    PaperReadinessLevel,
    ReadinessCheck,
    ReadinessConfig,
    ReadinessReport,
)
from validation.readiness.validator import ReadinessValidator
from validation.regression.runner import RegressionResult
from validation.reference import ValidationReference
from validation.risk.comparison import (
    RiskComparisonReport,
    RiskComparisonResult,
)
from validation.stats.calculator import TradeStatistics


# ── helpers ──────────────────────────────────────────────────────────


def _make_trade_delta(
    severity: str = "info",
    anomaly_type: str | None = None,
    field_deltas: list[FieldDelta] | None = None,
    trade_id: str = "1",
) -> TradeDelta:
    return TradeDelta(
        trade_id=trade_id,
        expected_trade_id=trade_id,
        actual_trade_id=trade_id,
        anomaly_type=anomaly_type,
        field_deltas=field_deltas or [],
        severity=severity,
        explanation="test",
    )


def _make_delta_report(
    matched: list[TradeDelta] | None = None,
    missing: list[TradeDelta] | None = None,
    extra: list[TradeDelta] | None = None,
) -> TradeDeltaReport:
    return TradeDeltaReport(
        reference_id="ref",
        platform_id="plat",
        matched_trades=matched or [],
        missing_trades=missing or [],
        extra_trades=extra or [],
    )


def _make_reference() -> ValidationReference:
    return ValidationReference(
        strategy_name="test",
        source_framework="unknown",
        symbol="EURUSD",
        timeframe="1h",
        settings={},
        trades=[],
        stats={},
        raw_metadata={},
        allow_empty=True,
    )


def _make_regression_result(passed: bool = True) -> RegressionResult:
    return RegressionResult(
        golden_path="/tmp/golden.json",
        current_reference=_make_reference(),
        trade_delta_report=_make_delta_report(),
        passed=passed,
    )


def _make_trade(
    trade_id: int = 1,
    symbol: str = "EURUSD",
    direction: str = "LONG",
    entry_time: str = "2024-01-01T10:00:00Z",
    exit_time: str = "2024-01-01T11:00:00Z",
    entry_price: float = 100.0,
    quantity: float = 1.0,
    pnl: float = 0.0,
) -> Trade:
    return Trade(
        id=trade_id,
        symbol=symbol,
        direction=direction,
        entry_time=entry_time,
        entry_price=entry_price,
        quantity=quantity,
        exits=[TradeExit(time=exit_time, price=entry_price, quantity=quantity)],
        pnl=pnl,
    )


def _make_risk_result(
    passed: bool = True,
    position_size_passed: bool = True,
    stop_distance_passed: bool = True,
) -> RiskComparisonResult:
    ps_expected = 1.0
    ps_actual = 1.0 if position_size_passed else 2.0
    sd_expected = 10.0
    sd_actual = 10.0 if stop_distance_passed else 20.0
    return RiskComparisonResult(
        expected_trade=_make_trade(trade_id=1),
        actual_trade=_make_trade(trade_id=2),
        expected_risk_profile=None,
        actual_risk_profile=None,
        field_results={
            "position_size": FieldComparisonResult(
                field="position_size",
                expected=ps_expected,
                actual=ps_actual,
                tolerance=(0.01, None),
                passed=position_size_passed,
                message="test",
            ),
            "stop_distance": FieldComparisonResult(
                field="stop_distance",
                expected=sd_expected,
                actual=sd_actual,
                tolerance=(0.01, None),
                passed=stop_distance_passed,
                message="test",
            ),
        },
        passed=passed,
        severity="info" if passed else "warning",
    )


def _make_risk_report(
    results: list[RiskComparisonResult] | None = None,
) -> RiskComparisonReport:
    return RiskComparisonReport(
        trade_results=results or [_make_risk_result()],
        passed=all(r.passed for r in (results or [_make_risk_result()])),
    )


def _make_stats(
    win_rate: float = 0.5,
    net_profit: float = 1000.0,
) -> TradeStatistics:
    return TradeStatistics(
        total_trades=10,
        winning_trades=5,
        losing_trades=5,
        win_rate=win_rate,
        net_profit=net_profit,
    )


# ── tests ────────────────────────────────────────────────────────────


class TestPerfectMatch:
    def test_ready_when_perfect_match(self) -> None:
        """A perfect delta report with passing regression and stats → READY."""
        report = _make_delta_report()
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig()

        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        assert result.level == PaperReadinessLevel.READY
        assert result.passed is True
        assert result.errors == 0
        assert result.warnings == 0
        assert len(result.checks) > 0


class TestDirectionError:
    def test_not_ready_on_direction_error(self) -> None:
        """A direction mismatch is severity=error → NOT_READY."""
        price_fd = FieldDelta(field="entry_price", expected=1.0, actual=1.1, passed=False)
        dir_fd = FieldDelta(field="direction", expected="LONG", actual="SHORT", passed=False)
        delta = _make_trade_delta(
            severity="error",
            anomaly_type="mismatch",
            field_deltas=[price_fd, dir_fd],
        )
        report = _make_delta_report(matched=[delta])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )

        assert result.level == PaperReadinessLevel.NOT_READY
        assert result.passed is False
        assert result.errors > 0


class TestPnlError:
    def test_not_ready_on_pnl_error(self) -> None:
        """A PnL mismatch is severity=error → NOT_READY."""
        pnl_fd = FieldDelta(field="pnl", expected=50.0, actual=-10.0, passed=False)
        delta = _make_trade_delta(
            severity="error",
            anomaly_type="mismatch",
            field_deltas=[pnl_fd],
        )
        report = _make_delta_report(matched=[delta])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )

        assert result.level == PaperReadinessLevel.NOT_READY
        assert result.passed is False
        assert result.errors > 0


class TestMissingTrades:
    def test_warning_on_missing_trade_within_threshold(self) -> None:
        """Missing trades within threshold produce a warning, not an error."""
        missing = _make_trade_delta(severity="warning", anomaly_type="missing")
        report = _make_delta_report(missing=[missing])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(max_missing_trades=2)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        missing_check = next(c for c in result.checks if c.name == "missing_trades")
        assert missing_check.passed is True
        assert missing_check.severity == "warning"
        assert result.level == PaperReadinessLevel.READY_WITH_WARNINGS
        assert result.warnings > 0

    def test_not_ready_on_missing_trade_above_threshold(self) -> None:
        """Missing trades above threshold are an error → NOT_READY."""
        missing1 = _make_trade_delta(severity="warning", anomaly_type="missing", trade_id="1")
        missing2 = _make_trade_delta(severity="warning", anomaly_type="missing", trade_id="2")
        report = _make_delta_report(missing=[missing1, missing2])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(max_missing_trades=1)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        missing_check = next(c for c in result.checks if c.name == "missing_trades")
        assert missing_check.passed is False
        assert missing_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY
        assert result.passed is False


class TestPriceMismatch:
    def test_warning_on_price_mismatch(self) -> None:
        """A price mismatch is severity=warning in the delta report."""
        price_fd = FieldDelta(field="entry_price", expected=1.0, actual=1.05, passed=False)
        delta = _make_trade_delta(
            severity="warning",
            anomaly_type="mismatch",
            field_deltas=[price_fd],
        )
        report = _make_delta_report(matched=[delta])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )

        warn_check = next(c for c in result.checks if c.name == "trade_warnings")
        assert warn_check.passed is False
        assert warn_check.severity == "warning"
        assert result.level == PaperReadinessLevel.READY_WITH_WARNINGS


class TestRegression:
    def test_not_ready_on_regression_failure(self) -> None:
        """A failed regression with require_all_goldens_pass=True → NOT_READY."""
        regression = _make_regression_result(passed=False)
        report = _make_delta_report()
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(require_all_goldens_pass=True)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        reg_check = next(c for c in result.checks if c.name == "regression_status")
        assert reg_check.passed is False
        assert reg_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY


class TestReadyWithWarnings:
    def test_ready_with_warnings(self) -> None:
        """Warnings but no errors → READY_WITH_WARNINGS."""
        missing = _make_trade_delta(severity="warning", anomaly_type="missing")
        report = _make_delta_report(missing=[missing])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(max_missing_trades=5)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        assert result.level == PaperReadinessLevel.READY_WITH_WARNINGS
        assert result.passed is True
        assert result.errors == 0
        assert result.warnings > 0


class TestRiskConsistency:
    def test_risk_consistency_pass(self) -> None:
        """Passing risk comparison → READY."""
        risk = _make_risk_report()
        report = _make_delta_report()
        regression = _make_regression_result(passed=True)
        stats = (_make_stats(), _make_stats())
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )

        ps_check = next(c for c in result.checks if c.name == "risk_position_size")
        sd_check = next(c for c in result.checks if c.name == "risk_stop_distance")
        assert ps_check.passed is True
        assert sd_check.passed is True
        assert result.level == PaperReadinessLevel.READY

    def test_risk_position_size_mismatch(self) -> None:
        """Position-size mismatch exceeding tolerance → NOT_READY."""
        risk_result = _make_risk_result(
            passed=False,
            position_size_passed=False,
            stop_distance_passed=True,
        )
        risk = _make_risk_report(results=[risk_result])
        report = _make_delta_report()
        regression = _make_regression_result(passed=True)
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(
            max_position_size_diff_pct=0.001,
            max_stop_distance_diff_pct=1.0,
        )
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        ps_check = next(c for c in result.checks if c.name == "risk_position_size")
        assert ps_check.passed is False
        assert ps_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY


class TestStatsConsistency:
    def test_stats_consistency_pass(self) -> None:
        """Identical stats → READY."""
        stats = (_make_stats(), _make_stats())
        report = _make_delta_report()
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )

        wr_check = next(c for c in result.checks if c.name == "stats_win_rate")
        np_check = next(c for c in result.checks if c.name == "stats_net_profit")
        assert wr_check.passed is True
        assert np_check.passed is True
        assert result.level == PaperReadinessLevel.READY

    def test_stats_win_rate_mismatch(self) -> None:
        """Win-rate mismatch exceeding tolerance → NOT_READY."""
        expected = _make_stats(win_rate=0.5)
        actual = _make_stats(win_rate=0.7)
        stats = (expected, actual)
        report = _make_delta_report()
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        config = ReadinessConfig(stats_win_rate_tolerance_pct=0.01)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        wr_check = next(c for c in result.checks if c.name == "stats_win_rate")
        assert wr_check.passed is False
        assert wr_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY


class TestReportMeta:
    def test_report_has_recommendations(self) -> None:
        """Any non-trivial validation yields at least one recommendation."""
        missing = _make_trade_delta(severity="warning", anomaly_type="missing")
        report = _make_delta_report(missing=[missing])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        config = ReadinessConfig(max_missing_trades=5)
        result = ReadinessValidator.validate(
            report=report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=config,
        )

        assert len(result.recommendations) > 0
        assert any("warning" in r.lower() or "ready" in r.lower() for r in result.recommendations)

    def test_level_calculation(self) -> None:
        """Explicitly verify level logic: error→NOT_READY, warn→READY_WITH_WARNINGS, else→READY."""
        # Error path
        err_delta = _make_trade_delta(severity="error", anomaly_type="mismatch")
        err_report = _make_delta_report(matched=[err_delta])
        regression = _make_regression_result(passed=True)
        risk = _make_risk_report()
        stats = (_make_stats(), _make_stats())
        err_result = ReadinessValidator.validate(
            report=err_report,
            regression=regression,
            risk_report=risk,
            stats=stats,
        )
        assert err_result.level == PaperReadinessLevel.NOT_READY

        # Warning path
        warn_delta = _make_trade_delta(severity="warning", anomaly_type="missing")
        warn_report = _make_delta_report(missing=[warn_delta])
        warn_config = ReadinessConfig(max_missing_trades=5)
        warn_result = ReadinessValidator.validate(
            report=warn_report,
            regression=regression,
            risk_report=risk,
            stats=stats,
            config=warn_config,
        )
        assert warn_result.level == PaperReadinessLevel.READY_WITH_WARNINGS

        # Ready path
        ready_result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=regression,
            risk_report=risk,
            stats=stats,
        )
        assert ready_result.level == PaperReadinessLevel.READY

    def test_empty_inputs(self) -> None:
        """#83: missing artifacts fail closed regardless of caller flags —
        no inputs must never read as READY, even with require_*=False."""
        config = ReadinessConfig(
            require_trade_report=False,
            require_regression=False,
            require_risk_report=False,
            require_stats=False,
        )
        result = ReadinessValidator.validate(config=config)

        assert result.level == PaperReadinessLevel.NOT_READY
        assert result.passed is False
        assert result.errors > 0
        assert all(
            c.severity == "error" for c in result.checks if not c.passed
        )


class TestRiskGateStructuralFailures:
    """DV-B37: the risk gate must fail closed on structurally failed reports."""

    def test_failed_report_with_non_numeric_fields_fails(self) -> None:
        """A failed risk result without numeric field deltas must not pass."""
        risk_result = RiskComparisonResult(
            expected_trade=_make_trade(trade_id=1),
            actual_trade=_make_trade(trade_id=2),
            expected_risk_profile=None,
            actual_risk_profile=None,
            field_results={},
            passed=False,
            severity="warning",
            explanation="Could not compute risk profile for one or both trades",
        )
        risk = RiskComparisonReport(
            trade_results=[risk_result],
            passed=False,
            explanation="Risk validation failed",
        )
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=risk,
            stats=(_make_stats(), _make_stats()),
        )

        failed_check = next(
            c for c in result.checks if c.name == "risk_failed_results"
        )
        assert failed_check.passed is False
        assert failed_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY

    def test_failed_report_verdict_is_consulted(self) -> None:
        """risk_report.passed == False must fail the readiness gate."""
        risk = _make_risk_report()  # individual fields pass
        risk.passed = False
        risk.explanation = "Risk validation failed: 1 failed trade(s)."

        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=risk,
            stats=(_make_stats(), _make_stats()),
        )

        verdict_check = next(
            c for c in result.checks if c.name == "risk_report_verdict"
        )
        assert verdict_check.passed is False
        assert verdict_check.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY

    def test_empty_report_marked_failed_fails(self) -> None:
        """Empty trade_results with passed=False must not pass vacuously."""
        risk = RiskComparisonReport(
            trade_results=[],
            passed=False,
            explanation="Risk validation failed: no trades compared.",
        )
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=risk,
            stats=(_make_stats(), _make_stats()),
        )

        assert result.level == PaperReadinessLevel.NOT_READY

    def test_empty_report_marked_passed_stays_informational(self) -> None:
        """An empty report the producer marked as passed stays a pass."""
        risk = RiskComparisonReport(trade_results=[], passed=True, explanation="")
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=risk,
            stats=(_make_stats(), _make_stats()),
        )

        assert result.level == PaperReadinessLevel.READY


class TestIssue83Gates:
    """#83: denominator on |expected|, Sharpe + max-drawdown gates."""

    def test_halved_denominator_fixed(self) -> None:
        """100 -> 200 must read as 100% drift, not 50%."""
        from validation.readiness.validator import _abs_pct_diff

        assert _abs_pct_diff(100.0, 200.0) == 1.0

    def test_sharpe_gate_trips_on_decay(self) -> None:
        expected = _make_stats()
        object.__setattr__(expected, "sharpe_ratio", 2.0)
        actual = _make_stats()
        object.__setattr__(actual, "sharpe_ratio", 0.01)
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=_make_risk_report(),
            stats=(expected, actual),
        )
        sharpe = next(c for c in result.checks if c.name == "stats_sharpe")
        assert sharpe.passed is False
        assert sharpe.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY

    def test_sharpe_gate_passes_within_absolute_band(self) -> None:
        expected = _make_stats()
        object.__setattr__(expected, "sharpe_ratio", 1.5)
        actual = _make_stats()
        object.__setattr__(actual, "sharpe_ratio", 1.55)
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=_make_risk_report(),
            stats=(expected, actual),
        )
        sharpe = next(c for c in result.checks if c.name == "stats_sharpe")
        assert sharpe.passed is True

    def test_max_drawdown_gate_trips_beyond_2pp(self) -> None:
        expected = _make_stats()
        object.__setattr__(expected, "max_drawdown_pct", 0.05)
        actual = _make_stats()
        object.__setattr__(actual, "max_drawdown_pct", 0.10)
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=_make_risk_report(),
            stats=(expected, actual),
        )
        dd = next(c for c in result.checks if c.name == "stats_max_drawdown")
        assert dd.passed is False
        assert dd.severity == "error"
        assert result.level == PaperReadinessLevel.NOT_READY

    def test_missing_stats_fails_closed(self) -> None:
        result = ReadinessValidator.validate(
            report=_make_delta_report(),
            regression=_make_regression_result(passed=True),
            risk_report=_make_risk_report(),
            stats=None,
            config=ReadinessConfig(require_stats=False),
        )
        assert result.level == PaperReadinessLevel.NOT_READY
        assert any(
            c.name == "stats_sharpe" and not c.passed for c in result.checks
        )
