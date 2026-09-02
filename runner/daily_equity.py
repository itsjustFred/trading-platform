"""Daily equity curve construction from backtest trades."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from shared.models import BacktestResult, EquityPoint, Trade


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp; None when it cannot be understood."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_day_bound(value: str | None, *, is_end: bool) -> datetime | None:
    """Normalize a YYYY-MM-DD (or ISO) boundary for trade-time comparison."""
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    raw = str(value or "").strip()
    if is_end and "T" not in raw and ":" not in raw:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _trade_in_window(trade: Trade, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    exit_time = None
    for exit_ in trade.exits:
        if exit_.time:
            exit_time = exit_.time
    if not exit_time:
        return False
    parsed = _parse_ts(exit_time)
    if parsed is None:
        # A timestamp we cannot understand must not silently pass the filter.
        return False
    if start_bound and parsed < start_bound:
        return False
    if end_bound and parsed > end_bound:
        return False
    return True


def _in_window(value: str, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    parsed = _parse_ts(value)
    if parsed is None:
        return False
    if start_bound and parsed < start_bound:
        return False
    if end_bound and parsed > end_bound:
        return False
    return True


def _scoped_view(
    result: BacktestResult, start_bound: datetime | None, end_bound: datetime | None
) -> BacktestResult:
    """Return a shallow copy of the result limited to the requested window."""
    if not start_bound and not end_bound:
        return result

    trades = [trade for trade in result.trades if _trade_in_window(trade, start_bound, end_bound)]
    equity_curve = result.equity_curve
    if equity_curve:
        equity_curve = [
            point
            for point in equity_curve
            if _in_window(point.time, start_bound, end_bound)
        ]
    return BacktestResult(
        meta=result.meta,
        trades=trades,
        equity_curve=equity_curve,
        stats=dict(result.stats or {}),
        indicators=list(result.indicators or []),
        drawings=list(result.drawings or []),
    )


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
        start: Optional start date filter (YYYY-MM-DD); trades and equity
            points before this date are excluded from the daily aggregation.
        end: Optional end date filter (YYYY-MM-DD); trades and equity points
            after this date are excluded from the daily aggregation.

    Returns:
        The same result object with daily equity attached
    """
    start_bound = _parse_day_bound(start, is_end=False)
    end_bound = _parse_day_bound(end, is_end=True)

    # If there are no trades and no existing equity curve, mark as unknown frequency
    if not result.trades and not result.equity_curve:
        result.meta.data = result.meta.data or {}
        result.meta.data["equity_frequency"] = "unknown"
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        return result

    # Build daily equity from trades, restricted to the requested window
    scoped_result = _scoped_view(result, start_bound, end_bound)
    daily_points, daily_returns = build_daily_equity_curve(scoped_result)

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