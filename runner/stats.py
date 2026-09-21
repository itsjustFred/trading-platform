"""
Statistics module — computes performance metrics from a list of Trades.

Uses QuantStats for equity-curve-based metrics (Sharpe, Sortino, Calmar, etc.)
and manual calculations for trade-based metrics.

Pure functions: Trade list in → stats dict out.
Used by the runner (after backtest) and the viewer (if stats not pre-computed).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
from scipy.stats import norm as _norm_dist
from scipy.stats import ttest_1samp

from shared.models import BacktestResult, BacktestStats, EquityPoint, Trade

logger = logging.getLogger(__name__)

MIN_CALMAR_DRAWDOWN = 0.001
SECONDS_PER_DAY = 24 * 60 * 60
DAILY_RETURN_PERIODS_PER_YEAR = 252

try:
    import quantstats as qs

    HAS_QUANTSTATS = True
except ImportError:
    HAS_QUANTSTATS = False
    logger.warning("quantstats not installed — falling back to manual calculations")


def compute_stats(
    trades: list[Trade],
    initial_capital: float = 100_000.0,
    equity_curve: list[EquityPoint] | None = None,
    risk_free_rate: float = 0.04,
) -> dict[str, Any]:
    """Compute comprehensive performance metrics from a list of trades.

    Args:
        trades: List of Trade objects.
        initial_capital: Starting capital.
        equity_curve: Optional equity curve for drawdown/Sharpe calculation.
        risk_free_rate: Annual risk-free rate for Sharpe/Sortino.

    Returns:
        Dictionary of metric name → value.
    """
    if not trades and not equity_curve:
        return _empty_stats()

    if trades:
        # #84: net-based stats — gross t.pnl ignores fees and flatters every
        # selection metric. Prefer stored net_pnl, else subtract stored fees.
        pnls = [_net_trade_pnl(t) for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]

        total_trades = len(trades)
        win_count = len(winners)
        loss_count = len(losers)

        total_pnl = sum(pnls)
        win_rate = (win_count / total_trades) if total_trades > 0 else 0.0

        avg_winner = sum(winners) / win_count if win_count > 0 else 0.0
        avg_loser = sum(losers) / loss_count if loss_count > 0 else 0.0

        gross_profit = sum(winners)
        gross_loss = abs(sum(losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Holding bars
        holding_bars_list = [t.holding_bars for t in trades if t.holding_bars is not None]
        avg_holding_bars = (
            sum(holding_bars_list) / len(holding_bars_list) if holding_bars_list else None
        )

        # Consecutive wins/losses
        max_consecutive_wins = _max_consecutive(pnls, positive=True)
        max_consecutive_losses = _max_consecutive(pnls, positive=False)
    else:
        # No trades, but equity curve exists
        total_trades = 0
        win_count = 0
        loss_count = 0
        win_rate = 0.0
        profit_factor = 0.0
        total_pnl = equity_curve[-1].equity - equity_curve[0].equity if equity_curve else 0.0
        avg_winner = 0.0
        avg_loser = 0.0
        avg_holding_bars = 0.0
        max_consecutive_wins = 0
        max_consecutive_losses = 0

    # Equity-curve-based metrics
    max_drawdown_pct = 0.0
    max_drawdown_duration = 0
    sharpe_ratio = 0.0
    sortino_ratio = 0.0
    calmar_ratio = 0.0
    cagr = 0.0
    total_return = 0.0
    psr = None  # Probabilistic Sharpe Ratio
    p_value = 1.0

    if equity_curve and len(equity_curve) > 1:
        equities = [e.equity for e in equity_curve]
        daily_returns = _daily_returns_from_equity_curve(equity_curve)
        p_value = _one_sided_p_value(daily_returns)
        cagr = _cagr_from_backtest_period(equity_curve=equity_curve, trades=trades)
        daily_return_series = _daily_returns_series_from_equity_curve(equity_curve)

        # Environment-independent definitions (Cleanup 16-D1, F17): the two
        # code paths below previously published DIFFERENT metric definitions
        # for the same backtest depending on whether quantstats was installed.
        # Now both paths share one definition per metric:
        # - total_return: equity endpoints (E_end / E_start - 1), NOT the
        #   compounded product of resampled daily returns;
        # - calmar_ratio: CAGR (elapsed-time based, identical on both paths)
        #   divided by max drawdown;
        # - sharpe/sortino: daily-resampled returns, annualized with 252
        #   periods (quantstats when available, manual fallback otherwise).
        max_drawdown_pct = -_max_drawdown_pct_manual(equities)
        total_return = (equities[-1] / equities[0]) - 1 if equities[0] > 0 else 0
        calmar_ratio = _safe_calmar_ratio(cagr, max_drawdown_pct)
        max_drawdown_duration = _max_drawdown_duration(equities)

        if HAS_QUANTSTATS:
            returns_series = daily_return_series
            qs_usable = returns_series is not None and not returns_series.empty
            if qs_usable and not isinstance(returns_series.index, pd.DatetimeIndex):
                # F17 third facet: QS needs a datetime index; a resampling
                # fallback that produced a bare RangeIndex makes every
                # _safe_qs call return 0.0 silently. Treat QS as unusable.
                qs_usable = False
            if not qs_usable:
                # QS installed but unusable (no daily series resolvable, or a
                # non-datetime index). Fall through to the manual path on raw
                # period returns instead of silently leaving Sharpe/Sortino at
                # 0.0 — same definition family as the QS branch.
                if len(daily_returns) > 1:
                    sharpe_ratio = _sharpe_manual(daily_returns, risk_free_rate)
                    sortino_ratio = _sortino_manual(daily_returns, risk_free_rate)
                    psr = _probabilistic_sharpe_manual(daily_returns, sharpe_ratio)
            elif len(returns_series) > 1:
                sharpe_ratio = _safe_qs(
                    qs.stats.sharpe,
                    returns_series,
                    rf=risk_free_rate,
                    periods=DAILY_RETURN_PERIODS_PER_YEAR,
                )
                sortino_ratio = _safe_qs(
                    qs.stats.sortino,
                    returns_series,
                    rf=risk_free_rate,
                    periods=DAILY_RETURN_PERIODS_PER_YEAR,
                )
                psr = _probabilistic_sharpe(returns_series, sharpe_ratio)
        else:
            # Manual fallback mirrors the quantstats definitions above
            # (daily returns, 252-period annualization).
            if len(daily_returns) > 1:
                sharpe_ratio = _sharpe_manual(daily_returns, risk_free_rate)
                sortino_ratio = _sortino_manual(daily_returns, risk_free_rate)
                psr = _probabilistic_sharpe_manual(daily_returns, sharpe_ratio)

    return BacktestStats(
        total_trades=total_trades,
        winners=win_count,
        losers=loss_count,
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 4) if profit_factor != float("inf") else None,
        total_pnl=round(total_pnl, 2),
        avg_winner=round(avg_winner, 2),
        avg_loser=round(avg_loser, 2),
        max_consecutive_wins=max_consecutive_wins,
        max_consecutive_losses=max_consecutive_losses,
        avg_holding_bars=round(avg_holding_bars, 1) if avg_holding_bars is not None else None,
        max_drawdown_pct=round(float(max_drawdown_pct), 6),
        max_drawdown_duration=max_drawdown_duration,
        sharpe_ratio=round(float(sharpe_ratio), 4),
        sortino_ratio=round(float(sortino_ratio), 4),
        calmar_ratio=round(float(calmar_ratio), 4),
        cagr=round(float(cagr), 6),
        total_return=round(float(total_return), 6),
        psr=round(float(psr), 4) if psr is not None else None,
        p_value=round(float(p_value), 6),
    ).to_dict()


def compute_stats_for_result(
    result: BacktestResult, risk_free_rate: float = 0.04
) -> dict[str, Any]:
    """Convenience: compute stats from a BacktestResult."""
    return compute_stats(
        trades=result.trades,
        initial_capital=result.meta.initial_capital,
        equity_curve=result.equity_curve or None,
        risk_free_rate=risk_free_rate,
    )


def merge_canonical_stats(result: BacktestResult, risk_free_rate: float = 0.04) -> None:
    """Merge freshly computed canonical stats over engine-provided stats.

    Engines may return arbitrary (stale or partially canonical) stats dicts;
    for every key the canonical values computed here from the persisted
    trades/equity curve **win**, engine-only keys are kept. Enforcing that
    ordering in one named helper keeps call sites from re-introducing the
    engine-wins merge by accident (pre-mortem 16-B #1).
    """
    result.stats = {**result.stats, **compute_stats_for_result(result, risk_free_rate)}


# ─── QuantStats helpers ──────────────────────────────────────────────────────


def _equity_to_returns_series(equity_curve: list[EquityPoint]) -> pd.Series | None:
    """Convert EquityPoint list to a pandas Series of returns with datetime index."""
    if len(equity_curve) < 2:
        return None

    equities = []
    dates = []
    for e in equity_curve:
        equities.append(e.equity)
        if hasattr(e, "time"):
            ts = e.time
        elif hasattr(e, "timestamp"):
            ts = e.timestamp
        else:
            ts = None
        dates.append(ts)

    if dates[0] is not None:
        try:
            dt_index = pd.to_datetime(dates, utc=True)
            eq_series = pd.Series(equities, index=dt_index, dtype=float).sort_index()
            eq_series = eq_series.groupby(level=0).last()
            return eq_series.pct_change().dropna()
        except Exception:
            logger.debug("Could not build datetime-indexed returns series", exc_info=True)

    eq_series = pd.Series(equities, dtype=float)
    return eq_series.pct_change().dropna()


def _daily_returns_series_from_equity_curve(equity_curve: list[EquityPoint]) -> pd.Series | None:
    """Convert any equity curve cadence into daily close-to-close returns.

    Sharpe, Sortino, PSR and p-values are reported on daily observations.  For
    intraday curves this avoids treating every bar as a full annualization
    period and prevents dense bar spacing from changing the metric definition.
    """
    if len(equity_curve) < 2:
        return None

    equities = [point.equity for point in equity_curve]
    try:
        timestamps = pd.to_datetime(
            [point.time for point in equity_curve], utc=True, format="ISO8601"
        )
        series = pd.Series(equities, index=timestamps, dtype=float).sort_index()
        series = series.groupby(level=0).last()
        daily_equity = series.resample("D").last().ffill()
        returns = daily_equity.pct_change().dropna()
        returns = returns.replace([math.inf, -math.inf], pd.NA).dropna()
        return returns if not returns.empty else None
    except Exception:
        logger.debug("Could not build daily returns series from equity curve", exc_info=True)

    raw_returns = _equity_to_returns_series(equity_curve)
    if raw_returns is None or raw_returns.empty:
        return None
    return raw_returns


def _attach_inferred_freq(returns: pd.Series) -> pd.Series:
    """Attach an inferred frequency when the datetime index is regular."""
    if not isinstance(returns.index, pd.DatetimeIndex) or len(returns.index) < 3:
        return returns

    try:
        freq = pd.infer_freq(returns.index)
    except ValueError:
        return returns

    if not freq:
        return returns

    try:
        copied = returns.copy()
        copied.index = pd.DatetimeIndex(copied.index, freq=freq)
        return copied
    except ValueError:
        return returns


def _net_trade_pnl(trade: Trade) -> float:
    """Net realized PnL for one trade (#84: never silently gross)."""
    if trade.net_pnl is not None:
        return float(trade.net_pnl)
    fees = float(trade.fees or 0.0)
    return float(trade.pnl) - fees


def _cagr_from_backtest_period(*, equity_curve: list[EquityPoint], trades: list[Trade]) -> float:
    if len(equity_curve) < 2:
        return 0.0

    equity_initial = float(equity_curve[0].equity)
    equity_final = float(equity_curve[-1].equity)
    if equity_initial <= 0 or equity_final <= 0:
        return 0.0

    start_dt, end_dt = _resolve_backtest_period_bounds(equity_curve=equity_curve, trades=trades)
    days = float((end_dt - start_dt).total_seconds()) / SECONDS_PER_DAY
    if days <= 0:
        return 0.0
    return (equity_final / equity_initial) ** (365.25 / days) - 1


def _resolve_backtest_period_bounds(
    *, equity_curve: list[EquityPoint], trades: list[Trade]
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Calendar-window bounds for CAGR annualization (#84).

    The old trade-span window (first entry -> last exit) exploded CAGR/Calmar
    for clustered trades. Annualize over the calendar backtest window, i.e.
    the equity-curve endpoints; trades are only a fallback when the curve
    carries no usable timestamps.
    """
    try:
        equity_times = [
            pd.Timestamp(ts)
            for ts in pd.to_datetime([point.time for point in equity_curve], utc=True, errors="coerce")
            if not pd.isna(ts)
        ]
    except Exception:
        equity_times = []

    if len(equity_times) >= 2:
        return min(equity_times), max(equity_times)

    trade_start_times: list[pd.Timestamp] = []
    trade_end_times: list[pd.Timestamp] = []

    for trade in trades:
        entry_ts = pd.to_datetime(str(trade.entry_time), utc=True, errors="coerce")
        if not pd.isna(entry_ts):
            trade_start_times.append(pd.Timestamp(entry_ts))

        exit_ts = pd.to_datetime(str(trade.last_exit_time), utc=True, errors="coerce")
        if not pd.isna(exit_ts):
            trade_end_times.append(pd.Timestamp(exit_ts))

    if trade_start_times and trade_end_times:
        return min(trade_start_times), max(trade_end_times)

    fallback_start = pd.Timestamp.now(tz="UTC")
    return fallback_start, fallback_start


def _safe_calmar_ratio(annualized_return: float, max_drawdown_pct: float) -> float:
    """Avoid exploding Calmar ratios when drawdown is effectively zero."""
    drawdown = abs(float(max_drawdown_pct))
    if drawdown < MIN_CALMAR_DRAWDOWN:
        return 0.0
    return float(annualized_return) / drawdown


def _safe_qs(func, returns: pd.Series, **kwargs) -> float:
    """Safely call a quantstats function, returning 0.0 on failure."""
    try:
        result = func(returns, **kwargs)
        if result is None or (
            isinstance(result, float) and (math.isnan(result) or math.isinf(result))
        ):
            return 0.0
        return float(result)
    except Exception as e:
        logger.debug("QuantStats %s failed: %s", func.__name__, e)
        return 0.0


# ─── Manual fallback helpers ─────────────────────────────────────────────────


def _empty_stats() -> dict[str, Any]:
    return BacktestStats().to_dict()


def _max_consecutive(pnls: list[float], positive: bool = False) -> int:
    """Compute max consecutive wins (positive=True) or losses (positive=False)."""
    max_streak = 0
    current = 0
    for p in pnls:
        if (positive and p > 0) or (not positive and p < 0):
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _max_drawdown_pct_manual(equities: list[float]) -> float:
    """Compute max drawdown as a positive fraction (0.05 = 5%)."""
    if len(equities) < 2:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _max_drawdown_duration(equities: list[float]) -> int:
    """Compute max drawdown duration in periods."""
    if len(equities) < 2:
        return 0
    peak = equities[0]
    max_dur = 0
    current_dur = 0
    for eq in equities:
        if eq >= peak:
            peak = eq
            current_dur = 0
        else:
            current_dur += 1
            max_dur = max(max_dur, current_dur)
    return max_dur


def _daily_returns(equities: list[float]) -> list[float]:
    """Compute period-over-period returns."""
    returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] > 0:
            returns.append(equities[i] / equities[i - 1] - 1)
        else:
            returns.append(0.0)
    return returns


def _daily_returns_from_equity_curve(equity_curve: list[EquityPoint]) -> list[float]:
    """Compute daily returns from an equity curve, falling back to raw period returns."""
    daily_series = _daily_returns_series_from_equity_curve(equity_curve)
    if daily_series is not None and not daily_series.empty:
        return [float(value) for value in daily_series.tolist() if pd.notna(value)]

    if len(equity_curve) < 2:
        return []

    equities = [point.equity for point in equity_curve]
    return _daily_returns(equities)


def _one_sided_p_value(returns: list[float]) -> float:
    """Compute the one-sided p-value for mean returns > 0; return 1.0 on failure."""
    try:
        clean_returns = [float(value) for value in returns if pd.notna(value)]
        if not clean_returns:
            return 1.0
        _, p_value = ttest_1samp(clean_returns, popmean=0.0, alternative="greater")
        p_value_float = float(p_value)
        if math.isnan(p_value_float) or math.isinf(p_value_float):
            return 1.0
        return max(0.0, min(1.0, p_value_float))
    except Exception:
        return 1.0


def _sharpe_manual(returns: list[float], risk_free_rate: float) -> float:
    """Annualized Sharpe ratio (manual)."""
    if not returns:
        return 0.0
    daily_rf = risk_free_rate / 252
    excess = [r - daily_rf for r in returns]
    mean_excess = sum(excess) / len(excess)
    variance = sum((r - mean_excess) ** 2 for r in excess) / len(excess)
    std = math.sqrt(variance) if variance > 0 else 0
    if std == 0:
        return 0.0
    return (mean_excess / std) * math.sqrt(252)


def _sortino_manual(returns: list[float], risk_free_rate: float) -> float:
    """Annualized Sortino ratio (manual)."""
    if not returns:
        return 0.0
    daily_rf = risk_free_rate / 252
    mean_return = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if len(downside) < 2:
        return 0.0
    downside_var = sum(r**2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0
    if downside_std == 0:
        return 0.0
    excess_mean = mean_return - daily_rf
    return (excess_mean / downside_std) * math.sqrt(252)


def _probabilistic_sharpe(
    returns_series: pd.Series, _sharpe_unused: float, sr_benchmark: float = 0.0
) -> float | None:
    """Probabilistic Sharpe Ratio using returns series (QuantStats path).
    PSR = Φ((SR - SR*) × √(n-1) / √(1 - skew×SR + ((kurt-1)/4)×SR²))
    Uses per-observation (non-annualized) SR for correct PSR calculation.
    """
    try:
        n = len(returns_series)
        if n < 10:
            return None
        mean_r = float(returns_series.mean())
        std_r = float(returns_series.std())
        if std_r == 0:
            return None
        sr_obs = mean_r / std_r  # per-observation Sharpe (NOT annualized)
        skew = float(returns_series.skew())
        kurt = float(returns_series.kurtosis()) + 3  # pandas excess → raw kurtosis
        return _psr_formula(sr_obs, sr_benchmark, n, skew, kurt)
    except Exception:
        return None


def _probabilistic_sharpe_manual(
    returns: list[float], _sharpe_unused: float, sr_benchmark: float = 0.0
) -> float | None:
    """PSR from raw returns list (manual fallback). Uses per-observation SR."""
    n = len(returns)
    if n < 10:
        return None
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(m2) if m2 > 0 else 0
    if std == 0:
        return None
    sr_obs = mean / std
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    skew = m3 / (std**3)
    kurt = m4 / (std**4)  # raw kurtosis
    return _psr_formula(sr_obs, sr_benchmark, n, skew, kurt)


def _psr_formula(sr: float, sr_bench: float, n: int, skew: float, kurt: float) -> float | None:
    """Core PSR formula: Φ((SR - SR*) × √(n-1) / √(1 - skew×SR + ((kurt-1)/4)×SR²))"""
    denom_inner = 1 - skew * sr + ((kurt - 1) / 4) * sr**2
    if denom_inner <= 0:
        return None
    z = (sr - sr_bench) * math.sqrt(n - 1) / math.sqrt(denom_inner)
    return float(_norm_dist.cdf(z))
