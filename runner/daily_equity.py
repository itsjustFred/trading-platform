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


def _last_exit_time(trade: Trade) -> str | None:
    """The trade's last non-empty exit time, or None when it has none."""
    exit_time = None
    for exit_ in trade.exits:
        if exit_.time:
            exit_time = exit_.time
    return exit_time


def _parsed_in_window(parsed: datetime | None, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    """Window test on an already-parsed timestamp (None never passes)."""
    if parsed is None:
        return False
    if start_bound and parsed < start_bound:
        return False
    if end_bound and parsed > end_bound:
        return False
    return True


def _trade_in_window(trade: Trade, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    exit_time = _last_exit_time(trade)
    if not exit_time:
        return False
    parsed = _parse_ts(exit_time)
    # A timestamp we cannot understand must not silently pass the filter.
    return _parsed_in_window(parsed, start_bound, end_bound)


def _in_window(value: str, start_bound: datetime | None, end_bound: datetime | None) -> bool:
    return _parsed_in_window(_parse_ts(value), start_bound, end_bound)


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


def _windowed_curve(
    result: BacktestResult, start_bound: datetime | None, end_bound: datetime | None
) -> list[EquityPoint] | None:
    """The engine-provided curve scoped to the same window as the aggregation.

    Delegates to ``_scoped_view``: with a window it is the windowed curve,
    without one the engine curve passes through unchanged (pre-mortem 16-B #7).
    """
    return _scoped_view(result, start_bound, end_bound).equity_curve


def _preserves_engine_curve(
    preserved_curve: list[EquityPoint] | None, daily_points: list[EquityPoint]
) -> bool:
    """Keep the engine curve only when it has more points than the daily build."""
    return bool(preserved_curve) and len(preserved_curve) > len(daily_points)


def _attach_daily_stats(result: BacktestResult, daily_returns: list[dict[str, Any]]) -> None:
    """Record the daily aggregation on stats and meta."""
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    result.meta.data["equity_frequency"] = "daily"
    result.meta.data["daily_return_count"] = len(daily_returns)


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
        exit_time = _last_exit_time(trade)
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

    # When a window is active, an engine-provided curve must be scoped to the
    # same bounds before the length comparison: preserving the full unwindowed
    # curve next to window-scoped daily_returns would make the curve and the
    # reported daily returns describe different periods (pre-mortem 16-B #7).
    # _scoped_view computes exactly this view; reuse it instead of re-filtering.
    preserved_curve = _windowed_curve(result, start_bound, end_bound)
    if _preserves_engine_curve(preserved_curve, daily_points):
        _attach_daily_stats(result, daily_returns)
        result.equity_curve = preserved_curve
        return result

    # Attach the daily equity curve
    result.equity_curve = daily_points
    _attach_daily_stats(result, daily_returns)

    return result
