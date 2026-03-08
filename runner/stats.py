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

from shared.models import BacktestResult, EquityPoint, Trade

logger = logging.getLogger(__name__)

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
        pnls = [t.pnl for t in trades]
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

    if equity_curve and len(equity_curve) > 1:
        equities = [e.equity for e in equity_curve]

        if HAS_QUANTSTATS:
            returns_series = _equity_to_returns_series(equity_curve)
            if returns_series is not None and len(returns_series) > 1:
                sharpe_ratio = _safe_qs(qs.stats.sharpe, returns_series, rf=risk_free_rate)
                sortino_ratio = _safe_qs(qs.stats.sortino, returns_series, rf=risk_free_rate)
                calmar_ratio = _safe_qs(qs.stats.calmar, returns_series)

                # Use manual calculation for MaxDD as it is more robust (qs can fail on intraday or non-datetime index)
                # MaxDD is purely geometric and doesn't require annualization logic like Sharpe
                equities_series = [e.equity for e in equity_curve]
                max_drawdown_pct = -_max_drawdown_pct_manual(equities_series)

                cagr = _safe_qs(qs.stats.cagr, returns_series, rf=risk_free_rate)
                total_return = float((1 + returns_series).prod() - 1)

                # Max drawdown duration (in periods)
                dd_dur = _max_drawdown_duration(equities)
                max_drawdown_duration = dd_dur

                # Probabilistic Sharpe Ratio
                psr = _probabilistic_sharpe(returns_series, sharpe_ratio)
        else:
            # Fallback to manual calculations
            max_drawdown_pct = -_max_drawdown_pct_manual(equities)
            daily_returns = _daily_returns(equities)
            if len(daily_returns) > 1:
                sharpe_ratio = _sharpe_manual(daily_returns, risk_free_rate)
                sortino_ratio = _sortino_manual(daily_returns, risk_free_rate)
                total_return = (equities[-1] / equities[0]) - 1 if equities[0] > 0 else 0
                n_points = len(equities)
                annualized_return = (1 + total_return) ** (252 / max(n_points, 1)) - 1
                cagr = annualized_return
                calmar_ratio = (
                    annualized_return / abs(max_drawdown_pct) if max_drawdown_pct != 0 else 0
                )
                max_drawdown_duration = _max_drawdown_duration(equities)
                psr = _probabilistic_sharpe_manual(daily_returns, sharpe_ratio)

    stats: dict[str, Any] = {
        "total_trades": total_trades,
        "winners": win_count,
        "losers": loss_count,
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "total_pnl": round(total_pnl, 2),
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
    }

    if avg_holding_bars is not None:
        stats["avg_holding_bars"] = round(avg_holding_bars, 1)

    if equity_curve and len(equity_curve) > 1:
        stats["max_drawdown_pct"] = round(float(max_drawdown_pct), 6)
        stats["max_drawdown_duration"] = max_drawdown_duration
        stats["sharpe_ratio"] = round(float(sharpe_ratio), 4)
        stats["sortino_ratio"] = round(float(sortino_ratio), 4)
        stats["calmar_ratio"] = round(float(calmar_ratio), 4)
        stats["cagr"] = round(float(cagr), 6)
        stats["total_return"] = round(float(total_return), 6)
        if psr is not None:
            stats["psr"] = round(float(psr), 4)

    return stats


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


# ─── QuantStats helpers ──────────────────────────────────────────────────────


def _equity_to_returns_series(equity_curve: list[EquityPoint]) -> pd.Series | None:
    """Convert EquityPoint list to a pandas Series of returns with datetime index."""
    if len(equity_curve) < 2:
        return None

    equities = []
    dates = []
    for e in equity_curve:
        equities.append(e.equity)
        ts = e.time if hasattr(e, "time") else e.timestamp if hasattr(e, "timestamp") else None
        dates.append(ts)

    eq_series = pd.Series(equities, dtype=float)
    returns = eq_series.pct_change().dropna()

    # Try to set datetime index
    if dates[0] is not None:
        try:
            dt_index = pd.to_datetime(dates[1:])  # skip first (NaN from pct_change)
            returns.index = dt_index
        except Exception:
            pass

    return returns


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
    return {
        "total_trades": 0,
        "winners": 0,
        "losers": 0,
        "win_rate": 0.0,
        "profit_factor": None,
        "total_pnl": 0.0,
        "avg_winner": 0.0,
        "avg_loser": 0.0,
        "max_consecutive_wins": 0,
        "max_consecutive_losses": 0,
    }


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
