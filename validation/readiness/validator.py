"""Paper-readiness validator for strategy validation.

Determines whether the platform's backtest output is close enough to a
reference backtest to justify moving to paper trading.
"""

from __future__ import annotations

import math
from typing import Any

from validation.comparison.delta_report import TradeDeltaReport
from validation.readiness.models import (
    PaperReadinessLevel,
    ReadinessCheck,
    ReadinessConfig,
    ReadinessReport,
)
from validation.regression.runner import RegressionResult
from validation.risk.comparison import RiskComparisonReport
from validation.stats.calculator import TradeStatistics


def _abs_pct_diff(expected: float, actual: float) -> float:
    """Relative difference against the EXPECTED base (#83).

    The old ``max(|expected|, |actual|)`` denominator halved real drift
    (100 -> 200 read as 50%, not 100%). Zero expected fails closed unless
    actual is also zero.
    """
    denom = abs(expected)
    if denom == 0.0:
        return 0.0 if actual == 0.0 else 1.0
    return abs(expected - actual) / denom


_NO_TRADE_DELTA_REPORT_MESSAGE = "No trade delta report provided; skipping."


def _threshold_severity(count: int, threshold: int) -> str:
    """Severity for a counted deviation against its allowed threshold."""
    if count > threshold:
        return "error"
    return "warning" if count > 0 else "info"


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
        equity_evidence_status: str | None = None,
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
        equity_evidence_status:
            Optional combined equity evidence status. ``missing`` fails closed.

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
        checks.extend(
            cls._check_equity_evidence(equity_evidence_status, recommendations)
        )

        # Check for required artifacts - missing required = NOT_READY
        missing_required = []
        if cfg.require_trade_report and report is None:
            missing_required.append("trade delta report")
        if cfg.require_regression and regression is None:
            missing_required.append("regression result")
        if cfg.require_risk_report and risk_report is None:
            missing_required.append("risk comparison report")
        if cfg.require_stats and stats is None:
            missing_required.append("statistics comparison")

        if missing_required:
            for artifact in missing_required:
                checks.append(
                    ReadinessCheck(
                        name=f"required_{artifact.replace(' ', '_')}",
                        description=f"Required artifact present: {artifact}",
                        passed=False,
                        severity="error",
                        message=f"Missing required artifact: {artifact}",
                    )
                )

        # `applicable` is excluded from both counts: a check with no answer must not be counted
        # as a failure, and must not be counted as a pass either. Default True, so this is a
        # no-op until something opts in.
        errors = sum(
            1 for c in checks if c.applicable and not c.passed and c.severity == "error"
        )
        warnings = sum(
            1 for c in checks if c.applicable and c.severity == "warning" and not c.passed
        )

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
    def _check_equity_evidence(
        cls,
        equity_evidence_status: str | None,
        recommendations: list[str],
    ) -> list[ReadinessCheck]:
        if equity_evidence_status is None:
            return []
        available = equity_evidence_status == "available"
        if not available:
            recommendations.append(
                "Provide complete equity evidence for both reference and platform "
                "before certifying readiness."
            )
        return [
            ReadinessCheck(
                name="equity_evidence",
                description="Reference and platform equity evidence is complete",
                passed=available,
                severity="info" if available else "error",
                message=(
                    "Equity evidence is available"
                    if available
                    else "Equity evidence is missing or incomplete; readiness cannot be certified."
                ),
                details={"status": equity_evidence_status},
            )
        ]

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
            # #83: missing artifacts fail closed regardless of caller flags —
            # a absent delta report must never read as four info passes.
            skipped_names = (
                ("trade_errors", "No error-level deviations in matched trades"),
                ("missing_trades", "Number of missing trades within threshold"),
                ("extra_trades", "Number of extra trades within threshold"),
                ("critical_mismatches", "No critical mismatches in direction or PnL"),
            )
            for name, description in skipped_names:
                checks.append(
                    ReadinessCheck(
                        name=name,
                        description=description,
                        passed=False,
                        severity="error",
                        message=_NO_TRADE_DELTA_REPORT_MESSAGE,
                    )
                )
            recommendations.append(
                "Provide the trade delta report (readiness cannot be certified "
                "on missing trade comparison)."
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
        trade_error_sev = _threshold_severity(error_count, cfg.max_error_count)
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
        missing_sev = _threshold_severity(missing_count, cfg.max_missing_trades)
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
        extra_sev = _threshold_severity(extra_count, cfg.max_extra_trades)
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
        crit_sev = _threshold_severity(critical_mismatch_count, cfg.max_mismatched_trades)
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
                    passed=False,
                    severity="error",
                    message="No regression result provided; cannot certify readiness.",
                )
            )
            recommendations.append(
                "Provide the regression result (readiness cannot be certified "
                "on missing regression)."
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
            # #83: fail closed — a missing risk report is not a pass.
            checks.append(
                ReadinessCheck(
                    name="risk_consistency",
                    description="Risk profiles are consistent between reference and platform",
                    passed=False,
                    severity="error",
                    message="No risk comparison report provided; cannot certify readiness.",
                )
            )
            recommendations.append(
                "Provide the risk comparison report (readiness cannot be "
                "certified on missing risk comparison)."
            )
            return checks

        failed_results = [r for r in risk_report.trade_results if not r.passed]
        has_risk_evidence = any(
            any(
                field_result.expected is not None or field_result.actual is not None
                for field_result in result.field_results.values()
            )
            for result in risk_report.trade_results
        )
        position_size_issues: list[dict[str, Any]] = []
        stop_distance_issues: list[dict[str, Any]] = []
        structural_failures = 0

        for result in failed_results:
            if result.expected_trade is None or result.actual_trade is None:
                # Missing/extra side of a comparison: no field-level evidence
                # exists, so it can only be counted structurally (DV-B37).
                structural_failures += 1
                continue
            fr = result.field_results
            numeric_issue = False
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
                    numeric_issue = True
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
                    numeric_issue = True
            if not numeric_issue:
                # A failed result whose evidence is not numeric (or absent)
                # would previously slip through both tolerance checks.
                structural_failures += 1

        max_ps_diff = max((i["diff_pct"] for i in position_size_issues), default=0.0)
        max_sd_diff = max((i["diff_pct"] for i in stop_distance_issues), default=0.0)

        ps_passed = max_ps_diff <= cfg.max_position_size_diff_pct
        sd_passed = max_sd_diff <= cfg.max_stop_distance_diff_pct

        # DV-B37: consult the report verdict and fail closed on structural
        # failures that carry no numeric evidence.  An empty report counts as
        # vacuously passing only when the producer marked it passed; both
        # conditions surface as error-level checks below.
        report_verdict_passed = (
            bool(risk_report.passed)
            and has_risk_evidence
            and not failed_results
        )

        checks.append(
            ReadinessCheck(
                name="risk_evidence",
                description="Risk comparison contains comparable field evidence",
                passed=has_risk_evidence,
                severity="info" if has_risk_evidence else "error",
                message=(
                    f"Risk evidence contains {len(risk_report.trade_results)} comparable result(s)"
                    if has_risk_evidence
                    else "Risk comparison has no comparable field evidence"
                ),
                details={
                    "result_count": len(risk_report.trade_results),
                    "field_evidence_count": sum(
                        len(result.field_results) for result in risk_report.trade_results
                    ),
                },
            )
        )
        if not has_risk_evidence:
            recommendations.append(
                "Provide non-empty risk comparison evidence before certifying readiness."
            )

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

        checks.append(
            ReadinessCheck(
                name="risk_failed_results",
                description="No structurally failed risk comparisons without numeric evidence",
                passed=structural_failures == 0,
                severity="error" if structural_failures else "info",
                message=(
                    f"{structural_failures} failed risk result(s) without "
                    "position-size/stop-distance evidence"
                    if structural_failures
                    else "All failed risk results carry numeric evidence"
                ),
                details={
                    "structural_failure_count": structural_failures,
                    "failed_result_count": len(failed_results),
                },
            )
        )
        if structural_failures:
            recommendations.append(
                f"Review {structural_failures} risk comparison(s) that failed "
                "without comparable position-size or stop-distance values."
            )

        checks.append(
            ReadinessCheck(
                name="risk_report_verdict",
                description="Risk comparison report verdict is passed",
                passed=report_verdict_passed,
                severity="error" if not report_verdict_passed else "info",
                message=(
                    "Risk comparison report passed"
                    if report_verdict_passed
                    else "Risk comparison report failed: "
                    + str(risk_report.explanation or "no explanation")
                ),
                details={"report_passed": report_verdict_passed},
            )
        )
        if not report_verdict_passed:
            recommendations.append(
                "Risk comparison report verdict is failed; resolve risk "
                "deviations before paper trading."
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
            # #83: fail closed — missing statistics are not two info passes.
            for name, description in (
                ("stats_win_rate", "Win rate within tolerance (reference vs platform)"),
                ("stats_net_profit", "Net profit within tolerance (reference vs platform)"),
                ("stats_sharpe", "Sharpe ratio within tolerance (reference vs platform)"),
                ("stats_max_drawdown", "Max drawdown within tolerance (reference vs platform)"),
            ):
                checks.append(
                    ReadinessCheck(
                        name=name,
                        description=description,
                        passed=False,
                        severity="error",
                        message="No statistics pair provided; cannot certify readiness.",
                    )
                )
            recommendations.append(
                "Provide the statistics pair (readiness cannot be certified "
                "on missing statistics)."
            )
            return checks

        expected_stats, actual_stats = stats
        risk_free_rates = (
            float(getattr(expected_stats, "risk_free_rate", 0.0)),
            float(getattr(actual_stats, "risk_free_rate", 0.0)),
        )
        canonical_rf = all(math.isfinite(rate) and rate == 0.0 for rate in risk_free_rates)
        checks.append(
            ReadinessCheck(
                name="stats_risk_free_rate",
                description="Statistics use the canonical zero risk-free rate",
                passed=canonical_rf,
                severity="info" if canonical_rf else "error",
                message=(
                    "Both statistics sides use risk_free_rate=0"
                    if canonical_rf
                    else f"Expected risk_free_rate=0 on both sides, got {risk_free_rates}"
                ),
                details={"expected": risk_free_rates[0], "actual": risk_free_rates[1]},
            )
        )
        if not canonical_rf:
            recommendations.append(
                "Recompute statistics with risk_free_rate=0 before readiness validation."
            )

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

        # #83: Sharpe gate (canonical rf=0 on both sides). Pass when the
        # absolute drift fits max(10% relative, 0.1 absolute).
        expected_sharpe = float(expected_stats.sharpe_ratio)
        actual_sharpe = float(actual_stats.sharpe_ratio)
        sharpe_abs_diff = abs(expected_sharpe - actual_sharpe)
        sharpe_threshold = max(
            cfg.stats_sharpe_tolerance_pct * abs(expected_sharpe),
            cfg.stats_sharpe_tolerance_abs,
        )
        sharpe_passed = sharpe_abs_diff <= sharpe_threshold
        # At an expected Sharpe of exactly zero the relative term vanishes and only the absolute
        # floor applies, so a "proportionate" comparison has no scale to work with. Whether that
        # should be a pass/fail or an explicit "not comparable" is the open policy question; the
        # config decides, and the default preserves today's behaviour.
        sharpe_not_comparable = (
            expected_sharpe == 0.0
            and cfg.stats_sharpe_zero_expectation_is_not_comparable
        )
        checks.append(
            ReadinessCheck(
                name="stats_sharpe",
                description="Sharpe ratio is within tolerance between reference and platform",
                passed=sharpe_passed,
                message=(
                    "Not comparable: reference Sharpe is exactly 0.0"
                    if sharpe_not_comparable
                    else (
                        f"Sharpe abs diff {sharpe_abs_diff:.4f} "
                        f"(threshold {sharpe_threshold:.4f})"
                    )
                ),
                details={
                    "expected": expected_sharpe,
                    "actual": actual_sharpe,
                    "abs_diff": sharpe_abs_diff,
                    "threshold": sharpe_threshold,
                    "applicable": not sharpe_not_comparable,
                },
                applicable=not sharpe_not_comparable,
                severity=(
                    "info"
                    if (sharpe_passed or sharpe_not_comparable)
                    else "error"
                ),
            )
        )
        if sharpe_not_comparable:
            recommendations.append(
                "Sharpe was not compared: the reference Sharpe is exactly 0.0, so there is no "
                "scale for a proportionate tolerance. Reported as not comparable rather than "
                "passed or failed."
            )
        elif not sharpe_passed:
            recommendations.append(
                f"Sharpe deviation exceeds threshold ({sharpe_abs_diff:.4f} > "
                f"{sharpe_threshold:.4f})."
            )

        # #83: max-drawdown gate — 2pp absolute on max_drawdown_pct.
        expected_dd = float(expected_stats.max_drawdown_pct)
        actual_dd = float(actual_stats.max_drawdown_pct)
        dd_abs_diff = abs(expected_dd - actual_dd)
        dd_passed = dd_abs_diff <= cfg.stats_max_drawdown_tolerance_pp
        checks.append(
            ReadinessCheck(
                name="stats_max_drawdown",
                description="Max drawdown is within tolerance between reference and platform",
                passed=dd_passed,
                severity="error" if not dd_passed else "info",
                message=(
                    f"Max-drawdown abs diff {dd_abs_diff:.4%} "
                    f"(threshold {cfg.stats_max_drawdown_tolerance_pp:.4%})"
                ),
                details={
                    "expected": expected_dd,
                    "actual": actual_dd,
                    "abs_diff": dd_abs_diff,
                    "threshold": cfg.stats_max_drawdown_tolerance_pp,
                },
            )
        )
        if not dd_passed:
            recommendations.append(
                f"Max-drawdown deviation exceeds threshold ({dd_abs_diff:.4%} > "
                f"{cfg.stats_max_drawdown_tolerance_pp:.4%})."
            )

        return checks
