"""Delta report generation for strategy validation.

Builds a human-readable, structured report containing all differences
between a reference backtest and the platform's backtest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.models import BacktestResult
from validation.comparison.anomalies import AnomalyType, TradeAnomaly
from validation.comparison.comparison import (
    FieldComparisonResult,
    TradeComparisonResult,
)
from validation.reference import ValidationReference


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _abs_diff(a: float, b: float) -> float:
    return abs(a - b)


def _rel_diff(expected: float, actual: float) -> float:
    denom = max(abs(expected), abs(actual))
    if denom == 0.0:
        return 0.0 if expected == actual else 1.0
    return abs(expected - actual) / denom


def _build_field_explanation(field_delta: FieldDelta) -> str:
    """Build a human-readable explanation for a FieldDelta."""
    field = field_delta.field
    expected = field_delta.expected
    actual = field_delta.actual
    tolerance = field_delta.tolerance
    passed = field_delta.passed
    abs_d = field_delta.absolute_diff
    rel_d = field_delta.relative_diff

    if passed:
        if _is_numeric(expected) and _is_numeric(actual) and rel_d is not None:
            return (
                f"{field} matches within tolerance (expected {expected}, actual {actual}, "
                f"relative diff {rel_d:.6f}, tolerance {tolerance!r})"
            )
        return f"{field} matches (expected {expected!r}, actual {actual!r})"

    if _is_numeric(expected) and _is_numeric(actual):
        if abs_d is not None and rel_d is not None:
            return (
                f"{field} differs by {rel_d:.4%} (expected {expected}, "
                f"actual {actual}, tolerance {tolerance!r}, "
                f"absolute diff {abs_d:.6f})"
            )
        return (
            f"{field} differs (expected {expected!r}, actual {actual!r}, "
            f"tolerance {tolerance!r})"
        )

    if tolerance is not None:
        return (
            f"{field} mismatch: expected {expected!r}, got {actual!r} "
            f"(tolerance {tolerance!r})"
        )
    return f"{field} mismatch: expected {expected!r}, got {actual!r}"


class FieldDelta(BaseModel):
    """A single field-level deviation between expected and actual values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    field: str
    expected: Any | None = None
    actual: Any | None = None
    absolute_diff: float | None = None
    relative_diff: float | None = None
    tolerance: Any | None = None
    passed: bool = False
    explanation: str = ""


class TradeDelta(BaseModel):
    """A single trade-level deviation in the comparison report."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trade_id: str | None = None
    expected_trade_id: str | None = None
    actual_trade_id: str | None = None
    anomaly_type: str | None = None
    field_deltas: list[FieldDelta] = Field(default_factory=list)
    severity: str = "info"
    explanation: str = ""


class TradeDeltaReport(BaseModel):
    """A comprehensive report of all differences between reference and platform backtests."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    reference_id: str
    platform_id: str
    matched_trades: list[TradeDelta] = Field(default_factory=list)
    missing_trades: list[TradeDelta] = Field(default_factory=list)
    extra_trades: list[TradeDelta] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    passed: bool = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    explanation: str = ""


