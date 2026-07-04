"""Daily equity curve construction from backtest trades."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from shared.models import BacktestResult, EquityPoint


def build_daily_equity_curve(result: BacktestResult) -> tuple[list[EquityPoint], list[dict[str, Any]]]:
    """
    Build a daily equity curve from trade PnL.

    Returns (equity_points, daily_returns) where equity_points is a list of
    EquityPoint objects and daily_returns is a list of dicts with
    'date', 'return', 'equity'.
    """
    initial_capital = result.meta.initial_capital
    equity_by_day: dict[str, float] = defaultdict(float)

    # Aggregate PnL per day from trades
    for trade in result.trades:
        pnl = trade.pnl or 0.0
        # Skip non-finite PnL values (nan, inf, -inf)
        if not math.isfinite(pnl):
            continue

        # Determine the day from the last exit
        exit_time = None
        for exit_ in trade.exits:
            if exit_.time:
                exit_time = exit_.time
        if exit_time:
            try:
                dt = datetime.fromisoformat(exit_time.replace("Z", "+00:00"))
                day_key = dt.date().isoformat()
                equity_by_day[day_key] += pnl
            except (ValueError, AttributeError):
                # Skip trades with invalid exit times
                continue

    # Build daily equity points
    daily_points = []
    daily_returns = []
    running_equity = initial_capital

    # Sort days chronologically
    for day in sorted(equity_by_day.keys()):
        running_equity += equity_by_day[day]
        # EquityPoint expects 'time' field (ISO datetime), use start of day UTC
        time_str = f"{day}T00:00:00+00:00"
        daily_points.append(EquityPoint(time=time_str, equity=running_equity))
        # Calculate daily return
        prev_equity = running_equity - equity_by_day[day]
        if prev_equity > 0:
            daily_return = (running_equity - prev_equity) / prev_equity
        else:
            daily_return = 0.0
        daily_returns.append({"date": day, "return": daily_return, "equity": running_equity})

    return daily_points, daily_returns


def attach_daily_equity(
    result: BacktestResult,
    start: str | None = None,
    end: str | None = None,
) -> BacktestResult:
    """
    Attach daily equity curve to a backtest result.

    Args:
        result: BacktestResult to update
        start: Optional start date filter (YYYY-MM-DD)
        end: Optional end date filter (YYYY-MM-DD)

    Returns:
        The same result object with daily equity attached
    """
    # If there are no trades and no existing equity curve, mark as unknown frequency
    if not result.trades and not result.equity_curve:
        result.meta.data = result.meta.data or {}
        result.meta.data["equity_frequency"] = "unknown"
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        return result

    # Build daily equity from trades
    daily_points, daily_returns = build_daily_equity_curve(result)

    # If no daily points could be built (e.g., no valid exit times), fallback to existing curve
    if not daily_points:
        result.meta.data = result.meta.data or {}
        # Only mark as daily if we actually built a daily curve
        if result.equity_curve and len(result.equity_curve) > 1:
            result.meta.data["equity_frequency"] = "daily"
        else:
            result.meta.data["equity_frequency"] = "unknown"
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        return result

    # Preserve existing equity_curve if it has more points than what we built
    # (e.g., the engine already provided a full equity curve with multiple points)
    if result.equity_curve and len(result.equity_curve) > len(daily_points):
        # Keep the original curve, but update metadata
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = len(daily_returns)
        result.stats["daily_returns"] = daily_returns
        result.meta.data = result.meta.data or {}
        result.meta.data["equity_frequency"] = "daily"
        result.meta.data["daily_return_count"] = len(daily_returns)
        return result

    # Attach the daily equity curve
    result.equity_curve = daily_points
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    result.meta.data["equity_frequency"] = "daily"
    result.meta.data["daily_return_count"] = len(daily_returns)

    return result