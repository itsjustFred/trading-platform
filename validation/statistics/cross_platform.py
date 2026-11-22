"""Cross-platform metric comparison helpers.

Builds an auditable matrix between platform backtest artifacts and external
reference reports.  The helpers are intentionally file-based so they can run in
CI without the live SQLite store or broker/data credentials.

MT5 reports print trade times on the broker's server clock, not in a civil time
zone, and that clock has changed its UTC offset several times (see
``shared/broker_clock.py``).  Each MT5 case therefore names the broker clock its
report was produced on instead of assuming a fixed timezone.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from shared.models import BacktestResult
from validation.importers.mt5_xlsx import MT5XLSXImporter

AUDIT_METRICS = ("trade_count", "win_rate", "net_profit", "cagr", "sharpe_ratio")
REFERENCE_RISK_FREE_RATE = 0.0
# ~100 years of daily equity points is already degenerate for a parity
# curve; anything longer means a corrupt timestamp, not a real strategy.
_MAX_PARITY_CURVE_DAYS = 366 * 100


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
    broker_clock: str


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
            broker_clock="dukascopy_mt5_index",
        ),
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_eurusd_h1",
            reference_path=comparisons_dir / "intraday_volatility_breakout" / "EURUSD_H1.xlsx",
            platform_path=platform_data_dir / "intraday_volatility_breakout_eurusd_h1.json",
            broker_clock="dukascopy_mt5_forex",
        ),
        MT5ComparisonCase(
            case_id="intraday_volatility_breakout_nas100_m15",
            reference_path=comparisons_dir / "intraday_volatility_breakout" / "NAS100_M15.xlsx",
            platform_path=platform_data_dir / "intraday_volatility_breakout_nas100_m15.json",
            broker_clock="dukascopy_mt5_index",
        ),
    ]

    rows.extend(_build_mt5_row(case) for case in mt5_cases)

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
    reference = MT5XLSXImporter().load(case.reference_path, broker_clock=case.broker_clock)
    reference_stats = _normalize_mt5_stats(
        reference.stats, case.reference_path, reference.raw_metadata
    )
    platform_stats = _load_platform_stats(
        case.platform_path, risk_free_rate=REFERENCE_RISK_FREE_RATE
    )
    # #229 maintainer decision: Sharpe parity must apply OUR formula to BOTH
    # equity curves — the platform's and the imported MT5 one — not compare
    # against MT5's printed Sharpe-Ratio, which uses MT5's own convention.
    # The printed value stays visible as ``sharpe_ratio_reported`` for audit;
    # ``sharpe_ratio`` becomes the same-formula recomputation (None when the
    # trade list cannot support an equity curve — fail closed, never fall
    # back to the printed ratio).
    reference_stats["sharpe_ratio_reported"] = reference_stats.get("sharpe_ratio")
    platform_context = _platform_return_context(case.platform_path)
    reference_stats["sharpe_ratio"] = _reference_sharpe_ratio(
        reference, **platform_context
    )
    # Sharpe parity compares realized-exit curves on BOTH sides: the
    # platform artifact's canonical Sharpe is mark-to-market, so it is kept
    # for audit as ``sharpe_ratio_mtm`` while ``sharpe_ratio`` becomes the
    # same-formula recomputation on a realized-exit curve rebuilt from the
    # platform trade list — like for like with the imported MT5 curve.
    platform_stats["sharpe_ratio_mtm"] = platform_stats.get("sharpe_ratio")
    platform_stats["sharpe_ratio"] = _platform_realized_sharpe_ratio(
        case.platform_path, platform_context
    )
    row = _row_payload(
        case_id=case.case_id,
        reference_framework="mt5_xlsx",
        reference_path=case.reference_path,
        platform_path=case.platform_path,
        reference_stats=reference_stats,
        platform_stats=platform_stats,
        notes=[
            (
                "MT5 fills, spread/commission, contract sizing and fixed-risk "
                "execution differ from platform fills."
            ),
            (
                "MT5 trade timestamps are the broker's server wall clock "
                f"({case.broker_clock}, shared/broker_clock.py) and are converted to UTC "
                "through that clock's historical offset segments."
            ),
            (
                "MT5 CAGR is derived from report period, initial deposit and "
                "net profit because the XLSX summary omits CAGR."
            ),
            (
                "Sharpe parity applies the platform's trading-day filtering "
                "and asset-class annualisation (252 equities/FX/CFDs, 365 "
                "crypto) to realized-exit equity curves rebuilt from BOTH "
                "trade lists, so the inputs are like for like; the platform's "
                "mark-to-market Sharpe is kept as sharpe_ratio_mtm and the "
                "MT5 printed Sharpe-Ratio as sharpe_ratio_reported — neither "
                "is the comparison basis."
            ),
        ],
    )
    row["reference_broker_clock"] = case.broker_clock
    return row


def _platform_return_context(platform_path: Path) -> dict[str, Any]:
    """Venue context of the platform artifact the MT5 curve is compared to.

    Sharpe parity applies OUR formula to both curves, so the imported MT5
    curve must resolve the same trading-day calendar and annualisation
    convention (252 equities/FX/CFDs, 365 crypto — #229 maintainer decision
    2026-11-22) as the platform run. The platform artifact's own
    ``meta.data`` carries that context (``execution_context`` first, then
    the top-level ``data`` fields); an absent or unreadable artifact yields
    an empty context and the reference falls back to its own symbol.
    """
    try:
        payload = json.loads(platform_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    meta_data = ((payload.get("meta") or {}).get("data")) or {}
    if not isinstance(meta_data, dict):
        return {}
    context = meta_data.get("execution_context") or {}
    if not isinstance(context, dict):
        context = {}
    symbols = context.get("symbols") or meta_data.get("symbols") or []
    data_source = context.get("data_source") or meta_data.get("source")
    data_dir = context.get("data_dir") or meta_data.get("data_dir")
    # The platform side recovers a book-level asset class from the OHLCV
    # path layout when metadata does not carry one; mirror that here so the
    # imported curve resolves the identical annualisation convention.
    from runner.stats import _book_asset_class

    symbol_list = [str(symbol) for symbol in symbols if str(symbol or "").strip()]
    return {
        "symbols": symbol_list,
        "data_source": data_source,
        "asset_class": _book_asset_class(
            meta_data, context, symbol_list, data_source, data_dir
        ),
        "data_dir": data_dir,
    }


def _reference_sharpe_ratio(reference: Any, **context: Any) -> float | None:
    """Platform Sharpe formula applied to the imported MT5 trade list."""
    from validation.reference import resolve_initial_capital

    return _realized_exit_sharpe_ratio(
        reference.trades,
        initial_capital=resolve_initial_capital(reference),
        fallback_symbols=[getattr(reference, "symbol", "") or ""],
        **context,
    )


def _platform_realized_sharpe_ratio(
    platform_path: Path, context: dict[str, Any]
) -> float | None:
    """Platform Sharpe formula on a realized-exit curve from the artifact's
    own trade list — the same curve shape ``_reference_sharpe_ratio`` builds
    for the MT5 side, so identical trade lists give identical Sharpes."""
    try:
        payload = json.loads(platform_path.read_text(encoding="utf-8"))
        result = BacktestResult.from_dict(payload)
    except Exception:
        return None
    meta_data = getattr(result.meta, "data", None) or {}
    fallback_symbols = (
        [str(symbol) for symbol in meta_data.get("symbols") or []]
        if isinstance(meta_data, dict)
        else []
    )
    initial_capital = getattr(result.meta, "initial_capital", 0.0)
    if not (
        isinstance(initial_capital, (int, float))
        and math.isfinite(initial_capital)
        and initial_capital > 0
    ):
        initial_capital = 100_000.0
    return _realized_exit_sharpe_ratio(
        result.trades,
        initial_capital=float(initial_capital),
        fallback_symbols=fallback_symbols,
        **context,
    )


def _realized_exit_sharpe_ratio(
    trades: list[Any],
    *,
    initial_capital: float,
    fallback_symbols: list[str] | None = None,
    **context: Any,
) -> float | None:
    """Platform Sharpe formula applied to a trade list's realized exits.

    Neither side of the parity check carries a like-for-like equity graph
    (the MT5 XLSX report has none at all, and the platform artifact's curve
    is mark-to-market), so the curve is rebuilt
    from the trade list: initial capital plus cumulative net PnL booked at
    each position's last exit — the same realized-equity evidence the trade
    list is the primary carrier of. ``compute_stats`` then applies the
    identical trading-day filtering and asset-class annualisation (252 for
    equities/FX/CFDs, 365 for 24/7 crypto — the platform artifact's own
    venue context is passed through ``**context``) used for the platform
    artifact, which is what makes the two Sharpes comparable (#229).

    The rebuilt curve is calendar-filled — one point per calendar day, flat
    between exits — because that is the curve shape production publishes:
    ``attach_daily_equity`` emits a point for every calendar day. An
    exits-only curve would space its observations by exit frequency and
    annualise on a different basis than the platform side, which is exactly
    the mismatch this comparison exists to remove; the venue calendar drops
    the weekend/holiday filler rows on both sides identically.

    Fails closed (``None``) when the trade list cannot support the curve:
    fewer than two exits, an unparseable exit timestamp, or a non-finite
    PnL — a silently thinned curve would compare a different strategy.
    """
    from runner.stats import compute_stats
    from shared.models import EquityPoint

    exits: list[tuple[pd.Timestamp, float]] = []
    for trade in trades:
        if not trade.exits:
            continue
        exit_ts = pd.to_datetime(str(trade.exits[-1].time), utc=True, errors="coerce")
        if pd.isna(exit_ts):
            return None
        if trade.net_pnl is not None:
            pnl = float(trade.net_pnl)
        else:
            pnl = float(trade.pnl or 0.0) - float(trade.fees or 0.0)
        if not math.isfinite(pnl):
            return None
        exits.append((pd.Timestamp(exit_ts), pnl))
    if len(exits) < 2:
        return None
    exits.sort(key=lambda item: item[0])

    start_candidates = []
    for trade in trades:
        entry_ts = pd.to_datetime(str(trade.entry_time), utc=True, errors="coerce")
        if not pd.isna(entry_ts):
            start_candidates.append(pd.Timestamp(entry_ts))
    start = min(start_candidates) if start_candidates else exits[0][0]

    pnl_by_day: dict[Any, float] = {}
    for exit_ts, pnl in exits:
        day = exit_ts.date()
        pnl_by_day[day] = pnl_by_day.get(day, 0.0) + pnl

    points = [EquityPoint(time=start.isoformat(), equity=float(initial_capital))]
    equity = float(initial_capital)
    day = start.date()
    last_day = exits[-1][0].date()
    if (last_day - day).days > _MAX_PARITY_CURVE_DAYS:
        # A corrupt far-future exit timestamp would otherwise make the
        # per-day loop below unbounded; the curve is degenerate data, so
        # fail closed like the other unusable-trade-list cases.
        return None
    while day <= last_day:
        equity += pnl_by_day.get(day, 0.0)
        # End-of-day stamp: always after the day's own timestamps, so the
        # daily resample in compute_stats books that day's exits on that day.
        points.append(
            EquityPoint(
                time=datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).isoformat(),
                equity=equity,
            )
        )
        day += timedelta(days=1)

    symbols = context.get("symbols") or list(fallback_symbols or [])
    stats = compute_stats(
        [],
        initial_capital=initial_capital,
        equity_curve=points,
        risk_free_rate=REFERENCE_RISK_FREE_RATE,
        symbols=[s for s in symbols if s] or None,
        data_source=context.get("data_source"),
        asset_class=context.get("asset_class"),
        data_dir=context.get("data_dir"),
    )
    sharpe = _safe_float(stats.get("sharpe_ratio"))
    return sharpe


def _build_json_reference_row(
    case_id: str, reference_path: Path, platform_path: Path
) -> dict[str, Any]:
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
    return MetricDelta(metric, reference, platform, absolute, absolute / abs(reference))


def _normalize_mt5_stats(
    stats: dict[str, Any],
    reference_path: Path,
    raw_metadata: dict[str, Any],
) -> dict[str, float | None]:
    normalized = _normalize_reference_stats(stats)
    meta = raw_metadata.get("parsed_meta", {}) if isinstance(raw_metadata, dict) else {}
    initial_capital = _safe_float(meta.get("initial_capital"))
    period = _extract_mt5_period(reference_path)
    if (
        normalized["cagr"] is None
        and initial_capital
        and normalized["net_profit"] is not None
        and period
    ):
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