class DeltaReportBuilder:
    """Builds a TradeDeltaReport from comparison results and anomalies."""

    @staticmethod
    def _field_result_to_delta(fcr: FieldComparisonResult) -> FieldDelta:
        """Convert a FieldComparisonResult to a FieldDelta with diffs and explanation."""
        abs_d: float | None = None
        rel_d: float | None = None
        if _is_numeric(fcr.expected) and _is_numeric(fcr.actual):
            abs_d = _abs_diff(float(fcr.expected), float(fcr.actual))
            rel_d = _rel_diff(float(fcr.expected), float(fcr.actual))

        field_delta = FieldDelta(
            field=fcr.field,
            expected=fcr.expected,
            actual=fcr.actual,
            absolute_diff=abs_d,
            relative_diff=rel_d,
            tolerance=fcr.tolerance,
            passed=fcr.passed,
            explanation="",
        )
        field_delta.explanation = _build_field_explanation(field_delta)
        return field_delta

    @classmethod
    def _comparison_result_to_delta(cls, comp: TradeComparisonResult) -> TradeDelta:
        """Convert a TradeComparisonResult into a TradeDelta."""
        ref = comp.matched_trade.reference
        act = comp.matched_trade.actual
        field_deltas = [
            cls._field_result_to_delta(fr) for fr in comp.field_results.values()
        ]

        if comp.is_match:
            severity = "info"
            explanation = (
                f"Trade id={ref.id} (expected) / id={act.id} (actual) matches perfectly"
            )
        else:
            error_fields = {"direction", "pnl"}
            has_error = any(name in error_fields for name in comp.mismatches)
            severity = "error" if has_error else "warning"
            mismatch_str = ", ".join(comp.mismatches)
            explanation = (
                f"Trade id={ref.id} (expected) / id={act.id} (actual) has "
                f"mismatches: {mismatch_str}"
            )

        return TradeDelta(
            trade_id=f"{ref.id}_{act.id}",
            expected_trade_id=str(ref.id),
            actual_trade_id=str(act.id),
            anomaly_type=None if comp.is_match else "mismatch",
            field_deltas=field_deltas,
            severity=severity,
            explanation=explanation,
        )

    @classmethod
    def _anomaly_to_delta(cls, anomaly: TradeAnomaly) -> TradeDelta:
        """Convert a TradeAnomaly into a TradeDelta."""
        if anomaly.anomaly_type == AnomalyType.MISSING_TRADE:
            trade = anomaly.expected_trade
            trade_id = str(trade.id) if trade else None
            return TradeDelta(
                trade_id=trade_id,
                expected_trade_id=trade_id,
                actual_trade_id=None,
                anomaly_type="missing",
                severity=anomaly.severity,
                explanation=anomaly.message,
            )
        if anomaly.anomaly_type == AnomalyType.EXTRA_TRADE:
            trade = anomaly.actual_trade
            trade_id = str(trade.id) if trade else None
            return TradeDelta(
                trade_id=trade_id,
                expected_trade_id=None,
                actual_trade_id=trade_id,
                anomaly_type="extra",
                severity=anomaly.severity,
                explanation=anomaly.message,
            )
        if anomaly.anomaly_type == AnomalyType.MISMATCHED_TRADE:
            mt = anomaly.matched_trade
            ref = mt.reference if mt else None
            act = mt.actual if mt else None
            field_deltas = [
                cls._field_result_to_delta(fcr)
                for fcr in anomaly.field_mismatches
            ]
            return TradeDelta(
                trade_id=f"{ref.id}_{act.id}" if ref and act else None,
                expected_trade_id=str(ref.id) if ref else None,
                actual_trade_id=str(act.id) if act else None,
                anomaly_type="mismatch",
                field_deltas=field_deltas,
                severity=anomaly.severity,
                explanation=anomaly.message,
            )
        if anomaly.anomaly_type == AnomalyType.DUPLICATE_MATCH:
            mt = anomaly.matched_trade
            ref = mt.reference if mt else None
            act = mt.actual if mt else None
            return TradeDelta(
                trade_id=f"{ref.id}_{act.id}" if ref and act else None,
                expected_trade_id=str(ref.id) if ref else None,
                actual_trade_id=str(act.id) if act else None,
                anomaly_type="duplicate_match",
                severity=anomaly.severity,
                explanation=anomaly.message,
            )
        return TradeDelta(
            severity=anomaly.severity,
            explanation=anomaly.message,
        )

    @classmethod
    def build(
        cls,
        reference: ValidationReference,
        platform_result: BacktestResult,
        comparison_results: list[TradeComparisonResult],
        anomalies: list[TradeAnomaly],
    ) -> TradeDeltaReport:
        """Build a TradeDeltaReport from all validation artifacts."""
        reference_id = (
            f"{reference.source_framework}_{reference.symbol}_{reference.timeframe}"
        )
        platform_id = platform_result.meta.id

        # Convert comparison results to matched trade deltas
        matched_trades: list[TradeDelta] = []
        for comp in comparison_results:
            matched_trades.append(cls._comparison_result_to_delta(comp))

        # Convert anomalies to appropriate buckets
        missing_trades: list[TradeDelta] = []
        extra_trades: list[TradeDelta] = []
        for anomaly in anomalies:
            delta = cls._anomaly_to_delta(anomaly)
            if delta.anomaly_type == "missing":
                missing_trades.append(delta)
            elif delta.anomaly_type == "extra":
                extra_trades.append(delta)
            elif delta.anomaly_type == "duplicate_match":
                matched_trades.append(delta)

        # Determine passed: no errors, no missing, no extra
        all_deltas = matched_trades + missing_trades + extra_trades
        has_error = any(d.severity == "error" for d in all_deltas)
        has_missing = len(missing_trades) > 0
        has_extra = len(extra_trades) > 0
        passed = not has_error and not has_missing and not has_extra

        # Build summary
        severity_counts: dict[str, int] = {}
        anomaly_counts: dict[str, int] = {}
        for delta in all_deltas:
            severity_counts[delta.severity] = severity_counts.get(delta.severity, 0) + 1
            atype = delta.anomaly_type or "unknown"
            anomaly_counts[atype] = anomaly_counts.get(atype, 0) + 1

        total_expected = len(reference.trades)
        total_actual = len(platform_result.trades)
        total_matched = sum(
            1 for d in matched_trades if d.anomaly_type not in ("duplicate_match",)
        )

        summary: dict[str, Any] = {
            "total_expected": total_expected,
            "total_actual": total_actual,
            "total_matched": total_matched,
            "severity_counts": severity_counts,
            "anomaly_counts": anomaly_counts,
        }

        # Build explanation
        if passed:
            explanation = (
                f"Validation passed: {total_matched} of {total_expected} trades "
                f"matched perfectly, {len(missing_trades)} missing, "
                f"{len(extra_trades)} extra."
            )
        else:
            parts: list[str] = []
            if has_missing:
                parts.append(f"{len(missing_trades)} missing trade(s)")
            if has_extra:
                parts.append(f"{len(extra_trades)} extra trade(s)")
            if severity_counts.get("error", 0):
                parts.append(f"{severity_counts['error']} error-level discrepancy(s)")
            if severity_counts.get("warning", 0):
                parts.append(f"{severity_counts['warning']} warning-level discrepancy(s)")
            issues = ", ".join(parts)
            explanation = (
                f"Validation failed: {total_matched} matched, "
                f"{len(missing_trades)} missing, {len(extra_trades)} extra. "
                f"Issues: {issues}."
            )

        return TradeDeltaReport(
            reference_id=reference_id,
            platform_id=platform_id,
            matched_trades=matched_trades,
            missing_trades=missing_trades,
            extra_trades=extra_trades,
            summary=summary,
            passed=passed,
            explanation=explanation,
        )
