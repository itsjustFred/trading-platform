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
REFERENCE_RISK_FREE_RATE = 0.0
MT5_REPORT_SOURCE_TIMEZONE = "Europe/Athens"


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


@dataclass(frozen=True)
class MT5ComparisonCase:
    case_id: str
    reference_path: Path
    platform_path: Path
    source_timezone: str


def build_comparison_matrix(
    *,
    comparisons_dir: Path,
    reports_dir: Path,
    platform_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Compare MT5 and QuantConnect references with available platform artifacts."""
    comparisons_dir = comparisons_dir.resolve()
    reports_dir = reports_dir.resolve()
    platform_data_dir = (platform_data_dir or reports_dir.parent / "data" / "backtests").resolve()
    rows: list[dict[str, Any]] = []

    mt5_cases = [
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_sp500_m1",
            reference_path=comparisons_dir / "intraday_volatility_breakout" / "SP500_M1.xlsx",
            platform_path=platform_data_dir / "intraday_volatility_breakout_sp500_m1.json",
            source_timezone=MT5_REPORT_SOURCE_TIMEZONE,
        ),
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_eurusd_h1",
            reference_path=comparisons_dir / "intraday_volatility_breakout" / "EURUSD_H1.xlsx",
            platform_path=platform_data_dir / "intraday_volatility_breakout_eurusd_h1.json",
            source_timezone=MT5_REPORT_SOURCE_TIMEZONE,
        ),
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_nas100_m15",
            reference_path=comparisons_dir / "intraday_volatility_breakout" / "NAS100_M15.xlsx",
            platform_path=platform_data_dir / "intraday_volatility_breakout_nas100_m15.json",
            source_timezone=MT5_REPORT_SOURCE_TIMEZONE,
        ),
    ]

    for case in mt5_cases:
        rows.append(_build_mt5_row(case))

    rows.append(
        _build_json_reference_row(
            "risk_parity_quantconnect",
            comparisons_dir / "risk_parity" / "risk_parity_reference.json",
            platform_data_dir / "risk_parity_quantconnect_reimplementation.json",
        )
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": list(AUDIT_METRICS),
        "risk_free_rate_note": (
            "Cross-platform Sharpe recomputation uses daily equity returns and "
            "risk_free_rate=0.0 because the MT5 and QuantConnect reference reports "
            "do not expose a matching risk-free-rate input. Platform runtime stats "
            "may still use a nonzero application default."
        ),
        "rows": rows,
    }


def _build_mt5_row(case: MT5ComparisonCase) -> dict[str, Any]:
    reference = MT5XLSXImporter().load(case.reference_path, source_timezone=case.source_timezone)
    reference_stats = _normalize_mt5_stats(reference.stats, case.reference_path, reference.raw_metadata)
    platform_stats = _load_platform_stats(case.platform_path, risk_free_rate=REFERENCE_RISK_FREE_RATE)
    row = _row_payload(
        case_id=case.case_id,
        reference_framework="mt5_xlsx",
        reference_path=case.reference_path,
        platform_path=case.platform_path,
        reference_stats=reference_stats,
        platform_stats=platform_stats,
        notes=[
            "MT5 fills, spread/commission, contract sizing and fixed-risk execution differ from platform fills.",
            f"MT5 trade timestamps are interpreted as {case.source_timezone} local time and converted to UTC.",
            "MT5 CAGR is derived from report period, initial deposit and net profit because the XLSX summary omits CAGR.",
        ],
    )
    row["reference_source_timezone"] = case.source_timezone
    return row


def _build_json_reference_row(case_id: str, reference_path: Path, platform_path: Path) -> dict[str, Any]:
    reference_payload = json.loads(reference_path.read_text(encoding="utf-8"))
    reference_stats = _normalize_reference_stats(reference_payload.get("stats", {}))
    platform_stats = _load_platform_stats(platform_path, risk_free_rate=REFERENCE_RISK_FREE_RATE)
    return _row_payload(
        case_id=case_id,
        reference_framework=str(reference_payload.get("source_framework") or "reference_json"),
        reference_path=reference_path,
        platform_path=platform_path,
        reference_stats=reference_stats,
        platform_stats=platform_stats,
        notes=[
            "QuantConnect PDF summary has limited extractable trade detail; JSON reference carries the auditable fields.",
            "For qc-parity platform artifacts, win_rate and net_profit use explicit rebalance-attribution fields when present.",
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
    if reference == 0.0:
        if platform == 0.0:
            return MetricDelta(metric, reference, platform, absolute, 0.0)
        # #128: no proportion exists for a zero reference; report the absolute
        # delta and leave the relative one undefined rather than inventing one.
        return MetricDelta(
            metric,
            reference,
            platform,
            absolute,
            None,
            "relative delta is undefined for a zero reference",
        )
    # #128: signed drift against the reference magnitude — max(|reference|,
    # |platform|) halved real drift (100 -> 200 read as 50%, not 100%).
    return MetricDelta(
        metric, reference, platform, absolute, absolute / abs(reference)
    )


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
        "trade_count": _first_metric(stats, "total_trades", "trade_count"),
        "win_rate": win_rate,
        "net_profit": _first_metric(stats, "net_profit", "total_pnl"),
        "cagr": _safe_float(stats.get("cagr")),
        "sharpe_ratio": _first_metric(stats, "sharpe_ratio", "sharpe"),
    }


def _load_platform_stats(
    platform_path: Path,
    *,
    risk_free_rate: float = REFERENCE_RISK_FREE_RATE,
) -> dict[str, float | None]:
    if not platform_path.exists():
        return {metric: None for metric in AUDIT_METRICS}

    payload = json.loads(platform_path.read_text(encoding="utf-8"))
    result = BacktestResult.from_dict(payload)
    from runner.stats import compute_stats_for_result

    # #84: the audit matrix always recomputes canonical stats — preferring
    # stored/curated numbers for qc-parity tags compared curated, not
    # canonical, numbers across platforms. The canonical dict publishes
    # net total as total_pnl; map it onto the audit's net_profit key so a
    # stale stored net_profit can never survive the merge.
    canonical = compute_stats_for_result(result, risk_free_rate=risk_free_rate)
    stats = {
        **dict(result.stats or {}),
        **canonical,
    }
    if "total_pnl" in canonical:
        stats["net_profit"] = canonical["total_pnl"]
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


def _first_metric(stats: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if stats.get(key) is not None:
            return _safe_float(stats.get(key))
    return None
