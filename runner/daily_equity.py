"""Daily equity curve construction from backtest trades."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from shared.models import BacktestResult, EquityPoint, Trade

MAX_PRESERVED_CURVE_GAP = pd.Timedelta(days=4)
MAX_PRESERVED_BOUNDARY_GAP = pd.Timedelta(days=4)
MIN_PRESERVED_DAY_COVERAGE = 0.5


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


def _parsed_in_window(
    parsed: datetime | None,
    start_bound: datetime | None,
    end_bound: datetime | None,
) -> bool:
    """Window test on an already-parsed timestamp (None never passes)."""
    if parsed is None:
        return False
    if start_bound and parsed < start_bound:
        return False
    if end_bound and parsed > end_bound:
        return False
    return True


def _trade_in_window(
    trade: Trade,
    start_bound: datetime | None,
    end_bound: datetime | None,
) -> bool:
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
    if len(preserved_curve or ()) < 2 or not daily_points:
        return False
    try:
        preserved_index = pd.DatetimeIndex(
            pd.to_datetime(
                [point.time for point in preserved_curve],
                utc=True,
                errors="coerce",
                format="mixed",
            )
        )
        daily_index = pd.DatetimeIndex(
            pd.to_datetime(
                [point.time for point in daily_points],
                utc=True,
                errors="coerce",
                format="mixed",
            )
        )
        preserved_values = pd.Series(
            pd.to_numeric(
                [point.equity for point in preserved_curve], errors="coerce"
            ),
            dtype=float,
        )
        daily_final = float(daily_points[-1].equity)
        preserved_final = float(preserved_values.iloc[-1])
    except (TypeError, ValueError, OverflowError):
        return False
    if (
        preserved_index.hasnans
        or daily_index.hasnans
        or preserved_values.isna().any()
        or not all(math.isfinite(float(value)) for value in preserved_values)
        or not math.isfinite(preserved_final)
        or not math.isfinite(daily_final)
    ):
        return False
    if not preserved_index.is_monotonic_increasing or preserved_index.has_duplicates:
        return False
    preserved_start = preserved_index[0].normalize()
    daily_start = daily_index[0].normalize()
    preserved_end = preserved_index[-1].normalize()
    daily_end = daily_index[-1].normalize()
    if preserved_start < daily_start or preserved_end > daily_end:
        return False
    if preserved_start - daily_start > MAX_PRESERVED_BOUNDARY_GAP:
        return False
    if daily_end - preserved_end > MAX_PRESERVED_BOUNDARY_GAP:
        return False
    preserved_days = preserved_index.normalize().nunique()
    daily_days = daily_index.normalize().nunique()
    if preserved_days / max(daily_days, 1) < MIN_PRESERVED_DAY_COVERAGE:
        return False
    gaps = preserved_index.to_series().diff().dropna()
    if gaps.empty or gaps.max() > MAX_PRESERVED_CURVE_GAP:
        return False
    tolerance = max(0.01, abs(daily_final) * 1e-9)
    return abs(preserved_final - daily_final) <= tolerance


def _curve_frequency(points: list[EquityPoint]) -> str:
    if len(points) < 2:
        return "unknown"
    try:
        spacing = (
            pd.to_datetime([point.time for point in points], utc=True)
            .to_series()
            .diff()
            .dropna()
            .median()
        )
    except (TypeError, ValueError):
        return "unknown"
    if pd.isna(spacing):
        return "unknown"
    if spacing >= pd.Timedelta(days=1):
        return "daily"
    return "intraday"


def _attach_daily_stats(
    result: BacktestResult,
    daily_returns: list[dict[str, Any]],
    *,
    equity_frequency: str = "daily",
) -> None:
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    result.meta.data["equity_frequency"] = equity_frequency
    result.meta.data["daily_return_count"] = len(daily_returns)


def build_daily_equity_curve(
    result: BacktestResult,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[EquityPoint], list[dict[str, Any]]]:
    initial_capital = float(result.meta.initial_capital)
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and > 0")

    equity_by_day: dict[str, float] = defaultdict(float)
    event_days: list[datetime] = []
    for trade in result.trades:
        pnl = (
            float(trade.net_pnl)
            if trade.net_pnl is not None
            else float(trade.pnl or 0.0) - float(trade.fees or 0.0)
        )
        if not math.isfinite(pnl):
            continue
        exit_time = _last_exit_time(trade)
        parsed = _parse_ts(exit_time)
        if parsed is None:
            continue
        event_days.append(parsed)
        equity_by_day[parsed.date().isoformat()] += pnl

    if not event_days:
        return [], []

    start_bound = _parse_ts(start) if start is not None else _parse_ts(result.meta.backtest_start)
    end_bound = _parse_ts(end) if end is not None else _parse_ts(result.meta.backtest_end)
    start = start_bound or (
        min(event_days).replace(hour=0, minute=0, second=0, microsecond=0)
        if event_days
        else None
    )
    end = end_bound or (
        max(event_days).replace(hour=0, minute=0, second=0, microsecond=0)
        if event_days
        else start
    )
    if start is None or end is None or end < start:
        return [], []
    start = start.astimezone(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    end = end.astimezone(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

    daily_points = [EquityPoint(time=start.isoformat(), equity=round(initial_capital, 2))]
    daily_returns: list[dict[str, Any]] = []
    running_equity = initial_capital
    cursor = start
    while cursor <= end:
        day = cursor.date().isoformat()
        pnl = equity_by_day.get(day, 0.0)
        previous = running_equity
        running_equity += pnl
        daily_returns.append(
            {
                "date": day,
                "return": (running_equity - previous) / previous if previous > 0 else None,
                "equity": running_equity,
            }
        )
        point_time = cursor + timedelta(days=1) - timedelta(microseconds=1)
        point = EquityPoint(time=point_time.isoformat(), equity=round(running_equity, 2))
        if daily_points[-1].time == point.time:
            daily_points[-1] = point
        else:
            daily_points.append(point)
        cursor += timedelta(days=1)

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
    daily_points, daily_returns = build_daily_equity_curve(
        scoped_result,
        start=start_bound,
        end=end_bound,
    )

    # If no daily points could be built (e.g., no valid exit times), fallback to existing curve
    if not daily_points:
        result.meta.data = result.meta.data or {}
        # Only mark as daily if we actually built a daily curve
        if result.equity_curve and len(result.equity_curve) > 1:
            result.meta.data["equity_frequency"] = _curve_frequency(result.equity_curve)
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
    result.meta.data = result.meta.data or {}
    result.meta.data["daily_equity_curve"] = [point.to_dict() for point in daily_points]
    if _preserves_engine_curve(preserved_curve, daily_points):
        result.equity_curve = preserved_curve
        _attach_daily_stats(
            result,
            daily_returns,
            equity_frequency=_curve_frequency(preserved_curve),
        )
        return result

    result.equity_curve = daily_points
    _attach_daily_stats(result, daily_returns, equity_frequency="daily")

    return result
