"""
Statistics module — computes performance metrics from a list of Trades.

Pure functions: Trade list in → stats dict out.
Used by the runner (after backtest) and the viewer (if stats not pre-computed).
"""

from __future__ import annotations

import logging
import math
from typing import Any

from shared.models import BacktestResult, EquityPoint, Trade

logger = logging.getLogger(__name__)


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
    if not trades:
        return _empty_stats()

    pnls = [t.pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]

    total_trades = len(trades)
    win_count = len(winners)
    loss_count = len(losers)

    total_pnl = sum(pnls)
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

    avg_winner = sum(winners) / win_count if win_count > 0 else 0.0
    avg_loser = sum(losers) / loss_count if loss_count > 0 else 0.0

    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Holding bars
    holding_bars_list = [t.holding_bars for t in trades if t.holding_bars is not None]
    avg_holding_bars = sum(holding_bars_list) / len(holding_bars_list) if holding_bars_list else None

    # Consecutive losses
    max_consecutive_losses = _max_consecutive_losses(pnls)

    # Equity-curve-based metrics
    max_drawdown_pct = 0.0
    sharpe_ratio = 0.0
    sortino_ratio = 0.0
    calmar_ratio = 0.0

    if equity_curve and len(equity_curve) > 1:
        equities = [e.equity for e in equity_curve]
        max_drawdown_pct = _max_drawdown_pct(equities)
        daily_returns = _daily_returns(equities)

        if len(daily_returns) > 1:
            sharpe_ratio = _sharpe(daily_returns, risk_free_rate)
            sortino_ratio = _sortino(daily_returns, risk_free_rate)

            total_return = (equities[-1] / equities[0]) - 1 if equities[0] > 0 else 0
            n_points = len(equities)
            # Rough annualization: assume ~252 trading days
            annualized_return = (1 + total_return) ** (252 / max(n_points, 1)) - 1
            calmar_ratio = annualized_return / abs(max_drawdown_pct / 100) if max_drawdown_pct != 0 else 0

    stats: dict[str, Any] = {
        "total_trades": total_trades,
        "winners": win_count,
        "losers": loss_count,
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "total_pnl": round(total_pnl, 2),
        "avg_winner": round(avg_winner, 2),
        "avg_loser": round(avg_loser, 2),
        "max_consecutive_losses": max_consecutive_losses,
    }

    if avg_holding_bars is not None:
        stats["avg_holding_bars"] = round(avg_holding_bars, 1)

    if equity_curve and len(equity_curve) > 1:
        stats["max_drawdown_pct"] = round(max_drawdown_pct, 2)
        stats["sharpe_ratio"] = round(sharpe_ratio, 4)
        stats["sortino_ratio"] = round(sortino_ratio, 4)
        stats["calmar_ratio"] = round(calmar_ratio, 4)

    return stats


def compute_stats_for_result(result: BacktestResult, risk_free_rate: float = 0.04) -> dict[str, Any]:
    """Convenience: compute stats from a BacktestResult."""
    return compute_stats(
        trades=result.trades,
        initial_capital=result.meta.initial_capital,
        equity_curve=result.equity_curve or None,
        risk_free_rate=risk_free_rate,
    )


# ─── Internal helpers ────────────────────────────────────────────────────────

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
        "max_consecutive_losses": 0,
    }


def _max_consecutive_losses(pnls: list[float]) -> int:
    max_streak = 0
    current = 0
    for p in pnls:
        if p < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _max_drawdown_pct(equities: list[float]) -> float:
    """Compute max drawdown as a percentage."""
    if len(equities) < 2:
        return 0.0
    peak = equities[0]
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _daily_returns(equities: list[float]) -> list[float]:
    """Compute period-over-period returns."""
    returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] > 0:
            returns.append(equities[i] / equities[i - 1] - 1)
        else:
            returns.append(0.0)
    return returns


def _sharpe(returns: list[float], risk_free_rate: float) -> float:
    """Annualized Sharpe ratio."""
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


def _sortino(returns: list[float], risk_free_rate: float) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if not returns:
        return 0.0
    daily_rf = risk_free_rate / 252
    mean_return = sum(returns) / len(returns)
    downside = [r for r in returns if r < 0]
    if len(downside) < 2:
        return 0.0
    downside_var = sum(r ** 2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0
    if downside_std == 0:
        return 0.0
    excess_mean = mean_return - daily_rf
    return (excess_mean / downside_std) * math.sqrt(252)
