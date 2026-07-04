"""JSON report generation for strategy validation.

Produces a fully-structured, agent-friendly JSON report containing
all deltas, summaries and explanations.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.models import BacktestMeta, BacktestResult, Trade
from validation.comparison.anomalies import AnomalyDetector
from validation.comparison.comparison import MatchedTrade, TradeComparator
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    FieldDelta,
    TradeDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import TradeMatcher
from validation.reference import ValidationReference
from validation.stats.calculator import TradeStatistics, compute_trade_statistics


class _SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that converts non-finite floats to None."""

    def encode(self, o: Any) -> str:
        return super().encode(_sanitize_for_json(o))

    def iterencode(self, o: Any, **kwargs: Any):
        return super().iterencode(_sanitize_for_json(o), **kwargs)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively sanitize non-finite floats to None for JSON serialization."""
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return None  # Convert nan, inf, -inf to None
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


def _stats_from_reference(reference: ValidationReference) -> TradeStatistics:
    """Build TradeStatistics from reference.stats when no trades are present."""
    allowed_fields = set(TradeStatistics.model_fields.keys())
    filtered = {k: v for k, v in reference.stats.items() if k in allowed_fields}
    return TradeStatistics(**filtered)

# Statistics fields compared when no pre-computed stats_delta is provided.
_STATS_FIELDS = [
    "win_rate",
    "profit_factor",
    "net_profit",
    "gross_profit",
    "gross_loss",
    "max_drawdown",
    "max_drawdown_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "recovery_factor",
]


def _is_numeric(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _abs_diff(a: float, b: float) -> float:
    return abs(a - b)


def _rel_diff(expected: float, actual: float) -> float:
    denom = max(abs(expected), abs(actual))
    if denom == 0.0:
        return 0.0 if expected == actual else 1.0
    return abs(expected - actual) / denom


def _build_stat_explanation(
    field: str,
    expected: Any,
    actual: Any,
    abs_diff: float | None,
    rel_diff: float | None,
    passed: bool,
) -> str:
    """Build a human-readable explanation for a stat delta."""
    if passed:
        if rel_diff is not None:
            return (
                f"{field} matches within tolerance (expected {expected}, actual {actual}, "
                f"relative diff {rel_diff:.6f})"
            )
        return f"{field} matches (expected {expected!r}, actual {actual!r})"

    parts: list[str] = []
    if _is_numeric(expected) and _is_numeric(actual):
        if rel_diff is not None:
            parts.append(f"differs by {rel_diff:.4%}")
        if abs_diff is not None:
            parts.append(f"absolute diff {abs_diff:.6f}")
    else:
        parts.append("mismatch")

    issues = ", ".join(parts)
    return f"{field} {issues}: expected {expected}, actual {actual}"


def _compare_stats(
    expected: TradeStatistics,
    actual: TradeStatistics,
    abs_tol: float = 0.01,
    rel_tol: float = 0.001,
) -> list[StatDelta]:
    """Compare two TradeStatistics objects and return StatDeltas."""
    deltas: list[StatDelta] = []
    for field in _STATS_FIELDS:
        exp_val = getattr(expected, field)
        act_val = getattr(actual, field)

        # Gracefully handle infinities
        if exp_val == float("inf") and act_val == float("inf"):
            deltas.append(
                StatDelta(
                    field=field,
                    expected=exp_val,
                    actual=act_val,
                    absolute_diff=0.0,
                    relative_diff=0.0,
                    passed=True,
                    explanation=f"{field} matches (both infinite)",
                    severity="info",
                )
            )
            continue
        if exp_val == float("inf") or act_val == float("inf"):
            deltas.append(
                StatDelta(
                    field=field,
                    expected=exp_val,
                    actual=act_val,
                    absolute_diff=None,
                    relative_diff=None,
                    passed=False,
                    explanation=f"{field} differs (expected {exp_val}, actual {act_val})",
                    severity="warning",
                )
            )
            continue

        abs_diff: float | None = None
        rel_diff: float | None = None
        if _is_numeric(exp_val) and _is_numeric(act_val):
            abs_diff = _abs_diff(float(exp_val), float(act_val))
            denom = max(abs(float(exp_val)), abs(float(act_val)))
            if denom == 0.0:
                rel_diff = 0.0 if exp_val == act_val else 1.0
            else:
                rel_diff = abs(float(exp_val) - float(act_val)) / denom

        passed = False
        checks = 0
        if abs_diff is not None:
            checks += 1
            passed = passed or (abs_diff <= abs_tol)
        if rel_diff is not None:
            checks += 1
            passed = passed or (rel_diff <= rel_tol)
        if checks == 0:
            passed = exp_val == act_val

        explanation = _build_stat_explanation(
            field, exp_val, act_val, abs_diff, rel_diff, passed
        )
        severity = "info" if passed else "warning"

        deltas.append(
            StatDelta(
                field=field,
                expected=exp_val,
                actual=act_val,
                absolute_diff=abs_diff,
                relative_diff=rel_diff,
                passed=passed,
                explanation=explanation,
                severity=severity,
            )
        )
    return deltas


class StatDelta(BaseModel):
    """A single stat-level deviation between expected and actual values."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    field: str
    expected: Any
    actual: Any
    absolute_diff: float | None = None
    relative_diff: float | None = None
    passed: bool = False
    explanation: str = ""
    severity: str = "info"


