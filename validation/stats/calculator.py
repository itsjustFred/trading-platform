"""Trade statistics calculator — pure functional metrics from a list of Trades.

This is the single canonical statistics implementation for the validation
framework (the parallel ``validation/statistics/`` module was removed).

Mathematical conventions (kept explicit so results are reproducible):

* **Standard deviation:** *population* std (divide by ``n``), risk-free rate 0.
* **Sharpe:** ``mean(returns) / population_std(returns) * sqrt(252)`` where the
  "returns" series is the per-trade realized PnL sequence.
* **Sortino:** ``mean(returns) / downside_deviation * sqrt(252)`` where the
  downside deviation divides the sum of squared *negative* returns by the count
  of *negative* returns only.
* **Annualization:** ``sqrt(252)`` (daily-trade assumption).
* **Drawdown:** absolute is in account currency; ``max_drawdown_pct`` is a
  fraction (0..1) of the running peak.
"""

from __future__ import annotations

import math
from typing import List

from pydantic import BaseModel

from shared.models import Trade, TradeExit


class TradeStatistics(BaseModel):
    """Canonical trade-based performance statistics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    recovery_factor: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    avg_trade: float = 0.0
    largest_winner: float = 0.0
    largest_loser: float = 0.0


def _realized_pnl(trade: Trade) -> float:
    """Sum PnL over all exits; no exits → 0."""
    if not trade.exits:
        return 0.0
    return sum((exit_.pnl or 0.0) for exit_ in trade.exits)


def _compute_drawdowns(realized_pnls: List[float]) -> tuple[float, float]:
    """Return (max_drawdown_absolute, max_drawdown_pct) from trade PnLs."""
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0

    for pnl in realized_pnls:
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = dd / peak
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

    return max_dd, max_dd_pct


def _sharpe(returns: List[float], annualize_factor: float = math.sqrt(252)) -> float:
    """Annualized Sharpe ratio (population std, risk-free rate = 0)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_return = sum(returns) / n
    variance = sum((r - mean_return) ** 2 for r in returns) / n
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0.0:
        return 0.0
    return (mean_return / std) * annualize_factor


def _sortino(returns: List[float], annualize_factor: float = math.sqrt(252)) -> float:
    """Annualized Sortino ratio (downside deviation, risk-free rate = 0)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean_return = sum(returns) / n
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_var = sum(r**2 for r in downside) / len(downside)
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
    if downside_std == 0.0:
        return 0.0
    return (mean_return / downside_std) * annualize_factor


def compute_trade_statistics(trades: List[Trade]) -> TradeStatistics:
    """Compute all trade-based statistics from a list of Trade objects."""
    if not trades:
        return TradeStatistics()

    realized_pnls = [_realized_pnl(t) for t in trades]

    total_trades = len(trades)
    winning_trades = sum(1 for p in realized_pnls if p > 0)
    losing_trades = sum(1 for p in realized_pnls if p < 0)
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

    net_profit = sum(realized_pnls)
    gross_profit = sum(p for p in realized_pnls if p > 0)
    gross_loss = sum(p for p in realized_pnls if p < 0)

    if gross_loss != 0.0:
        profit_factor = abs(gross_profit / gross_loss)
    else:
        profit_factor = float("inf")

    max_drawdown, max_drawdown_pct = _compute_drawdowns(realized_pnls)

    sharpe_ratio = _sharpe(realized_pnls)
    sortino_ratio = _sortino(realized_pnls)

    if max_drawdown != 0.0:
        recovery_factor = net_profit / max_drawdown
    else:
        recovery_factor = float("inf")

    avg_winner = gross_profit / winning_trades if winning_trades > 0 else 0.0
    avg_loser = gross_loss / losing_trades if losing_trades > 0 else 0.0
    avg_trade = net_profit / total_trades if total_trades > 0 else 0.0

    largest_winner = max((p for p in realized_pnls if p > 0), default=0.0)
    largest_loser = min((p for p in realized_pnls if p < 0), default=0.0)

    return TradeStatistics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        net_profit=net_profit,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        recovery_factor=recovery_factor,
        avg_winner=avg_winner,
        avg_loser=avg_loser,
        avg_trade=avg_trade,
        largest_winner=largest_winner,
        largest_loser=largest_loser,
    )
