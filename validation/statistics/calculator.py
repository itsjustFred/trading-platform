"""Trade statistics calculator for the Strategy Validation Framework.

Computes performance metrics (win rate, profit factor, drawdown, Sharpe,
Sortino, etc.) from a list of ``Trade`` objects.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from shared.models import Trade


def _empty_stats() -> dict[str, Any]:
    """Return a stats dict with all-zero values for an empty trade list."""
    return {
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "net_profit": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "sortino_ratio": 0.0,
        "recovery_factor": 0.0,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "avg_winner": 0.0,
        "avg_loser": 0.0,
        "avg_trade": 0.0,
        "expectancy": 0.0,
    }


def _max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Compute max drawdown (absolute and percentage) from an equity curve.

    The equity curve must start with an initial value (e.g. 0 or initial
    capital) followed by post-trade equities.

    Returns:
        (max_drawdown_abs, max_drawdown_pct)
    """
    if len(equity_curve) < 2:
        return 0.0, 0.0

    peak = equity_curve[0]
    max_dd_abs = 0.0
    max_dd_pct = 0.0

    for eq in equity_curve[1:]:
        if eq > peak:
            peak = eq
            continue

        dd = peak - eq
        if dd > max_dd_abs:
            max_dd_abs = dd
            if peak > 0:
                max_dd_pct = (dd / peak) * 100.0
            else:
                max_dd_pct = 0.0

    return max_dd_abs, max_dd_pct


def _compute_returns(trades: list[Trade]) -> list[float]:
    """Build a return series from trades.

    When timestamps are available trades are grouped by exit date and the
    daily PnL sum is used as the daily return observation.  Without
    timestamps each trade PnL is used as a per-trade return.
    """
    if not trades:
        return []

    has_timestamps = any(
        (t.last_exit_time or t.entry_time) for t in trades
    )

    if not has_timestamps:
        return [float(t.pnl) for t in trades]

    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        ts = t.last_exit_time or t.entry_time
        if not ts:
            daily[str(id(t))] += float(t.pnl)
            continue
        day = ts[:10]  # YYYY-MM-DD
        daily[day] += float(t.pnl)

    return list(daily.values())


def _sharpe(returns: list[float]) -> float:
    """Annualised Sharpe ratio (mean / sample-std * sqrt(252))."""
    n = len(returns)
    if n < 2:
        return 0.0

    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(variance)

    if std_r == 0 or not math.isfinite(std_r):
        return 0.0

    ratio = (mean_r / std_r) * math.sqrt(252)
    return ratio if math.isfinite(ratio) else 0.0


def _sortino(returns: list[float]) -> float:
    """Annualised Sortino ratio (mean / downside-deviation * sqrt(252))."""
    n = len(returns)
    if n < 2:
        return 0.0

    mean_r = sum(returns) / n
    downside_sum = sum(r * r for r in returns if r < 0)
    if downside_sum <= 0:
        return 0.0

    downside_std = math.sqrt(downside_sum / n)
    if downside_std == 0 or not math.isfinite(downside_std):
        return 0.0

    ratio = (mean_r / downside_std) * math.sqrt(252)
    return ratio if math.isfinite(ratio) else 0.0


class TradeStatisticsCalculator:
    """Compute trading statistics from a list of ``Trade`` objects."""

    @staticmethod
    def from_trades(trades: list[Trade]) -> dict[str, Any]:
        """Calculate all supported metrics.

        Args:
            trades: List of closed trades (at least one exit per trade).

        Returns:
            Dictionary of metric name -> value.
        """
        if not trades:
            return _empty_stats()

        pnls = [float(t.pnl) for t in trades]
        total_trades = len(pnls)
        winning_pnls = [p for p in pnls if p > 0]
        losing_pnls = [p for p in pnls if p < 0]
        winning_count = len(winning_pnls)
        losing_count = len(losing_pnls)

        net_profit = sum(pnls)
        gross_profit = sum(winning_pnls)
        gross_loss = sum(losing_pnls)  # negative number

        win_rate = winning_count / total_trades if total_trades else 0.0

        avg_winner = sum(winning_pnls) / winning_count if winning_count else 0.0
        avg_loser = sum(losing_pnls) / losing_count if losing_count else 0.0
        avg_trade = net_profit / total_trades if total_trades else 0.0

        abs_gross_loss = abs(gross_loss)
        if abs_gross_loss > 0:
            profit_factor = gross_profit / abs_gross_loss
        else:
            profit_factor = float("inf") if gross_profit > 0 else 0.0

        expectancy = (win_rate * avg_winner) + ((1.0 - win_rate) * avg_loser)

        # --- Drawdown (cumulative PnL curve) ---
        sorted_trades = sorted(
            trades,
            key=lambda t: (t.last_exit_time or t.entry_time or ""),
        )
        cumulative = 0.0
        equity_curve = [0.0]
        for t in sorted_trades:
            cumulative += float(t.pnl)
            equity_curve.append(cumulative)

        max_dd_abs, max_dd_pct = _max_drawdown(equity_curve)

        if max_dd_abs > 0:
            recovery_factor = net_profit / max_dd_abs
        else:
            recovery_factor = float("inf") if net_profit != 0 else 0.0

        # --- Sharpe / Sortino ---
        returns = _compute_returns(sorted_trades)
        sharpe = _sharpe(returns)
        sortino = _sortino(returns)

        return {
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "net_profit": net_profit,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "max_drawdown": max_dd_abs,
            "max_drawdown_pct": max_dd_pct,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "recovery_factor": recovery_factor,
            "total_trades": total_trades,
            "winning_trades": winning_count,
            "losing_trades": losing_count,
            "avg_winner": avg_winner,
            "avg_loser": avg_loser,
            "avg_trade": avg_trade,
            "expectancy": expectancy,
        }
