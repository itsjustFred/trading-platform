"""Cross-platform metric comparison helpers.

Builds an auditable matrix between platform backtest artifacts and external
reference reports.  The helpers are intentionally file-based so they can run in
CI without the live SQLite store or broker/data credentials.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared.models import BacktestResult
from validation.importers.mt5_xlsx import MT5XLSXImporter

AUDIT_METRICS = ("trade_count", "win_rate", "net_profit", "cagr", "sharpe_ratio")


@dataclass(frozen=True)
class MetricDelta:
    metric: str
    reference: float | None
    platform: float | None
    absolute_delta: float | None
    relative_delta: float | None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "reference": self.reference,
            "platform": self.platform,
            "absolute_delta": self.absolute_delta,
            "relative_delta": self.relative_delta,
            "note": self.note,
        }


def build_comparison_matrix(
    *,
    comparisons_dir: Path,
    reports_dir: Path,
) -> dict[str, Any]:
    """Compare MT5 and QuantConnect references with available platform artifacts."""
    comparisons_dir = comparisons_dir.resolve()
    reports_dir = reports_dir.resolve()
    rows: list[dict[str, Any]] = []

    mt5_cases = [
        (
            "intraday_volatility_breakout_sp500_m1",
            comparisons_dir / "intraday_volatility_breakout" / "SP500_M1.xlsx",
            reports_dir / "strategy_comparison_backtest_SP500_M1_after_align_20260623.trades.json",
        ),
        (
            "intraday_volatility_breakout_eurusd_h1",
            comparisons_dir / "intraday_volatility_breakout" / "EURUSD_H1.xlsx",
            reports_dir / "strategy_comparison_backtest_EURUSD_H1_after_align_20260623.trades.json",
        ),
        (
            "intraday_volatility_breakout_nas100_m15",
            comparisons_dir / "intraday_volatility_breakout" / "NAS100_M15.xlsx",
            reports_dir / "strategy_comparison_backtest_US100_M15_after_align_20260623.trades.json",
        ),
    ]

    for case_id, reference_path, platform_path in mt5_cases:
        rows.append(_build_mt5_row(case_id, reference_path, platform_path))

    rows.append(
        _build_json_reference_row(
            "risk_parity_quantconnect",
            comparisons_dir / "risk_parity" / "risk_parity_reference.json",
            reports_dir / "strategy_comparison_backtest_risk_parity_20260623.trades.json",
        )
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": list(AUDIT_METRICS),
        "risk_free_rate_note": (
            "Platform canonical Sharpe uses daily equity returns and risk_free_rate=0.04 "
            "by default; MT5 and QuantConnect reference reports do not expose a matching "
            "risk-free-rate input, so Sharpe deltas include R_f and execution-model effects."
        ),
        "rows": rows,
    }


def _build_mt5_row(case_id: str, reference_path: Path, platform_path: Path) -> dict[str, Any]:
    reference = MT5XLSXImporter().load(reference_path)
    reference_stats = _normalize_mt5_stats(reference.stats, reference_path, reference.raw_metadata)
    platform_stats = _load_platform_stats(platform_path)
    return _row_payload(
        case_id=case_id,
        reference_framework="mt5_xlsx",
        reference_path=reference_path,
        platform_path=platform_path,
        reference_stats=reference_stats,
        platform_stats=platform_stats,
        notes=[
            "MT5 fills, spread/commission, contract sizing and fixed-risk execution differ from platform fills.",
            "MT5 CAGR is derived from report period, initial deposit and net profit because the XLSX summary omits CAGR.",
        ],
    )


def _build_json_reference_row(case_id: str, reference_path: Path, platform_path: Path) -> dict[str, Any]:
    reference_payload = json.loads(reference_path.read_text(encoding="utf-8"))
    reference_stats = _normalize_reference_stats(reference_payload.get("stats", {}))
    platform_stats = _load_platform_stats(platform_path)
    return _row_payload(
        case_id=case_id,
        reference_framework=str(reference_payload.get("source_framework") or "reference_json"),
        reference_path=reference_path,
        platform_path=platform_path,
        reference_stats=reference_stats,
        platform_stats=platform_stats,
        notes=[
            "QuantConnect PDF summary has limited extractable trade detail; JSON reference carries the auditable fields.",
            "Risk Parity implementations may differ in universe, rebalance timing, covariance estimator and R_f convention.",
        ],
    )


def _row_payload(
    *,
    case_id: str,
    reference_framework: str,
    reference_path: Path,
    platform_path: Path,
    reference_stats: dict[str, float | None],
    platform_stats: dict[str, float | None],
    notes: list[str],
) -> dict[str, Any]:
    deltas = [
        _metric_delta(metric, reference_stats.get(metric), platform_stats.get(metric))
        for metric in AUDIT_METRICS
    ]
    missing = []
    if not reference_path.exists():
        missing.append(str(reference_path))
    if not platform_path.exists():
        missing.append(str(platform_path))

    return {
        "case_id": case_id,
        "reference_framework": reference_framework,
        "reference_path": str(reference_path),
        "platform_path": str(platform_path),
        "missing_artifacts": missing,
        "reference": reference_stats,
        "platform": platform_stats,
        "deltas": [delta.to_dict() for delta in deltas],
        "notes": notes,
    }


def _metric_delta(
    metric: str,
    reference: float | None,
    platform: float | None,
) -> MetricDelta:
    if reference is None or platform is None:
        return MetricDelta(metric, reference, platform, None, None, "missing metric")
    absolute = platform - reference
    denominator = max(abs(reference), abs(platform))
    relative = absolute / denominator if denominator else 0.0
    return MetricDelta(metric, reference, platform, absolute, relative)


def _normalize_mt5_stats(
    stats: dict[str, Any],
    reference_path: Path,
    raw_metadata: dict[str, Any],
) -> dict[str, float | None]:
    normalized = _normalize_reference_stats(stats)
    meta = raw_metadata.get("parsed_meta", {}) if isinstance(raw_metadata, dict) else {}
    initial_capital = _safe_float(meta.get("initial_capital"))
    period = _extract_mt5_period(reference_path)
    if normalized["cagr"] is None and initial_capital and normalized["net_profit"] is not None and period:
        start, end = period
        days = (end - start).total_seconds() / (24 * 60 * 60)
        ending = initial_capital + normalized["net_profit"]
        if days > 0 and initial_capital > 0 and ending > 0:
            normalized["cagr"] = (ending / initial_capital) ** (365.25 / days) - 1.0
    return normalized


def _normalize_reference_stats(stats: dict[str, Any]) -> dict[str, float | None]:
    win_rate = _safe_float(stats.get("win_rate"))
    if win_rate is not None and win_rate > 1.0:
        win_rate /= 100.0

    return {
        "trade_count": _safe_float(stats.get("total_trades") or stats.get("trade_count")),
        "win_rate": win_rate,
        "net_profit": _safe_float(
            stats.get("net_profit")
            if stats.get("net_profit") is not None
            else stats.get("total_pnl")
        ),
        "cagr": _safe_float(stats.get("cagr")),
        "sharpe_ratio": _safe_float(stats.get("sharpe_ratio") or stats.get("sharpe")),
    }


def _load_platform_stats(platform_path: Path) -> dict[str, float | None]:
    if not platform_path.exists():
        return {metric: None for metric in AUDIT_METRICS}

    payload = json.loads(platform_path.read_text(encoding="utf-8"))
    result = BacktestResult.from_dict(payload)
    from runner.stats import compute_stats_for_result

    stats = {**dict(result.stats or {}), **compute_stats_for_result(result)}
    normalized = _normalize_reference_stats(stats)
    if normalized["net_profit"] is None:
        normalized["net_profit"] = _safe_float(stats.get("total_pnl"))
    return normalized


def _extract_mt5_period(path: Path) -> tuple[datetime, datetime] | None:
    try:
        df = pd.read_excel(path, sheet_name=0, header=None, nrows=12, dtype=object)
    except Exception:
        return None

    pattern = re.compile(r"\((\d{4}\.\d{2}\.\d{2})\s*-\s*(\d{4}\.\d{2}\.\d{2})\)")
    for value in df.to_numpy().ravel():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        match = pattern.search(str(value))
        if not match:
            continue
        start = datetime.strptime(match.group(1), "%Y.%m.%d").replace(tzinfo=timezone.utc)
        end = datetime.strptime(match.group(2), "%Y.%m.%d").replace(tzinfo=timezone.utc)
        return start, end
    return None


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number
