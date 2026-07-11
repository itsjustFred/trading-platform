"""Paper-readiness validator for strategy validation.

Determines whether the platform's backtest output is close enough to a
reference backtest to justify moving to paper trading.
"""

from __future__ import annotations

from typing import Any

from validation.comparison.delta_report import TradeDelta, TradeDeltaReport
from validation.readiness.models import (
    PaperReadinessLevel,
    ReadinessCheck,
    ReadinessConfig,
    ReadinessReport,
)
from validation.regression.runner import RegressionResult
from validation.risk.comparison import RiskComparisonReport, RiskComparisonResult
from validation.stats.calculator import TradeStatistics


def _abs_pct_diff(expected: float, actual: float) -> float:
    denom = max(abs(expected), abs(actual))
    if denom == 0.0:
        return 0.0 if expected == actual else 1.0
    return abs(expected - actual) / denom


class ReadinessValidator:
    """Validate platform backtest results for paper-trading readiness.

    The validator evaluates a set of discrete checks against the comparison
    artifacts produced by the validation framework (trade delta report,
    regression result, risk comparison report and statistics).  It never
    executes trades or talks to brokers.
    """

    @classmethod
    def validate(
        cls,
        report: TradeDeltaReport | None = None,
        regression: RegressionResult | None = None,
        risk_report: RiskComparisonReport | None = None,
        stats: tuple[TradeStatistics, TradeStatistics] | None = None,
        config: ReadinessConfig | None = None,
    ) -> ReadinessReport:
        """Run all readiness checks and return a structured report.

        Parameters
        ----------
        report:
            Optional :class:`TradeDeltaReport` from trade-level comparison.
        regression:
            Optional :class:`RegressionResult` from golden-file regression.
        risk_report:
            Optional :class:`RiskComparisonReport` from risk-profile comparison.
        stats:
            Optional pair *(expected_stats, actual_stats)* for statistical
            consistency checks.
        config:
            Optional :class:`ReadinessConfig` with thresholds.  Defaults are
            used when not provided.

        Returns
        -------
        ReadinessReport
            Aggregated report with a :class:`PaperReadinessLevel`, individual
            checks, counts and recommendations.
        """
        cfg = config if config is not None else ReadinessConfig()
        checks: list[ReadinessCheck] = []
        recommendations: list[str] = []

        checks.extend(
            cls._check_required_artifacts(
                report, regression, risk_report, stats, cfg, recommendations
            )
        )
        checks.extend(cls._check_trades(report, cfg, recommendations))
        checks.extend(cls._check_regression(regression, cfg, recommendations))
        checks.extend(cls._check_risk(risk_report, cfg, recommendations))
        checks.extend(cls._check_stats(stats, cfg, recommendations))

        errors = sum(1 for c in checks if not c.passed and c.severity == "error")
        warnings = sum(1 for c in checks if c.severity == "warning")

        if errors > 0:
            level = PaperReadinessLevel.NOT_READY
        elif warnings > 0:
            level = PaperReadinessLevel.READY_WITH_WARNINGS
        else:
            level = PaperReadinessLevel.READY

        passed = level != PaperReadinessLevel.NOT_READY

        if level == PaperReadinessLevel.NOT_READY:
            recommendations.insert(
                0,
                "Platform is NOT READY for paper trading. Fix error-level "
                "discrepancies before proceeding.",
            )
        elif level == PaperReadinessLevel.READY_WITH_WARNINGS:
            recommendations.insert(
                0,
                "Platform is READY WITH WARNINGS for paper trading. Review "
                "warnings and consider tightening alignment before going live.",
            )
        else:
            recommendations.insert(
                0,
                "Platform is READY for paper trading based on the provided "
                "validation artifacts.",
            )

        return ReadinessReport(
            level=level,
            checks=checks,
            passed=passed,
            errors=errors,
            warnings=warnings,
            recommendations=recommendations,
        )

    @classmethod
    def _check_required_artifacts(
        cls,
        report: TradeDeltaReport | None,
        regression: RegressionResult | None,
        risk_report: RiskComparisonReport | None,
        stats: tuple[TradeStatistics, TradeStatistics] | None,
        cfg: ReadinessConfig,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        """Error if a mandatory validation artifact is missing.

        Without this, a partial run (e.g. only trade checks, no regression/risk/
        stats) could be classified READY even though the validation never
        actually ran end-to-end.
        """
        checks: list[ReadinessCheck] = []
        required = [
            ("trade_delta_report", cfg.require_trade_report, report is not None),
            ("regression_result", cfg.require_regression, regression is not None),
            ("risk_comparison_report", cfg.require_risk_report, risk_report is not None),
            ("trade_statistics", cfg.require_stats, stats is not None),
        ]
        for name, is_required, present in required:
            if not is_required:
                continue
            checks.append(
                ReadinessCheck(
                    name=f"required_artifact_{name}",
                    description=f"Required validation artifact '{name}' is present",
                    passed=present,
                    severity="info" if present else "error",
                    message=(
                        f"Artifact '{name}' present"
                        if present
                        else f"Required validation artifact '{name}' is missing; "
                        "cannot certify paper-trading readiness."
                    ),
                )
            )
            if not present:
                recommendations.append(
                    f"Provide the '{name}' artifact (a full validation run is "
                    "required before readiness can be certified)."
                )
        return checks

    @classmethod
    def _check_trades(
        cls,
        report: TradeDeltaReport | None,
        cfg: ReadinessConfig,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        """Trade-level readiness checks."""
        checks: list[ReadinessCheck] = []

        if report is None:
            checks.append(
                ReadinessCheck(
                    name="trade_errors",
                    description="No error-level deviations in matched trades",
                    passed=True,
                    severity="info",
                    message="No trade delta report provided; skipping.",
                )
            )
            checks.append(
                ReadinessCheck(
                    name="missing_trades",
                    description="Number of missing trades within threshold",
                    passed=True,
                    severity="info",
                    message="No trade delta report provided; skipping.",
                )
            )
            checks.append(
                ReadinessCheck(
                    name="extra_trades",
                    description="Number of extra trades within threshold",
                    passed=True,
                    severity="info",
                    message="No trade delta report provided; skipping.",
                )
            )
            checks.append(
                ReadinessCheck(
                    name="critical_mismatches",
                    description="No critical mismatches in direction or PnL",
                    passed=True,
                    severity="info",
                    message="No trade delta report provided; skipping.",
                )
            )
            return checks

        all_deltas = report.matched_trades + report.missing_trades + report.extra_trades
        error_count = sum(1 for d in all_deltas if d.severity == "error")
        missing_count = len(report.missing_trades)
        extra_count = len(report.extra_trades)
        critical_mismatch_count = sum(
            1
            for d in report.matched_trades
            if d.anomaly_type == "mismatch" and d.severity == "error"
        )
        warning_mismatch_count = sum(
            1
            for d in report.matched_trades
            if d.anomaly_type == "mismatch" and d.severity == "warning"
        )

        # Check a: No error-level deviations
        trade_error_sev = (
            "error"
            if error_count > cfg.max_error_count
            else ("warning" if error_count > 0 else "info")
        )
        trade_error_passed = error_count <= cfg.max_error_count
        checks.append(
            ReadinessCheck(
                name="trade_errors",
                description="No error-level deviations in matched trades",
                passed=trade_error_passed,
                severity=trade_error_sev,
                message=(
                    f"{error_count} error-level deviation(s) found"
                    if error_count > 0
                    else "No error-level deviations"
                ),
                details={"error_count": error_count, "threshold": cfg.max_error_count},
            )
        )
        if not trade_error_passed:
            recommendations.append(
                f"Reduce error-level trade discrepancies (found {error_count}, "
                f"threshold {cfg.max_error_count})."
            )

        # Check b: Missing trades within threshold
        missing_sev = (
            "error"
            if missing_count > cfg.max_missing_trades
            else ("warning" if missing_count > 0 else "info")
        )
        missing_passed = missing_count <= cfg.max_missing_trades
        checks.append(
            ReadinessCheck(
                name="missing_trades",
                description="Number of missing trades within threshold",
                passed=missing_passed,
                severity=missing_sev,
                message=(
                    f"{missing_count} missing trade(s)"
                    if missing_count > 0
                    else "No missing trades"
                ),
                details={"missing_count": missing_count, "threshold": cfg.max_missing_trades},
            )
        )
        if not missing_passed:
            recommendations.append(
                f"Reduce missing trades (found {missing_count}, "
                f"threshold {cfg.max_missing_trades})."
            )

        # Check b: Extra trades within threshold
        extra_sev = (
            "error"
            if extra_count > cfg.max_extra_trades
            else ("warning" if extra_count > 0 else "info")
        )
        extra_passed = extra_count <= cfg.max_extra_trades
        checks.append(
            ReadinessCheck(
                name="extra_trades",
                description="Number of extra trades within threshold",
                passed=extra_passed,
                severity=extra_sev,
                message=(
                    f"{extra_count} extra trade(s)"
                    if extra_count > 0
                    else "No extra trades"
                ),
                details={"extra_count": extra_count, "threshold": cfg.max_extra_trades},
            )
        )
        if not extra_passed:
            recommendations.append(
                f"Reduce extra trades (found {extra_count}, "
                f"threshold {cfg.max_extra_trades})."
            )

        # Check c: No critical mismatches in direction or PnL
        crit_sev = (
            "error"
            if critical_mismatch_count > cfg.max_mismatched_trades
            else ("warning" if critical_mismatch_count > 0 else "info")
        )
        critical_mismatch_passed = critical_mismatch_count <= cfg.max_mismatched_trades
        checks.append(
            ReadinessCheck(
                name="critical_mismatches",
                description="No critical mismatches in direction or PnL",
                passed=critical_mismatch_passed,
                severity=crit_sev,
                message=(
                    f"{critical_mismatch_count} critical mismatched trade(s)"
                    if critical_mismatch_count > 0
                    else "No critical mismatched trades"
                ),
                details={
                    "mismatched_count": critical_mismatch_count,
                    "threshold": cfg.max_mismatched_trades,
                },
            )
        )
        if not critical_mismatch_passed:
            recommendations.append(
                f"Reduce critical mismatched trades (found {critical_mismatch_count}, "
                f"threshold {cfg.max_mismatched_trades})."
            )

        # Warning-level mismatches (e.g. price mismatch)
        warn_sev = "warning" if warning_mismatch_count > 0 else "info"
        checks.append(
            ReadinessCheck(
                name="trade_warnings",
                description="Warning-level trade discrepancies present",
                passed=warning_mismatch_count == 0,
                severity=warn_sev,
                message=(
                    f"{warning_mismatch_count} warning-level trade discrepancy(ies)"
                    if warning_mismatch_count > 0
                    else "No warning-level trade discrepancies"
                ),
                details={"warning_count": warning_mismatch_count},
            )
        )
        if warning_mismatch_count > 0:
            recommendations.append(
                f"Review {warning_mismatch_count} warning-level trade discrepancy(ies)."
            )

        return checks

    @classmethod
    def _check_regression(
        cls,
        regression: RegressionResult | None,
        cfg: ReadinessConfig,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        """Regression-level readiness checks."""
        checks: list[ReadinessCheck] = []

        if regression is None:
            checks.append(
                ReadinessCheck(
                    name="regression_status",
                    description="Regression result passes when required",
                    passed=True,
                    severity="info",
                    message="No regression result provided; skipping.",
                )
            )
            return checks

        regression_passed = regression.passed or not cfg.require_all_goldens_pass
        checks.append(
            ReadinessCheck(
                name="regression_status",
                description="Regression result passes when required",
                passed=regression_passed,
                severity="error" if not regression_passed else "info",
                message=(
                    "Regression passed"
                    if regression.passed
                    else "Regression failed"
                ),
                details={
                    "regression_passed": regression.passed,
                    "require_all_goldens_pass": cfg.require_all_goldens_pass,
                },
            )
        )
        if not regression_passed:
            recommendations.append(
                "Regression test failed. Review golden-file deltas and fix "
                "deviations before paper trading."
            )

        return checks

    @classmethod
    def _check_risk(
        cls,
        risk_report: RiskComparisonReport | None,
        cfg: ReadinessConfig,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        """Risk-profile readiness checks."""
        checks: list[ReadinessCheck] = []

        if risk_report is None:
            checks.append(
                ReadinessCheck(
                    name="risk_consistency",
                    description="Risk profiles are consistent between reference and platform",
                    passed=True,
                    severity="info",
                    message="No risk comparison report provided; skipping.",
                )
            )
            return checks

        failed_results = [
            r for r in risk_report.trade_results if not r.passed and r.severity != "info"
        ]
        position_size_issues: list[dict[str, Any]] = []
        stop_distance_issues: list[dict[str, Any]] = []

        for result in failed_results:
            if result.expected_trade is None or result.actual_trade is None:
                continue
            fr = result.field_results
            if "position_size" in fr and not fr["position_size"].passed:
                expected = fr["position_size"].expected
                actual = fr["position_size"].actual
                if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                    position_size_issues.append(
                        {
                            "trade_id": result.expected_trade.id,
                            "diff_pct": _abs_pct_diff(float(expected), float(actual)),
                        }
                    )
            if "stop_distance" in fr and not fr["stop_distance"].passed:
                expected = fr["stop_distance"].expected
                actual = fr["stop_distance"].actual
                if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                    stop_distance_issues.append(
                        {
                            "trade_id": result.expected_trade.id,
                            "diff_pct": _abs_pct_diff(float(expected), float(actual)),
                        }
                    )

        max_ps_diff = max((i["diff_pct"] for i in position_size_issues), default=0.0)
        max_sd_diff = max((i["diff_pct"] for i in stop_distance_issues), default=0.0)

        ps_passed = max_ps_diff <= cfg.max_position_size_diff_pct
        sd_passed = max_sd_diff <= cfg.max_stop_distance_diff_pct

        checks.append(
            ReadinessCheck(
                name="risk_position_size",
                description="Position size differences within tolerance",
                passed=ps_passed,
                severity="error" if not ps_passed else "info",
                message=(
                    f"Max position-size diff {max_ps_diff:.4%} "
                    f"(threshold {cfg.max_position_size_diff_pct:.4%})"
                ),
                details={
                    "max_diff_pct": max_ps_diff,
                    "threshold": cfg.max_position_size_diff_pct,
                    "issue_count": len(position_size_issues),
                },
            )
        )
        if not ps_passed:
            recommendations.append(
                f"Position-size mismatch exceeds threshold ({max_ps_diff:.4%} > "
                f"{cfg.max_position_size_diff_pct:.4%})."
            )

        checks.append(
            ReadinessCheck(
                name="risk_stop_distance",
                description="Stop distance differences within tolerance",
                passed=sd_passed,
                severity="error" if not sd_passed else "info",
                message=(
                    f"Max stop-distance diff {max_sd_diff:.4%} "
                    f"(threshold {cfg.max_stop_distance_diff_pct:.4%})"
                ),
                details={
                    "max_diff_pct": max_sd_diff,
                    "threshold": cfg.max_stop_distance_diff_pct,
                    "issue_count": len(stop_distance_issues),
                },
            )
        )
        if not sd_passed:
            recommendations.append(
                f"Stop-distance mismatch exceeds threshold ({max_sd_diff:.4%} > "
                f"{cfg.max_stop_distance_diff_pct:.4%})."
            )

        return checks

    @classmethod
    def _check_stats(
        cls,
        stats: tuple[TradeStatistics, TradeStatistics] | None,
        cfg: ReadinessConfig,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        """Statistics-level readiness checks."""
        checks: list[ReadinessCheck] = []

        if stats is None:
            checks.append(
                ReadinessCheck(
                    name="stats_win_rate",
                    description="Win rate is within tolerance between reference and platform",
                    passed=True,
                    severity="info",
                    message="No statistics pair provided; skipping.",
                )
            )
            checks.append(
                ReadinessCheck(
                    name="stats_net_profit",
                    description="Net profit is within tolerance between reference and platform",
                    passed=True,
                    severity="info",
                    message="No statistics pair provided; skipping.",
                )
            )
            return checks

        expected_stats, actual_stats = stats

        win_rate_diff = _abs_pct_diff(expected_stats.win_rate, actual_stats.win_rate)
        win_rate_passed = win_rate_diff <= cfg.stats_win_rate_tolerance_pct
        checks.append(
            ReadinessCheck(
                name="stats_win_rate",
                description="Win rate is within tolerance between reference and platform",
                passed=win_rate_passed,
                severity="error" if not win_rate_passed else "info",
                message=(
                    f"Win-rate diff {win_rate_diff:.4%} "
                    f"(threshold {cfg.stats_win_rate_tolerance_pct:.4%})"
                ),
                details={
                    "expected": expected_stats.win_rate,
                    "actual": actual_stats.win_rate,
                    "diff_pct": win_rate_diff,
                },
            )
        )
        if not win_rate_passed:
            recommendations.append(
                f"Win-rate deviation exceeds threshold ({win_rate_diff:.4%} > "
                f"{cfg.stats_win_rate_tolerance_pct:.4%})."
            )

        net_profit_diff = _abs_pct_diff(expected_stats.net_profit, actual_stats.net_profit)
        net_profit_passed = net_profit_diff <= cfg.stats_net_profit_tolerance_pct
        checks.append(
            ReadinessCheck(
                name="stats_net_profit",
                description="Net profit is within tolerance between reference and platform",
                passed=net_profit_passed,
                severity="error" if not net_profit_passed else "info",
                message=(
                    f"Net-profit diff {net_profit_diff:.4%} "
                    f"(threshold {cfg.stats_net_profit_tolerance_pct:.4%})"
                ),
                details={
                    "expected": expected_stats.net_profit,
                    "actual": actual_stats.net_profit,
                    "diff_pct": net_profit_diff,
                },
            )
        )
        if not net_profit_passed:
            recommendations.append(
                f"Net-profit deviation exceeds threshold ({net_profit_diff:.4%} > "
                f"{cfg.stats_net_profit_tolerance_pct:.4%})."
            )

        return checks
