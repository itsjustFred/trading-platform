"""Risk comparison engine for strategy validation.

Compares risk profiles (stop distance, risk per trade, position size) between
an external reference backtest and the platform's own backtest engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.models import Trade
from validation.comparison.comparison import FieldComparisonResult
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    TradeDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import TradeMatcher
from validation.reference import ValidationReference
from validation.risk.adapter import RiskAdapter
from validation.risk.fixed_risk import FixedRiskConfig
from validation.risk.models import RiskSizingInput, SizingModel, TradeRiskProfile
from validation.risk.percent_risk import PercentRiskConfig

logger = logging.getLogger(__name__)


def _pct_diff(a: float, b: float) -> float:
    """Relative difference using max(|a|, |b|) as denominator."""
    denom = max(abs(a), abs(b))
    if denom == 0.0:
        return 0.0 if a == b else 1.0
    return abs(a - b) / denom


def _abs_diff(a: float, b: float) -> float:
    return abs(a - b)


@dataclass
class RiskComparisonConfig:
    """Tolerances and sizing parameters used by :class:`RiskComparator`."""

    stop_distance_pct: float = 0.01
    stop_distance_abs: float | None = None
    risk_per_trade_pct: float = 0.01
    risk_per_trade_abs: float | None = None
    position_size_pct: float = 0.01
    position_size_abs: float | None = None
    time_tolerance_seconds: int = 0
    match_symbol: bool = True
    account_balance: float = 100_000.0
    sizing_model: SizingModel = SizingModel.FIXED_RISK
    model_config: FixedRiskConfig | PercentRiskConfig | None = None
    #: Phantom (actual-without-expected) trades fail the comparison by
    #: default (#85). A strict count threshold is explicit opt-in only —
    #: mirrors ``ReadinessConfig.max_extra_trades`` (default 0).
    max_extra_trades: int = 0


@dataclass
class RiskComparisonResult:
    """Result of comparing risk profiles for a single trade pair."""

    expected_trade: Trade | None = None
    actual_trade: Trade | None = None
    expected_risk_profile: TradeRiskProfile | None = None
    actual_risk_profile: TradeRiskProfile | None = None
    field_results: dict[str, FieldComparisonResult] = field(default_factory=dict)
    passed: bool = False
    severity: str = "info"
    explanation: str = ""


@dataclass
class RiskComparisonReport:
    """Aggregated risk comparison report across all trades."""

    trade_results: list[RiskComparisonResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    explanation: str = ""


class RiskComparator:
    """Compare risk profiles between a reference backtest and platform trades.

    Trades are paired via :class:`TradeMatcher`.  For each pair a
    :class:`TradeRiskProfile` is computed on both sides using
    :class:`RiskAdapter` and the configured sizing model.  The profiles are
    then compared field-by-field using the tolerances in
    :class:`RiskComparisonConfig`.
    """

    def __init__(
        self,
        config: RiskComparisonConfig | None = None,
        adapter: RiskAdapter | None = None,
    ) -> None:
        self.config = config if config is not None else RiskComparisonConfig()
        self.adapter = adapter if adapter is not None else RiskAdapter()

    def compare(
        self,
        expected: ValidationReference | list[Trade],
        actual: ValidationReference | list[Trade],
    ) -> RiskComparisonReport:
        """Run the full risk comparison and return a report."""
        expected_trades = (
            expected.trades if isinstance(expected, ValidationReference) else expected
        )
        actual_trades = (
            actual.trades if isinstance(actual, ValidationReference) else actual
        )

        match_result = TradeMatcher.match(
            reference_trades=expected_trades,
            actual_trades=actual_trades,
            time_tolerance_seconds=self.config.time_tolerance_seconds,
            match_symbol=self.config.match_symbol,
        )

        results: list[RiskComparisonResult] = []

        for ref_trade, act_trade, _key in match_result.matched:
            exp_profile = self._compute_profile(ref_trade)
            act_profile = self._compute_profile(act_trade)
            results.append(
                self._compare_pair(ref_trade, act_trade, exp_profile, act_profile)
            )

        for ref_trade in match_result.missing:
            profile = self._compute_profile(ref_trade)
            results.append(
                RiskComparisonResult(
                    expected_trade=ref_trade,
                    actual_trade=None,
                    expected_risk_profile=profile,
                    actual_risk_profile=None,
                    field_results={},
                    passed=False,
                    # C4: missing flips the risk gate — error, not warning.
                    severity="error",
                    explanation=(
                        f"Expected trade {ref_trade.id} has no matching actual trade"
                    ),
                )
            )

        # Phantom trades (#85): an actual trade with no expected counterpart
        # used to pass as `info` — certifying agreement that was never
        # checked. Fail by default; a strict threshold is explicit opt-in.
        extra_trades = list(match_result.extra)
        extra_passed = len(extra_trades) <= max(0, int(self.config.max_extra_trades))
        for act_trade in extra_trades:
            profile = self._compute_profile(act_trade)
            results.append(
                RiskComparisonResult(
                    expected_trade=None,
                    actual_trade=act_trade,
                    expected_risk_profile=None,
                    actual_risk_profile=profile,
                    field_results={},
                    passed=extra_passed,
                    severity="info" if extra_passed else "error",
                    explanation=(
                        f"Actual trade {act_trade.id} has no matching expected trade"
                        + (
                            ""
                            if extra_passed
                            else " (phantom trade: exceeds max_extra_trades="
                            f"{self.config.max_extra_trades})"
                        )
                    ),
                )
            )

        return self._build_report(results)

    def _compute_profile(self, trade: Trade) -> TradeRiskProfile | None:
        """Build a risk profile for *trade* using the configured adapter."""
        stop_loss = getattr(trade, "stop_loss", None)
        if stop_loss is None and trade.meta:
            for _key in ("stop_loss", "sl_price", "stop_price"):
                stop_loss = trade.meta.get(_key)
                if stop_loss is not None:
                    break

        take_profit = getattr(trade, "take_profit", None)
        if take_profit is None and trade.meta:
            for _key in ("take_profit", "tp_price"):
                take_profit = trade.meta.get(_key)
                if take_profit is not None:
                    break

        if stop_loss is None:
            return None

        try:
            sizing_input = RiskSizingInput(
                entry_price=float(trade.entry_price),
                stop_loss=float(stop_loss),
                take_profit=float(take_profit) if take_profit is not None else None,
                direction=trade.direction,
                trade=trade,
                account_balance=self.config.account_balance,
            )
            result = self.adapter.size(
                sizing_input,
                model=self.config.sizing_model,
                config=self.config.model_config,
            )
            return result.profiles[0] if result.profiles else None
        except Exception as exc:
            # DV-B48: a sizing failure (e.g. a sub-minimum position size that
            # now fails closed) must not vanish — a missing profile fails the
            # comparison structurally, so record why it is missing.
            logger.warning(
                "Risk profile for trade %s could not be computed: %s",
                trade.id,
                exc,
            )
            return None

    def _compare_pair(
        self,
        ref_trade: Trade,
        act_trade: Trade,
        exp_profile: TradeRiskProfile | None,
        act_profile: TradeRiskProfile | None,
    ) -> RiskComparisonResult:
        """Compare risk profiles of a matched trade pair."""
        field_results: dict[str, FieldComparisonResult] = {}

        if exp_profile is None or act_profile is None:
            # C4: sizing failures are error, not warning (global fail-closed
            # rule — an uncomputable profile fails the risk gate).
            return RiskComparisonResult(
                expected_trade=ref_trade,
                actual_trade=act_trade,
                expected_risk_profile=exp_profile,
                actual_risk_profile=act_profile,
                field_results=field_results,
                passed=False,
                severity="error",
                explanation="Could not compute risk profile for one or both trades",
            )

        field_results["stop_distance"] = self._compare_numeric(
            exp_profile.stop_distance,
            act_profile.stop_distance,
            self.config.stop_distance_pct,
            self.config.stop_distance_abs,
            "stop_distance",
        )
        field_results["risk_per_trade"] = self._compare_numeric(
            exp_profile.risk_amount,
            act_profile.risk_amount,
            self.config.risk_per_trade_pct,
            self.config.risk_per_trade_abs,
            "risk_per_trade",
        )
        field_results["position_size"] = self._compare_numeric(
            exp_profile.position_size,
            act_profile.position_size,
            self.config.position_size_pct,
            self.config.position_size_abs,
            "position_size",
        )

        mismatches = [
            name for name, res in field_results.items() if not res.passed
        ]
        passed = len(mismatches) == 0
        # C4: a failed pair flips the risk gate — error, not warning.
        severity = "info" if passed else "error"

        if passed:
            explanation = (
                f"Trade {ref_trade.id} (expected) / {act_trade.id} (actual) "
                f"risk profiles match"
            )
        else:
            explanation = (
                f"Trade {ref_trade.id} (expected) / {act_trade.id} (actual) "
                f"has risk mismatches: {', '.join(mismatches)}"
            )

        return RiskComparisonResult(
            expected_trade=ref_trade,
            actual_trade=act_trade,
            expected_risk_profile=exp_profile,
            actual_risk_profile=act_profile,
            field_results=field_results,
            passed=passed,
            severity=severity,
            explanation=explanation,
        )

    def _compare_numeric(
        self,
        expected: float | None,
        actual: float | None,
        tolerance_pct: float,
        tolerance_abs: float | None,
        field_name: str,
    ) -> FieldComparisonResult:
        """Compare a single numeric field with percentage and optional absolute tolerance."""
        if expected is None and actual is None:
            return FieldComparisonResult(
                field=field_name,
                expected=expected,
                actual=actual,
                tolerance=(tolerance_pct, tolerance_abs),
                passed=True,
                message=f"{field_name}: both None (match)",
            )
        if expected is None or actual is None:
            return FieldComparisonResult(
                field=field_name,
                expected=expected,
                actual=actual,
                tolerance=(tolerance_pct, tolerance_abs),
                passed=False,
                message=(
                    f"{field_name}: expected {expected!r}, got {actual!r} "
                    f"(mismatch)"
                ),
            )

        diff_pct = _pct_diff(expected, actual)
        diff_abs = _abs_diff(expected, actual)
        passed_pct = diff_pct <= tolerance_pct
        passed_abs = True if tolerance_abs is None else diff_abs <= tolerance_abs
        passed = passed_pct and passed_abs

        msg = (
            f"{field_name} relative difference is {diff_pct:.6f} "
            f"(tolerance {tolerance_pct:.6f})"
        )
        if tolerance_abs is not None:
            msg += f", absolute diff {diff_abs:.6f} (tolerance {tolerance_abs})"
        msg += " (match)" if passed else " (mismatch)"

        return FieldComparisonResult(
            field=field_name,
            expected=expected,
            actual=actual,
            tolerance=(tolerance_pct, tolerance_abs),
            passed=passed,
            message=msg,
        )

    def _build_report(
        self, results: list[RiskComparisonResult]
    ) -> RiskComparisonReport:
        """Aggregate individual results into a report."""
        total = len(results)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = total - passed_count
        severity_counts: dict[str, int] = {}
        for r in results:
            severity_counts[r.severity] = severity_counts.get(r.severity, 0) + 1

        summary = {
            "total_trades": total,
            "passed": passed_count,
            "failed": failed_count,
            "severity_counts": severity_counts,
        }

        passed = all(r.passed for r in results)

        if passed:
            explanation = (
                f"All {passed_count} trade risk profile(s) match within tolerance"
            )
        else:
            parts: list[str] = []
            if failed_count:
                parts.append(f"{failed_count} failed trade(s)")
            for sev, count in severity_counts.items():
                if sev != "info" and count > 0:
                    parts.append(f"{count} {sev}(s)")
            issues = ", ".join(parts)
            explanation = f"Risk validation failed: {issues}."

        return RiskComparisonReport(
            trade_results=results,
            summary=summary,
            passed=passed,
            explanation=explanation,
        )


class RiskReportBuilder:
    """Convert a :class:`RiskComparisonReport` to a :class:`TradeDeltaReport`."""

    @classmethod
    def to_delta_report(
        cls,
        report: RiskComparisonReport,
        reference_id: str = "",
        platform_id: str = "",
    ) -> TradeDeltaReport:
        """Transform risk comparison results into the canonical delta report format."""
        matched: list[TradeDelta] = []
        missing: list[TradeDelta] = []
        extra: list[TradeDelta] = []

        for result in report.trade_results:
            delta = cls._to_trade_delta(result)
            if result.expected_trade is not None and result.actual_trade is not None:
                matched.append(delta)
            elif result.expected_trade is not None:
                missing.append(delta)
            else:
                extra.append(delta)

        return TradeDeltaReport(
            reference_id=reference_id,
            platform_id=platform_id,
            matched_trades=matched,
            missing_trades=missing,
            extra_trades=extra,
            summary=report.summary,
            passed=report.passed,
            explanation=report.explanation,
        )

    @classmethod
    def _to_trade_delta(cls, result: RiskComparisonResult) -> TradeDelta:
        """Convert a single :class:`RiskComparisonResult` to a :class:`TradeDelta`."""
        field_deltas = [
            DeltaReportBuilder._field_result_to_delta(fcr)
            for fcr in result.field_results.values()
        ]

        exp_id = str(result.expected_trade.id) if result.expected_trade else None
        act_id = str(result.actual_trade.id) if result.actual_trade else None
        trade_id = f"{exp_id}_{act_id}" if exp_id and act_id else (exp_id or act_id)

        anomaly_type: str | None = None
        if result.expected_trade is not None and result.actual_trade is not None:
            anomaly_type = None if result.passed else "mismatch"
        elif result.expected_trade is not None:
            anomaly_type = "missing"
        else:
            anomaly_type = "extra"

        return TradeDelta(
            trade_id=trade_id,
            expected_trade_id=exp_id,
            actual_trade_id=act_id,
            anomaly_type=anomaly_type,
            field_deltas=field_deltas,
            severity=result.severity,
            explanation=result.explanation,
        )
