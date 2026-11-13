"""Regression runner: compare golden backtests against current platform output."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from validation.comparison.anomalies import AnomalyDetector
from validation.comparison.comparison import (
    MatchedTrade,
    TradeComparator,
    TradeComparisonConfig,
)
from validation.comparison.delta_report import (
    DeltaReportBuilder,
    FieldDelta,
    TradeDeltaReport,
)
from validation.comparison.matcher import TradeMatcher
from validation.comparison.tolerances import finite_tolerance, relative_difference
from validation.reference import ValidationReference
from validation.stats.calculator import TradeStatistics, compute_trade_statistics

# Statistics fields that are compared between golden and current output.
_STATS_FIELDS = [
    "risk_free_rate",
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


def _infinite_like(v: Any) -> bool:
    """True when *v* is a non-finite float (+inf, -inf or nan)."""
    return isinstance(v, float) and not math.isfinite(v)


def _build_stat_explanation(
    field: str,
    expected: float,
    actual: float,
    abs_tol: float | None,
    rel_tol: float | None,
    abs_diff: float | None,
    rel_diff: float | None,
    passed: bool,
) -> str:
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

    tol_parts: list[str] = []
    if abs_tol is not None:
        tol_parts.append(f"abs tol {abs_tol}")
    if rel_tol is not None:
        tol_parts.append(f"rel tol {rel_tol}")

    issues = ", ".join(parts)
    tols = f" (tolerance: {', '.join(tol_parts)})" if tol_parts else ""
    return f"{field} {issues}{tols}: expected {expected}, actual {actual}"


def _compare_stats(
    golden: TradeStatistics,
    current: TradeStatistics,
    config: RegressionConfig,
) -> list[FieldDelta]:
    """Compare two TradeStatistics objects field-by-field.

    Gate semantics (DV-B35): a field passes only when every configured
    tolerance holds (AND).  Infinite values (DV-B34) compare symmetrically:
    identical infinities match, any other infinite combination fails.
    """
    deltas: list[FieldDelta] = []
    for field in _STATS_FIELDS:
        expected = getattr(golden, field)
        actual = getattr(current, field)

        # Handle infinities gracefully in either direction
        exp_inf = _infinite_like(expected)
        act_inf = _infinite_like(actual)
        if exp_inf or act_inf:
            if exp_inf and act_inf and expected == actual:
                deltas.append(
                    FieldDelta(
                        field=field,
                        expected=expected,
                        actual=actual,
                        passed=True,
                        explanation=f"{field} matches (both infinite: {expected})",
                    )
                )
            else:
                deltas.append(
                    FieldDelta(
                        field=field,
                        expected=expected,
                        actual=actual,
                        passed=False,
                        explanation=f"{field} differs (expected {expected}, actual {actual})",
                    )
                )
            continue

        abs_tol = config.stats_absolute_tolerance.get(
            field, config.default_stats_absolute_tolerance
        )
        rel_tol = config.stats_relative_tolerance.get(
            field, config.default_stats_relative_tolerance
        )

        abs_diff: float | None = None
        rel_diff: float | None = None

        if _is_numeric(expected) and _is_numeric(actual):
            abs_diff = abs(float(expected) - float(actual))
            rel_diff = relative_difference(float(expected), float(actual))

        # Tolerance bands: every *configured* band must hold (DV-B35 - AND,
        # not OR).  A 0.0 band is therefore exact equality, not "disabled":
        # only a band that is genuinely absent (None) is skipped.
        passed = True
        checks = 0
        if abs_tol is not None and abs_diff is not None:
            checks += 1
            passed = abs_diff <= abs_tol
        if rel_tol is not None and rel_diff is not None:
            checks += 1
            passed = passed and (rel_diff <= rel_tol)
        if checks == 0:
            passed = expected == actual
        explanation = _build_stat_explanation(
            field, expected, actual, abs_tol, rel_tol, abs_diff, rel_diff, passed
        )
        deltas.append(
            FieldDelta(
                field=field,
                expected=expected,
                actual=actual,
                absolute_diff=abs_diff,
                relative_diff=rel_diff,
                tolerance=(abs_tol, rel_tol),
                passed=passed,
                explanation=explanation,
            )
        )
    return deltas


class RegressionConfig(BaseModel):
    """Configuration for a regression run."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    trade_config: TradeComparisonConfig = Field(default_factory=TradeComparisonConfig)
    # C4: matching tolerance, plumbed into TradeMatcher.match (previously
    # the default 0s demanded exact seconds while nothing forwarded it).
    match_time_tolerance_seconds: int = 0
    stats_absolute_tolerance: dict[str, float] = Field(default_factory=dict)
    stats_relative_tolerance: dict[str, float] = Field(default_factory=dict)
    # Tolerance semantics (DV-B35): every *configured* band must hold (AND).
    # A 0.0 band means exact equality, not "disabled"; a field with no band
    # at all falls back to exact equality.
    default_stats_absolute_tolerance: float = 0.01
    default_stats_relative_tolerance: float = 0.001
    golden_base_path: str = ""
    output_mode: Literal["json", "markdown", "both"] = "both"

    @model_validator(mode="after")
    def _validate_tolerances(self) -> "RegressionConfig":
        value = finite_tolerance(
            self.match_time_tolerance_seconds, "match_time_tolerance_seconds"
        )
        self.match_time_tolerance_seconds = int(value or 0)
        value = finite_tolerance(
            self.default_stats_absolute_tolerance,
            "default_stats_absolute_tolerance",
        )
        self.default_stats_absolute_tolerance = value or 0.0
        value = finite_tolerance(
            self.default_stats_relative_tolerance,
            "default_stats_relative_tolerance",
        )
        self.default_stats_relative_tolerance = value or 0.0
        for name, values in (
            ("stats_absolute_tolerance", self.stats_absolute_tolerance),
            ("stats_relative_tolerance", self.stats_relative_tolerance),
        ):
            for field, tolerance in values.items():
                values[field] = finite_tolerance(tolerance, f"{name}.{field}", optional=True)
        return self


