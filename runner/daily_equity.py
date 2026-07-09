#!/usr/bin/env python3
"""Daily equity-curve helpers for grid and portfolio optimization workflows."""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pandas as pd

from shared.models import BacktestResult, EquityPoint

logger = logging.getLogger(__name__)


def _parse_date(value: str | None, default: date | None = None) -> date | None:
    if not value:
        return default
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except (TypeError, ValueError):
            return default


def _date_bounds(result: BacktestResult, start_date: str | None, end_date: str | None) -> tuple[date | None, date | None]:
    start = _parse_date(start_date)
    end = _parse_date(end_date)

    if result.meta.backtest_start and start is None:
        start = _parse_date(result.meta.backtest_start)
    if result.meta.backtest_end and end is None:
        end = _parse_date(result.meta.backtest_end)

    trade_dates: list[date] = []
    for trade in result.trades:
        exit_time = getattr(trade, "last_exit_time", None) or getattr(trade, "exit_time", None)
        if exit_time:
            parsed = _parse_date(str(exit_time))
            if parsed:
                trade_dates.append(parsed)

    if trade_dates:
        start = min([d for d in [start, min(trade_dates)] if d]) if not start else start
        end = max([d for d in [end, max(trade_dates)] if d]) if not end else end

    if not start and result.equity_curve:
        start = _parse_date(result.equity_curve[0].time)
    if not end and result.equity_curve:
        end = _parse_date(result.equity_curve[-1].time)

    return start, end


def build_daily_equity_curve(
    result: BacktestResult,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[list[EquityPoint], list[dict[str, Any]]]:
    """Return daily realized equity points and daily returns for a backtest.

    The canonical trade format stores realized PnL on trades. For Sharpe,
    Sortino, correlation, and portfolio allocation we need an equity value for
    every calendar day in the backtest window, so missing days are forward-filled
    from the last known equity and daily returns are stored explicitly.
    """
    initial = float(result.meta.initial_capital or 0.0)
    if not math.isfinite(initial):
        initial = 0.0
    start, end = _date_bounds(result, start_date, end_date)
    if start is None or end is None or end < start:
        return result.equity_curve, []

    pnl_by_day: dict[date, float] = defaultdict(float)
    for trade in result.trades:
        exit_time = getattr(trade, "last_exit_time", None) or getattr(trade, "exit_time", None)
        exit_day = _parse_date(str(exit_time)) if exit_time else None
        if exit_day is None:
            continue
        try:
            pnl = float(trade.pnl or 0.0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pnl):
            # Never let nan/inf PnL contaminate the equity curve.
            logger.warning("Skipping non-finite PnL on %s for daily equity", exit_day)
            continue
        pnl_by_day[exit_day] += pnl

    points: list[EquityPoint] = []
    returns: list[dict[str, Any]] = []
    equity = initial
    previous_equity = initial

    current = start
    while current <= end:
        equity += pnl_by_day.get(current, 0.0)
        if not math.isfinite(equity):
            # Defensive: keep the curve finite even if upstream data is corrupt.
            logger.warning("Non-finite equity on %s; holding previous equity", current)
            equity = previous_equity
        equity = round(equity, 6)
        ts = datetime.combine(current, time.min, tzinfo=timezone.utc).isoformat()
        points.append(EquityPoint(time=ts, equity=equity))
        if current == start:
            daily_return = 0.0
        else:
            daily_return = equity / previous_equity - 1.0 if previous_equity else 0.0
        returns.append({"time": ts, "return": round(float(daily_return), 12), "equity": equity})
        previous_equity = equity
        current += timedelta(days=1)

    return points, returns


def attach_daily_equity(
    result: BacktestResult,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> BacktestResult:
    """Attach daily equity points and daily returns to a result in-place."""
    daily_points, daily_returns = build_daily_equity_curve(
        result,
        start_date=start_date,
        end_date=end_date,
    )
    result.equity_curve = daily_points
    result.stats = result.stats or {}
    result.stats["daily_return_count"] = len(daily_returns)
    result.stats["daily_returns"] = daily_returns
    result.meta.data = result.meta.data or {}
    # Only claim a daily frequency when an actual daily curve was synthesized.
    # The fallback path (invalid/empty date window) returns the original,
    # possibly non-daily curve, so it must not be mislabeled as "daily".
    result.meta.data["equity_frequency"] = "daily" if daily_returns else "unknown"
    result.meta.data["daily_return_count"] = len(daily_returns)
    return result
