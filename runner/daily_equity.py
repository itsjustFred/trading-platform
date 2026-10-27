"""Daily equity curve construction from backtest trades."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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


def _trade_net_pnl(trade: Trade) -> float | None:
    pnl = (
        float(trade.net_pnl)
        if trade.net_pnl is not None
        else float(trade.pnl or 0.0) - float(trade.fees or 0.0)
    )
    return pnl if math.isfinite(pnl) else None


def _net_pnl_before(
    result: BacktestResult,
    cutoff: datetime,
    *,
    inclusive: bool = False,
) -> float:
    total = 0.0
    for trade in result.trades:
        exit_time = _parse_ts(_last_exit_time(trade))
        if exit_time is None:
            continue
        if exit_time > cutoff or (not inclusive and exit_time == cutoff):
            continue
        pnl = _trade_net_pnl(trade)
        if pnl is None:
            continue
        total += pnl
        if not math.isfinite(total):
            raise ValueError("Net trade PnL accumulation is non-finite")
    return total


def _window_starting_equity(
    result: BacktestResult,
    start_bound: datetime | None,
) -> float:
    initial_capital = float(result.meta.initial_capital)
    if not math.isfinite(initial_capital):
        raise ValueError("Initial capital must be finite")
    if start_bound is None:
        return initial_capital

    prior_points: list[tuple[datetime, float]] = []
    for point in result.equity_curve:
        point_time = _parse_ts(point.time)
        equity = float(point.equity)
        if point_time is not None and point_time < start_bound:
            prior_points.append((point_time, equity))

    starting_equity = initial_capital + _net_pnl_before(result, start_bound)
    if not prior_points:
        return starting_equity

    reconciled_through, reconciled_equity = max(prior_points, key=lambda item: item[0])
    expected_equity = initial_capital + _net_pnl_before(
        result,
        reconciled_through,
        inclusive=True,
    )
    if not math.isclose(
        reconciled_equity,
        expected_equity,
        rel_tol=0.0,
        abs_tol=0.01,
    ):
        return starting_equity
    return reconciled_equity + (
        starting_equity
        - initial_capital
        - _net_pnl_before(result, reconciled_through, inclusive=True)
    )


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
    preserved_curve: list[EquityPoint] | None,
    daily_points: list[EquityPoint],
    trades: list[Trade],
) -> bool:
    """Keep the engine curve only when it has more points than the daily build."""
    if not preserved_curve or not daily_points or len(preserved_curve) <= len(daily_points):
        return False
    if not all(math.isfinite(float(point.equity)) for point in preserved_curve):
        return False
    curve_end = _parse_ts(preserved_curve[-1].time)
    trade_end_times = [
        parsed
        for trade in trades
        if (parsed := _parse_ts(_last_exit_time(trade))) is not None
    ]
    if trade_end_times and (curve_end is None or curve_end < max(trade_end_times)):
        return False
    return math.isclose(
        float(preserved_curve[-1].equity),
        float(daily_points[-1].equity),
        rel_tol=0.0,
        abs_tol=0.01,
    )


def _strictly_increasing_curve(
    curve: list[EquityPoint],
    inception: datetime,
    end_bound: datetime | None,
) -> list[EquityPoint] | None:
    coalesced: list[EquityPoint] = []
    previous_source: datetime | None = None
    for point in curve:
        current = _parse_ts(point.time)
        if current is None:
            raise ValueError("Engine equity curve contains an invalid timestamp")
        if previous_source is not None and current < previous_source:
            raise ValueError("Engine equity curve timestamps are out of order")
        if previous_source is not None and current == previous_source:
            coalesced[-1] = point
        else:
            coalesced.append(point)
        previous_source = current

    normalized: list[EquityPoint] = []
    previous = inception
    for point in coalesced:
        current = _parse_ts(point.time)
        if current is None or current < previous:
            raise ValueError("Engine equity curve timestamps are not strictly ordered")
        equity = float(point.equity)
        if not math.isfinite(equity):
            raise ValueError("Engine equity curve contains non-finite equity")
        if current <= previous:
            current = previous + timedelta(microseconds=1)
            if end_bound is not None and current > end_bound:
                return None
        normalized.append(EquityPoint(time=current.isoformat(), equity=equity))
        previous = current
    return normalized


def _ensure_inception_point(
    result: BacktestResult,
    curve: list[EquityPoint],
    start_bound: datetime | None,
    end_bound: datetime | None,
    starting_equity: float,
) -> list[EquityPoint] | None:
    if not curve:
        return curve
    initial_capital = float(starting_equity)
    inception = start_bound or _parse_ts(result.meta.backtest_start)
    first_time = _parse_ts(curve[0].time)
    if inception is None:
        inception = first_time
    if inception is None:
        return None
    if first_time is not None and first_time < inception:
        raise ValueError("Engine equity curve begins before the requested window")
    matches_starting_equity = math.isclose(
        curve[0].equity,
        initial_capital,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    first_is_inception = first_time is not None and first_time == inception
    normalization_floor = (
        inception - timedelta(microseconds=1)
        if first_is_inception and matches_starting_equity
        else inception
    )
    normalized_curve = _strictly_increasing_curve(
        curve,
        normalization_floor,
        end_bound,
    )
    if normalized_curve is None:
        return None
    normalized_first_time = _parse_ts(normalized_curve[0].time)
    normalized_matches_starting_equity = math.isclose(
        normalized_curve[0].equity,
        initial_capital,
        rel_tol=0.0,
        abs_tol=1e-9,
    )
    if (
        first_is_inception
        and normalized_first_time == inception
        and normalized_matches_starting_equity
    ):
        return normalized_curve
    if normalized_first_time == inception:
        shifted_time = inception + timedelta(microseconds=1)
        if end_bound is not None and shifted_time > end_bound:
            return None
        normalized_curve[0] = EquityPoint(
            time=shifted_time.isoformat(),
            equity=normalized_curve[0].equity,
        )
    return [EquityPoint(time=inception.isoformat(), equity=initial_capital), *normalized_curve]


def _attach_daily_stats(result: BacktestResult, daily_returns: list[dict[str, Any]]) -> None:
    """Record the daily aggregation on stats and meta."""
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    result.meta.data["equity_frequency"] = "daily"
    result.meta.data["daily_return_count"] = len(daily_returns)


def build_daily_equity_curve(
    result: BacktestResult,
    start_bound: datetime | None = None,
    end_bound: datetime | None = None,
    starting_equity: float | None = None,
) -> tuple[list[EquityPoint], list[dict[str, Any]]]:
    """
    Build a daily equity curve from trade PnL.

    Returns (equity_points, daily_returns) where equity_points is a list of
    EquityPoint objects and daily_returns is a list of dicts with
    'date', 'return', 'equity'.
    """
    initial_capital = float(
        result.meta.initial_capital if starting_equity is None else starting_equity
    )
    if not math.isfinite(initial_capital):
        raise ValueError("Initial capital must be finite")
    equity_by_day: dict[str, float] = defaultdict(float)

    # Aggregate NET PnL per day from trades (#84: gross flatters the curve).
    for trade in result.trades:
        pnl = _trade_net_pnl(trade)
        if pnl is None:
            continue

        # Determine the day from the last exit
        exit_time = _last_exit_time(trade)
        if exit_time:
            dt = _parse_ts(exit_time)
            if dt is not None:
                day_key = dt.astimezone(timezone.utc).date().isoformat()
                equity_by_day[day_key] += pnl

    if not equity_by_day:
        return [], []

    inception = start_bound or _parse_ts(result.meta.backtest_start)
    if inception is None:
        inception = datetime.fromisoformat(min(equity_by_day)).replace(tzinfo=timezone.utc)
    daily_points = [EquityPoint(time=inception.isoformat(), equity=initial_capital)]
    daily_returns = []
    running_equity = initial_capital

    for day in sorted(equity_by_day.keys()):
        running_equity += equity_by_day[day]
        if not math.isfinite(running_equity):
            raise ValueError("Daily equity accumulation is non-finite")
        close = datetime.fromisoformat(day).replace(
            hour=23,
            minute=59,
            second=59,
            microsecond=999999,
            tzinfo=timezone.utc,
        )
        if (
            end_bound is not None
            and end_bound.astimezone(timezone.utc).date().isoformat() == day
        ):
            close = min(close, end_bound.astimezone(timezone.utc))
        daily_points.append(EquityPoint(time=close.isoformat(), equity=running_equity))
        prev_equity = running_equity - equity_by_day[day]
        if prev_equity > 0:
            daily_return = (running_equity - prev_equity) / prev_equity
        else:
            daily_return = 0.0
        daily_returns.append({"date": day, "return": daily_return, "equity": running_equity})

    normalized_closes = _strictly_increasing_curve(
        daily_points[1:],
        inception,
        end_bound,
    )
    if normalized_closes is None:
        raise ValueError("Daily equity closes cannot be normalized within the requested window")
    return [daily_points[0], *normalized_closes], daily_returns


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
    if start is not None and start_bound is None:
        raise ValueError("Daily equity start bound is invalid")
    if end is not None and end_bound is None:
        raise ValueError("Daily equity end bound is invalid")
    if start_bound is not None and end_bound is not None and start_bound > end_bound:
        raise ValueError("Daily equity start bound must not exceed end bound")
    if start_bound is not None and end_bound is not None and start_bound == end_bound:
        raise ValueError("Daily equity start and end bounds must differ")

    # If there are no trades and no existing equity curve, mark as unknown frequency
    if not result.trades and not result.equity_curve:
        result.equity_curve = []
        result.meta.data = result.meta.data or {}
        result.meta.data["equity_frequency"] = "unknown"
        result.meta.data.pop("daily_return_count", None)
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        result.stats["daily_returns"] = []
        return result

    # Build daily equity from trades, restricted to the requested window
    scoped_result = _scoped_view(result, start_bound, end_bound)
    starting_equity = _window_starting_equity(result, start_bound)
    preserved_curve = _windowed_curve(result, start_bound, end_bound)
    daily_points, daily_returns = build_daily_equity_curve(
        scoped_result,
        start_bound=start_bound,
        end_bound=end_bound,
        starting_equity=starting_equity,
    )

    if not daily_points:
        result.meta.data = result.meta.data or {}
        normalized_curve = _ensure_inception_point(
            scoped_result,
            preserved_curve or [],
            start_bound,
            end_bound,
            starting_equity,
        )
        result.equity_curve = normalized_curve or []
        result.meta.data["equity_frequency"] = (
            "daily" if len(result.equity_curve) > 1 else "unknown"
        )
        result.stats = result.stats or {}
        result.stats["daily_return_count"] = 0
        result.stats["daily_returns"] = []
        if result.meta.data["equity_frequency"] == "daily":
            result.meta.data["daily_return_count"] = 0
        else:
            result.meta.data.pop("daily_return_count", None)
        return result

    # When a window is active, an engine-provided curve must be scoped to the
    # same bounds before the length comparison: preserving the full unwindowed
    # curve next to window-scoped daily_returns would make the curve and the
    # reported daily returns describe different periods (pre-mortem 16-B #7).
    # _scoped_view computes exactly this view; reuse it instead of re-filtering.
    if _preserves_engine_curve(preserved_curve, daily_points, scoped_result.trades):
        normalized_curve = _ensure_inception_point(
            scoped_result,
            preserved_curve or [],
            start_bound,
            end_bound,
            starting_equity,
        )
        if normalized_curve is not None:
            _attach_daily_stats(result, daily_returns)
            result.equity_curve = normalized_curve
            return result

    # Attach the daily equity curve
    result.equity_curve = daily_points
    _attach_daily_stats(result, daily_returns)

    return result