class RegressionResult(BaseModel):
    """Complete result of a regression comparison."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    golden_path: str
    current_reference: ValidationReference
    trade_delta_report: TradeDeltaReport
    stats_delta: list[FieldDelta] = Field(default_factory=list)
    passed: bool = False
    deviations: list[Any] = Field(default_factory=list)
    explanation: str = ""


class RegressionReporter:
    """Produces machine-readable JSON and human-readable Markdown reports."""

    @staticmethod
    def to_json_report(result: RegressionResult) -> str:
        """Serialize a RegressionResult to a pretty-printed JSON string."""
        return json.dumps(
            result.model_dump(mode="json"),
            indent=2,
            ensure_ascii=False,
        )

    @staticmethod
    def to_markdown_report(result: RegressionResult) -> str:
        """Produce a Markdown report with explained deviations."""
        lines: list[str] = []
        lines.append("# Regression Report")
        lines.append("")
        lines.append(f"**Golden**: `{result.golden_path}`")
        lines.append(f"**Passed**: {'Yes' if result.passed else 'No'}")
        lines.append(
            f"**Generated**: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}"
        )
        lines.append("")
        lines.append("## Explanation")
        lines.append(result.explanation)
        lines.append("")

        tdr = result.trade_delta_report
        lines.append("## Trade Comparison")
        lines.append(
            f"- Expected trades: {tdr.summary.get('total_expected', 'N/A')}"
        )
        lines.append(f"- Actual trades: {tdr.summary.get('total_actual', 'N/A')}")
        lines.append(f"- Matched: {tdr.summary.get('total_matched', 'N/A')}")
        lines.append(f"- Missing: {len(tdr.missing_trades)}")
        lines.append(f"- Extra: {len(tdr.extra_trades)}")
        lines.append("")

        if tdr.missing_trades:
            lines.append("### Missing Trades")
            lines.extend(f"- {d.explanation}" for d in tdr.missing_trades)
            lines.append("")

        if tdr.extra_trades:
            lines.append("### Extra Trades")
            lines.extend(f"- {d.explanation}" for d in tdr.extra_trades)
            lines.append("")

        matched_issues = [d for d in tdr.matched_trades if d.anomaly_type]
        if matched_issues:
            lines.append("### Trade Mismatches")
            # Not a flat transformed list: each mismatch's explanation must be followed
            # by that mismatch's own field deltas, in that order, so a single
            # comprehension (or an `extend` per level) would regroup the report.
            # PERF401's premise does not hold on this loop.
            for d in matched_issues:  # noqa: PERF401
                lines.append(f"- {d.explanation}")
                lines.extend(
                    f"  - {fd.explanation}" for fd in d.field_deltas if not fd.passed
                )
            lines.append("")

        stats_issues = [d for d in result.stats_delta if not d.passed]
        lines.append("## Statistics Comparison")
        if stats_issues:
            lines.append("### Deviations")
            lines.extend(f"- {d.explanation}" for d in stats_issues)
        else:
            lines.append("All statistics match within tolerance.")
        lines.append("")

        return "\n".join(lines)


def run_regression(
    golden_path: str | Path,
    importer_class: type | None = None,
    current_data: ValidationReference | None = None,
    config: RegressionConfig | None = None,
) -> RegressionResult:
    """Run a full regression comparing a golden file against current output.

    Parameters
    ----------
    golden_path:
        Path to the golden JSON file.
    importer_class:
        Optional importer class used to re-import the source file referenced
        by the golden.  Ignored when *current_data* is provided directly.
    current_data:
        Optional pre-built current ``ValidationReference``.  Takes precedence
        over *importer_class*.
    config:
        Optional ``RegressionConfig`` with tolerances and output mode.

    Returns
    -------
    RegressionResult
        Structured result containing deltas, pass/fail status and reports.
    """
    cfg = config if config is not None else RegressionConfig()
    golden_path = Path(golden_path)
    if not golden_path.exists():
        raise FileNotFoundError(f"Golden file not found: {golden_path}")

    golden_text = golden_path.read_text(encoding="utf-8")
    golden_ref = ValidationReference.model_validate_json(golden_text)

    if current_data is not None:
        current_ref = current_data
    elif importer_class is not None:
        if not golden_ref.source_path:
            raise ValueError(
                "Golden file has no source_path; cannot re-import with importer."
            )
        # Resolve source_path relative to golden file if it's relative
        source_path = Path(golden_ref.source_path)
        if not source_path.is_absolute():
            source_path = (golden_path.parent / source_path).resolve()
        importer = importer_class()
        current_ref = importer.load(source_path)
    else:
        raise ValueError("Either importer_class or current_data must be provided.")

    platform_result = current_ref.to_backtest_result()

    # --- trade matching & comparison -----------------------------------
    match_result = TradeMatcher.match(
        golden_ref.trades,
        current_ref.trades,
        time_tolerance_seconds=cfg.match_time_tolerance_seconds,
    )

    comparison_results = []
    for ref_trade, act_trade, key in match_result.matched:
        matched = MatchedTrade(reference=ref_trade, actual=act_trade, key=key)
        comparator = TradeComparator(cfg.trade_config)
        comparison_results.append(comparator.compare(matched))

    anomalies = AnomalyDetector.detect(
        golden_ref,
        current_ref.trades,
        comparison_results,
        # CodeRabbit round: reuse the configured-tolerance match — the
        # default re-match inside detect() would use tolerance 0 and
        # contradict the matcher's own verdict.
        match_result=match_result,
    )

    trade_delta_report = DeltaReportBuilder.build(
        golden_ref, platform_result, comparison_results, anomalies
    )

    # --- statistics comparison ------------------------------------------
    golden_stats = compute_trade_statistics(
        golden_ref.trades,
        initial_capital=golden_ref.to_backtest_result().meta.initial_capital,
    )
    current_stats = compute_trade_statistics(
        current_ref.trades,
        initial_capital=current_ref.to_backtest_result().meta.initial_capital,
    )
    stats_deltas = _compare_stats(golden_stats, current_stats, cfg)

    has_trade_mismatch = any(
        d.anomaly_type is not None for d in trade_delta_report.matched_trades
    )
    trade_passed = trade_delta_report.passed and not has_trade_mismatch
    stats_passed = all(d.passed for d in stats_deltas)
    passed = trade_passed and stats_passed

    # --- assemble deviations -------------------------------------------
    deviations: list[Any] = []
    deviations.extend(trade_delta_report.matched_trades)
    deviations.extend(trade_delta_report.missing_trades)
    deviations.extend(trade_delta_report.extra_trades)
    deviations.extend(stats_deltas)

    # --- explanation ---------------------------------------------------
    parts: list[str] = []
    if trade_delta_report.explanation:
        parts.append(trade_delta_report.explanation)
    stats_issues = [d for d in stats_deltas if not d.passed]
    if stats_issues:
        parts.append(
            f"Stats deviations: {len(stats_issues)} field(s) out of tolerance."
        )

    if passed:
        explanation = (
            "Regression passed: trades and stats match within configured tolerances."
        )
    else:
        explanation = " ".join(parts) if parts else "Regression failed due to deviations."

    return RegressionResult(
        golden_path=str(golden_path),
        current_reference=current_ref,
        trade_delta_report=trade_delta_report,
        stats_delta=stats_deltas,
        passed=passed,
        deviations=deviations,
        explanation=explanation,
    )