class ReportSummary(BaseModel):
    """Count summary for a report section."""

    matched: int = 0
    missing: int = 0
    extra: int = 0
    mismatched: int = 0
    passed: int = 0
    errors: int = 0
    warnings: int = 0


class ValidationReport(BaseModel):
    """Top-level agent-friendly validation report."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    strategy_name: str
    symbol: str
    timeframe: str
    source_framework: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    passed: bool = False

    trade_passed: bool = False
    stat_passed: bool = False
    risk_passed: bool = True

    trade_summary: ReportSummary = Field(default_factory=ReportSummary)
    stat_summary: ReportSummary = Field(default_factory=ReportSummary)

    trade_deltas: list[TradeDelta] = Field(default_factory=list)
    stat_deltas: list[StatDelta] = Field(default_factory=list)
    risk_deltas: list[StatDelta] = Field(default_factory=list)

    anomalies: list[TradeDelta] = Field(default_factory=list)
    explanations: list[str] = Field(default_factory=list)


def _field_delta_to_stat_delta(fd: FieldDelta) -> StatDelta:
    """Convert a FieldDelta (e.g. from regression runner) into a StatDelta."""
    return StatDelta(
        field=fd.field,
        expected=fd.expected,
        actual=fd.actual,
        absolute_diff=fd.absolute_diff,
        relative_diff=fd.relative_diff,
        passed=fd.passed,
        explanation=fd.explanation,
        severity="info" if fd.passed else "warning",
    )


def _to_backtest_result(
    platform_result: ValidationReference | list[Trade] | BacktestResult,
) -> BacktestResult:
    """Normalise platform_result to a BacktestResult for downstream builders."""
    if isinstance(platform_result, BacktestResult):
        return platform_result
    if isinstance(platform_result, ValidationReference):
        return platform_result.to_backtest_result()

    meta = BacktestMeta(
        id="platform_unknown",
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        engine="unknown",
        strategies=["unknown"],
    )
    return BacktestResult(meta=meta, trades=platform_result, stats={})


def _build_trade_delta_report(
    reference: ValidationReference,
    platform_result: BacktestResult,
    time_tolerance_seconds: int = 0,
    trade_comparison_config: TradeComparisonConfig | None = None,
) -> TradeDeltaReport:
    """Run the full trade matching / comparison / anomaly pipeline."""
    match_result = TradeMatcher.match(
        reference.trades, platform_result.trades,
        time_tolerance_seconds=time_tolerance_seconds,
    )

    comparison_results: list[Any] = []
    for ref_trade, act_trade, key in match_result.matched:
        matched = MatchedTrade(reference=ref_trade, actual=act_trade, key=key)
        comparator = TradeComparator(config=trade_comparison_config)
        comparison_results.append(comparator.compare(matched))

    anomalies = AnomalyDetector.detect(
        reference, platform_result.trades, comparison_results,
        time_tolerance_seconds=time_tolerance_seconds,
    )

    return DeltaReportBuilder.build(
        reference, platform_result, comparison_results, anomalies
    )


def _extract_risk_deltas(risk_delta_report: Any) -> list[StatDelta]:
    """Extract StatDeltas from a RiskComparisonReport (or similar)."""
    risk_deltas: list[StatDelta] = []
    if risk_delta_report is None:
        return risk_deltas

    # Accept RiskComparisonReport (has trade_results with field_results)
    if hasattr(risk_delta_report, "trade_results"):
        for result in risk_delta_report.trade_results:
            if hasattr(result, "field_results"):
                for fcr in result.field_results.values():
                    risk_deltas.append(
                        StatDelta(
                            field=f"risk_{fcr.field}",
                            expected=fcr.expected,
                            actual=fcr.actual,
                            passed=fcr.passed,
                            explanation=fcr.message,
                            severity="warning" if not fcr.passed else "info",
                        )
                    )
    # Accept a plain list of StatDelta / FieldDelta
    elif isinstance(risk_delta_report, list):
        for item in risk_delta_report:
            if isinstance(item, StatDelta):
                risk_deltas.append(item)
            elif isinstance(item, FieldDelta):
                risk_deltas.append(_field_delta_to_stat_delta(item))

    return risk_deltas


def generate_json_report(
    reference: ValidationReference,
    platform_result: ValidationReference | list[Trade],
    trade_delta_report: TradeDeltaReport | None = None,
    stats_delta: list[StatDelta] | list[FieldDelta] | None = None,
    risk_delta_report: Any | None = None,
    time_tolerance_seconds: int = 0,
    trade_comparison_config: TradeComparisonConfig | None = None,
    stats_abs_tol: float = 0.01,
    stats_rel_tol: float = 0.001,
) -> str:
    """Generate a complete JSON validation report.

    Parameters
    ----------
    reference:
        Ground-truth ``ValidationReference`` (external framework).
    platform_result:
        Platform results as ``ValidationReference`` or a plain list of ``Trade``.
    trade_delta_report:
        Optional pre-computed trade delta report.
    stats_delta:
        Optional pre-computed stat deltas (``StatDelta`` or ``FieldDelta``).
    risk_delta_report:
        Optional pre-computed risk report (``RiskComparisonReport`` or list).

    Returns
    -------
    str
        Pretty-printed JSON string.
    """
    platform_bt = _to_backtest_result(platform_result)

    # --- trade deltas --------------------------------------------------
    if trade_delta_report is None:
        trade_delta_report = _build_trade_delta_report(
            reference, platform_bt,
            time_tolerance_seconds=time_tolerance_seconds,
            trade_comparison_config=trade_comparison_config,
        )

    # --- stat deltas ---------------------------------------------------
    if stats_delta is None:
        if reference.trades:
            ref_stats = compute_trade_statistics(reference.trades)
        elif reference.stats:
            ref_stats = _stats_from_reference(reference)
        else:
            ref_stats = compute_trade_statistics(reference.trades)
        if platform_bt.trades:
            act_stats = compute_trade_statistics(platform_bt.trades)
        else:
            platform_raw_stats: dict[str, Any] | None = None
            if isinstance(platform_result, ValidationReference):
                platform_raw_stats = platform_result.stats
            elif hasattr(platform_result, 'stats'):
                platform_raw_stats = getattr(platform_result, 'stats')
            elif isinstance(platform_result, dict):
                platform_raw_stats = platform_result.get('stats')
            if platform_raw_stats:
                allowed_fields = set(TradeStatistics.model_fields.keys())
                filtered = {k: v for k, v in platform_raw_stats.items() if k in allowed_fields}
                act_stats = TradeStatistics(**filtered)
            else:
                act_stats = compute_trade_statistics(platform_bt.trades)
        stat_deltas: list[StatDelta] = _compare_stats(
            ref_stats, act_stats, abs_tol=stats_abs_tol, rel_tol=stats_rel_tol
        )
    else:
        stat_deltas = [
            _field_delta_to_stat_delta(d) if isinstance(d, FieldDelta) else d
            for d in stats_delta
        ]

    # --- risk deltas ---------------------------------------------------
    risk_deltas = _extract_risk_deltas(risk_delta_report)

    # --- summaries -----------------------------------------------------
    all_trade_deltas = (
        trade_delta_report.matched_trades
        + trade_delta_report.missing_trades
        + trade_delta_report.extra_trades
    )
    trade_summary = ReportSummary(
        matched=trade_delta_report.summary.get("total_matched", 0),
        missing=len(trade_delta_report.missing_trades),
        extra=len(trade_delta_report.extra_trades),
        mismatched=sum(
            1 for d in trade_delta_report.matched_trades if d.anomaly_type is not None
        ),
        passed=1 if trade_delta_report.passed else 0,
        errors=sum(1 for d in all_trade_deltas if d.severity == "error"),
        warnings=sum(1 for d in all_trade_deltas if d.severity == "warning"),
    )

    stat_summary = ReportSummary(
        matched=sum(1 for d in stat_deltas if d.passed),
        missing=0,
        extra=0,
        mismatched=sum(1 for d in stat_deltas if not d.passed),
        passed=1 if all(d.passed for d in stat_deltas) else 0,
        errors=0,
        warnings=sum(1 for d in stat_deltas if not d.passed),
    )

    # --- anomalies -----------------------------------------------------
    anomalies = (
        list(trade_delta_report.missing_trades)
        + list(trade_delta_report.extra_trades)
        + [
            d
            for d in trade_delta_report.matched_trades
            if d.anomaly_type is not None
        ]
    )

    # --- explanations --------------------------------------------------
    explanations: list[str] = [trade_delta_report.explanation]
    stat_issues = sum(1 for d in stat_deltas if not d.passed)
    if stat_issues:
        explanations.append(
            f"Stats deviations: {stat_issues} field(s) out of tolerance."
        )
    if risk_deltas:
        risk_issues = sum(1 for d in risk_deltas if not d.passed)
        if risk_issues:
            explanations.append(
                f"Risk deviations: {risk_issues} field(s) out of tolerance."
            )

    # --- overall passed ------------------------------------------------
    trade_passed = trade_delta_report.passed
    stat_passed = all(d.passed for d in stat_deltas)
    risk_passed = all(d.passed for d in risk_deltas) if risk_deltas else True
    overall_passed = trade_passed and stat_passed and risk_passed

    report = ValidationReport(
        strategy_name=reference.strategy_name,
        symbol=reference.symbol,
        timeframe=reference.timeframe,
        source_framework=reference.source_framework,
        passed=overall_passed,
        trade_passed=trade_passed,
        stat_passed=stat_passed,
        risk_passed=risk_passed,
        trade_summary=trade_summary,
        stat_summary=stat_summary,
        trade_deltas=trade_delta_report.matched_trades,
        stat_deltas=stat_deltas,
        risk_deltas=risk_deltas,
        anomalies=anomalies,
        explanations=explanations,
    )

    return json.dumps(
        report.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        cls=_SafeJSONEncoder,
    )


def _build_recommendation(data: dict[str, Any]) -> list[str]:
    """Build a short recommendation paragraph based on the report data."""
    lines: list[str] = []
    passed = data.get("passed", False)
    trade_passed = data.get("trade_passed", False)
    stat_passed = data.get("stat_passed", False)

    if passed:
        lines.append(
            "All validation checks passed. The platform backtest results "
            "are consistent with the reference framework."
        )
        return lines

    issues: list[str] = []
    if not trade_passed:
        ts = data.get("trade_summary", {})
        if ts.get("missing", 0) > 0:
            issues.append("missing trades")
        if ts.get("extra", 0) > 0:
            issues.append("extra trades")
        if ts.get("mismatched", 0) > 0:
            issues.append("trade field mismatches")
        if ts.get("errors", 0) > 0:
            issues.append("error-level discrepancies")
    if not stat_passed:
        issues.append("statistical deviations")

    if issues:
        lines.append(
            f"Review required: {', '.join(issues)}. "
            "Please investigate the discrepancies noted above."
        )
    else:
        lines.append("Review required. Please investigate the discrepancies noted above.")

    return lines


def generate_markdown_report(
    reference: ValidationReference | ValidationReport,
    platform_result: ValidationReference | list[Trade] | None = None,
    trade_delta_report: TradeDeltaReport | None = None,
    stats_delta: list[StatDelta] | list[FieldDelta] | None = None,
    risk_delta_report: Any | None = None,
    time_tolerance_seconds: int = 0,
    trade_comparison_config: TradeComparisonConfig | None = None,
    stats_abs_tol: float = 0.01,
    stats_rel_tol: float = 0.001,
) -> str:
    """Generate a human-readable Markdown validation report.

    Can be called either with a pre-built ``ValidationReport`` as the sole
    argument, or with the same arguments as :func:`generate_json_report`.
    """
    if isinstance(reference, ValidationReport):
        report = reference
        data = report.model_dump(mode="json")
    else:
        if platform_result is None:
            raise ValueError(
                "platform_result is required when reference is not a ValidationReport"
            )
        json_str = generate_json_report(
            reference=reference,
            platform_result=platform_result,
            trade_delta_report=trade_delta_report,
            stats_delta=stats_delta,
            risk_delta_report=risk_delta_report,
            time_tolerance_seconds=time_tolerance_seconds,
            trade_comparison_config=trade_comparison_config,
            stats_abs_tol=stats_abs_tol,
            stats_rel_tol=stats_rel_tol,
        )
        data = json.loads(json_str)

    lines: list[str] = []
    lines.append(f"# Validation Report: {data.get('strategy_name', 'Unknown')}")
    lines.append("")
    lines.append(f"- **Symbol**: {data.get('symbol')}")
    lines.append(f"- **Timeframe**: {data.get('timeframe')}")
    lines.append(f"- **Source Framework**: {data.get('source_framework')}")
    lines.append(
        f"- **Generated At**: {data.get('generated_at', datetime.now(timezone.utc).isoformat())}"
    )
    lines.append(f"- **Overall Passed**: {'Yes' if data.get('passed') else 'No'}")
    lines.append("")

    # ── Summary ──────────────────────────────────────────────────────────
    lines.append("## Summary")
    trade_summary = data.get("trade_summary", {})
    total_trades = (
        trade_summary.get("matched", 0)
        + trade_summary.get("missing", 0)
        + trade_summary.get("extra", 0)
    )
    lines.append(f"- **Passed**: {'Yes' if data.get('passed') else 'No'}")
    lines.append(f"- **Total Trades**: {total_trades}")
    lines.append(f"- **Matched**: {trade_summary.get('matched', 0)}")
    lines.append(f"- **Missing**: {trade_summary.get('missing', 0)}")
    lines.append(f"- **Extra**: {trade_summary.get('extra', 0)}")
    lines.append(f"- **Mismatched**: {trade_summary.get('mismatched', 0)}")
    lines.append(f"- **Errors**: {trade_summary.get('errors', 0)}")
    lines.append(f"- **Warnings**: {trade_summary.get('warnings', 0)}")
    lines.append("")

    # ── Statistics Summary ───────────────────────────────────────────────
    lines.append("## Statistics Summary")
    stat_summary = data.get("stat_summary", {})
    lines.append(f"- **Matched Fields**: {stat_summary.get('matched', 0)}")
    lines.append(f"- **Mismatched Fields**: {stat_summary.get('mismatched', 0)}")
    lines.append(f"- **Warnings**: {stat_summary.get('warnings', 0)}")
    lines.append("")

    # ── Explanations ─────────────────────────────────────────────────────
    explanations = data.get("explanations", [])
    if explanations:
        lines.append("## Explanations")
        for exp in explanations:
            lines.append(f"- {exp}")
        lines.append("")

    # ── Trade Deltas ─────────────────────────────────────────────────────
    trade_deltas = data.get("trade_deltas", [])
    issue_deltas = [d for d in trade_deltas if d.get("anomaly_type")]
    if issue_deltas:
        lines.append("## Trade Deltas")
        for d in issue_deltas:
            severity = d.get("severity", "info")
            trade_id = d.get("trade_id") or d.get("expected_trade_id") or "unknown"
            explanation = d.get("explanation", "")
            lines.append(f"- **{trade_id}** ({severity}): {explanation}")
            for fd in d.get("field_deltas", []):
                if not fd.get("passed"):
                    fd_explanation = fd.get("explanation", "")
                    lines.append(f"  - {fd_explanation}")
        lines.append("")

    # ── Stat Deltas ──────────────────────────────────────────────────────
    stat_deltas = data.get("stat_deltas", [])
    bad_stats = [d for d in stat_deltas if not d.get("passed")]
    if bad_stats:
        lines.append("## Stat Deltas")
        for d in bad_stats:
            explanation = d.get("explanation", "")
            lines.append(f"- {explanation}")
        lines.append("")

    # ── Anomalies (Missing / Extra / Mismatched) ─────────────────────────
    anomalies = data.get("anomalies", [])
    if anomalies:
        lines.append("## Anomalies")
        # Group by anomaly type for readability
        grouped: dict[str, list[dict[str, Any]]] = {}
        for anom in anomalies:
            atype = anom.get("anomaly_type") or "unknown"
            grouped.setdefault(atype, []).append(anom)

        for atype in sorted(grouped.keys()):
            lines.append(f"### {atype.replace('_', ' ').title()}")
            for anom in grouped[atype]:
                lines.append(f"- {anom.get('explanation', '')}")
            lines.append("")
        lines.append("")

    # ── Conclusion / Recommendation ──────────────────────────────────────
    lines.append("## Conclusion")
    lines.extend(_build_recommendation(data))
    lines.append("")

    return "\n".join(lines)
